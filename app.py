import os
import sys
import time
import hmac
import shutil
import signal
import tempfile
import threading
import subprocess
from collections import defaultdict, deque

from flask import Flask, render_template, request, jsonify, Response
from werkzeug.middleware.proxy_fix import ProxyFix

import config

app = Flask(__name__)
app.config["SECRET_KEY"] = config.SECRET_KEY

# Render sits behind a proxy: trust one hop so request.remote_addr
# is the real client IP.
app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1)

_slots = threading.BoundedSemaphore(config.MAX_CONCURRENT_RUNS)
_hits = defaultdict(deque)
_hits_lock = threading.Lock()

# ------------------------------------------------------------
#  Isolated Linux launcher
# ------------------------------------------------------------
#
# Applies resource limits, then replaces itself with the student's
# Python process. The resource limits survive exec().
#
# IMPORTANT:
# Python's -I isolated mode ignores PYTHONIOENCODING and PYTHONUTF8
# environment variables. Therefore UTF-8 must also be explicitly
# enabled with "-X utf8=1".
#
# This is especially important for Unicode output such as:
#   ✨ 🏆 ╔ ═ ║ ╗ █
#
# ANSI escape sequences are NOT removed here. They are intentionally
# preserved in stdout/stderr so the browser can render them.
#
_LAUNCHER = r"""
import os
import sys

try:
    import resource

    cpu, mem, fsize = (int(x) for x in sys.argv[2:5])

    resource.setrlimit(
        resource.RLIMIT_CPU,
        (cpu, cpu)
    )

    resource.setrlimit(
        resource.RLIMIT_AS,
        (mem, mem)
    )

    resource.setrlimit(
        resource.RLIMIT_FSIZE,
        (fsize, fsize)
    )

    resource.setrlimit(
        resource.RLIMIT_CORE,
        (0, 0)
    )

except Exception:
    pass

# IMPORTANT:
# -I isolates the Python environment.
# PYTHONIOENCODING is therefore not sufficient.
# Explicitly enable UTF-8 mode for the student's interpreter.
os.execv(
    sys.executable,
    [
        sys.executable,
        "-I",
        "-X", "utf8=1",
        "-B",
        sys.argv[1],
    ],
)
"""

# ------------------------------------------------------------
#  Helpers
# ------------------------------------------------------------

def _sanitize(text, temp_path):
    """
    Show 'main.py' instead of the real temporary path in tracebacks.
    """
    if not text:
        return ""

    if temp_path:
        text = text.replace(temp_path, "main.py")

        parent = os.path.dirname(temp_path)
        if parent:
            text = text.replace(parent + os.sep, "")

    return text

def _truncate(text, limit):
    """
    Keep returned output within the configured response size.
    """
    if text and len(text) > limit:
        return text[:limit] + "\n... [output truncated]"

    return text or ""

def _read(path, limit):
    """
    Read captured program output as UTF-8.

    errors='replace' prevents a malformed byte sequence from crashing
    the server. Valid Unicode, emoji, box-drawing characters, etc.
    are preserved.
    """
    try:
        with open(
            path,
            "r",
            encoding="utf-8",
            errors="replace",
        ) as f:
            return f.read(limit + 1)

    except OSError:
        return ""

def _clean_env(workdir):
    """
    Pass only a minimal environment to student code.

    This prevents server secrets, credentials, and unrelated
    environment variables from being inherited.

    UTF-8 is explicitly requested here. The child interpreter also
    receives '-X utf8=1' because Python isolated mode (-I) does not
    honor PYTHONIOENCODING.
    """

    env = {
        "PATH": os.environ.get(
            "PATH",
            "/usr/local/bin:/usr/bin:/bin",
        ),

        "HOME": workdir,

        "TMPDIR": workdir,

        "LANG": "C.UTF-8",

        # Keep these for normal Python launches.
        "PYTHONIOENCODING": "utf-8",
        "PYTHONDONTWRITEBYTECODE": "1",
    }

    # Windows Python needs SYSTEMROOT for normal operation.
    if os.name == "nt":
        env["SYSTEMROOT"] = os.environ.get(
            "SYSTEMROOT",
            "",
        )

    return env

def _rate_limited(ip):
    """
    Basic per-IP rate limiter.
    """
    now = time.time()

    with _hits_lock:

        # Keep memory bounded.
        if len(_hits) > 5000:
            stale_keys = [
                key
                for key, q in _hits.items()
                if not q
                or now - q[-1] > config.RATE_LIMIT_WINDOW
            ]

            for key in stale_keys:
                del _hits[key]

        q = _hits[ip]

        while q and now - q[0] > config.RATE_LIMIT_WINDOW:
            q.popleft()

        if len(q) >= config.RATE_LIMIT_MAX:
            return True

        q.append(now)

        return False

def _kill(proc):
    """
    Kill a running student process and wait briefly for cleanup.

    On POSIX, the process is started in its own process group so the
    entire child process group can be terminated.
    """

    try:
        os.killpg(
            os.getpgid(proc.pid),
            signal.SIGKILL,
        )

    except Exception:
        try:
            proc.kill()

        except Exception:
            pass

    try:
        proc.wait(timeout=2)

    except Exception:
        pass

# ------------------------------------------------------------
#  Optional site-wide password
#  Set BASIC_AUTH_USER / BASIC_AUTH_PASS in config.py
# ------------------------------------------------------------

@app.before_request
def _require_login():

    if not (
        config.BASIC_AUTH_USER
        and config.BASIC_AUTH_PASS
    ):
        return None

    if request.path == "/health":
        return None

    auth = request.authorization

    if auth:

        user_ok = hmac.compare_digest(
            (auth.username or "").encode(),
            config.BASIC_AUTH_USER.encode(),
        )

        pass_ok = hmac.compare_digest(
            (auth.password or "").encode(),
            config.BASIC_AUTH_PASS.encode(),
        )

        if user_ok and pass_ok:
            return None

    return Response(
        "Login required",
        401,
        {
            "WWW-Authenticate":
                'Basic realm="PyOlympiad"'
        },
    )

# ------------------------------------------------------------
#  Routes
# ------------------------------------------------------------

@app.route("/")
def index():
    return render_template("index.html")

@app.route("/health")
def health():
    return "ok", 200

@app.route("/run", methods=["POST"])
def run_code():

    data = request.get_json(silent=True) or {}

    code = data.get("code", "")
    user_input = data.get("input", "")

    # --------------------------------------------------------
    # Validate request
    # --------------------------------------------------------

    if not isinstance(code, str):
        return jsonify({
            "success": False,
            "output": "",
            "error": "Invalid code.",
        }), 400

    if not isinstance(user_input, str):
        user_input = ""

    if len(code) > config.MAX_CODE_SIZE:
        return jsonify({
            "success": False,
            "output": "",
            "error": "Code is too large.",
        }), 400

    if len(user_input) > config.MAX_INPUT_SIZE:
        return jsonify({
            "success": False,
            "output": "",
            "error": "Input is too large.",
        }), 400

    if not code.strip():
        return jsonify({
            "success": False,
            "output": "",
            "error": "Please enter some Python code.",
        }), 400

    # --------------------------------------------------------
    # Rate limiting
    # --------------------------------------------------------

    if _rate_limited(
        request.remote_addr or "unknown"
    ):
        return jsonify({
            "success": False,
            "output": "",
            "error": (
                "Too many runs in a short time. "
                "Please wait a few seconds and try again."
            ),
        }), 429

    # --------------------------------------------------------
    # Concurrent execution limit
    # --------------------------------------------------------

    if not _slots.acquire(blocking=False):
        return jsonify({
            "success": False,
            "output": "",
            "error": (
                "The server is busy running other programs. "
                "Please try again in a moment."
            ),
        }), 503

    workdir = None
    proc = None

    try:

        # ----------------------------------------------------
        # Every run receives a completely isolated directory.
        # ----------------------------------------------------

        workdir = tempfile.mkdtemp(
            prefix="pyrun_"
        )

        temp_path = os.path.join(
            workdir,
            "main.py",
        )

        in_path = os.path.join(
            workdir,
            ".stdin",
        )

        out_path = os.path.join(
            workdir,
            ".stdout",
        )

        err_path = os.path.join(
            workdir,
            ".stderr",
        )

        # ----------------------------------------------------
        # Write student's source code as UTF-8.
        # ----------------------------------------------------

        with open(
            temp_path,
            "w",
            encoding="utf-8",
        ) as f:
            f.write(code)

        # ----------------------------------------------------
        # Write supplied input as UTF-8.
        # ----------------------------------------------------

        with open(
            in_path,
            "w",
            encoding="utf-8",
        ) as f:
            f.write(user_input)

        # ----------------------------------------------------
        # Build execution command.
        # ----------------------------------------------------
        #
        # IMPORTANT:
        # "-X utf8=1" is deliberately specified directly on the
        # Python command line.
        #
        # This is required because "-I" isolated mode does not
        # honor PYTHONIOENCODING/PYTHONUTF8 from the environment.
        #
        # Therefore Unicode output such as:
        #
        #   ✨
        #   🏆
        #   ╔══════════════╗
        #   ██████████████
        #
        # is emitted as UTF-8 instead of Windows CP1252.
        #
        if os.name == "posix":

            cmd = [
                sys.executable,

                # Isolated Python environment.
                "-I",

                # Explicit UTF-8 mode.
                "-X",
                "utf8=1",

                # Execute the launcher.
                "-c",
                _LAUNCHER,

                # Launcher arguments.
                temp_path,
                str(config.CPU_LIMIT),
                str(config.MEMORY_LIMIT),
                str(config.MAX_FILE_BYTES),
            ]

        else:

            cmd = [
                sys.executable,

                # Isolated Python environment.
                "-I",

                # Explicit UTF-8 mode.
                "-X",
                "utf8=1",

                # Don't create __pycache__.
                "-B",

                # Student program.
                temp_path,
            ]

        # ----------------------------------------------------
        # Open redirected stdin/stdout/stderr.
        # ----------------------------------------------------
        #
        # Files are used instead of subprocess.PIPE so that
        # unlimited output cannot accumulate in server memory.
        #
        # On POSIX, RLIMIT_FSIZE is also applied by _LAUNCHER.
        #

        with open(
            in_path,
            "rb",
        ) as fin, open(
            out_path,
            "wb",
        ) as fout, open(
            err_path,
            "wb",
        ) as ferr:

            proc = subprocess.Popen(
                cmd,

                stdin=fin,
                stdout=fout,
                stderr=ferr,

                cwd=workdir,

                env=_clean_env(workdir),

                # Give POSIX executions their own process group
                # so timeout termination kills child processes too.
                start_new_session=(
                    os.name == "posix"
                ),
            )

            # ------------------------------------------------
            # Wait for normal completion or timeout.
            # ------------------------------------------------

            try:

                proc.wait(
                    timeout=config.CODE_TIMEOUT
                )

                timed_out = False

            except subprocess.TimeoutExpired:

                _kill(proc)

                timed_out = True

        # ----------------------------------------------------
        # Read captured output.
        # ----------------------------------------------------
        #
        # ANSI escape sequences are intentionally preserved.
        #
        # For example:
        #
        #   ESC[1;33m✨ Student Profile ESC[0m
        #
        # reaches the frontend unchanged.
        #
        # The frontend ANSI renderer is responsible for converting
        # those sequences into safe styled HTML.
        #
        stdout = _truncate(
            _read(
                out_path,
                config.MAX_OUTPUT_SIZE,
            ),
            config.MAX_OUTPUT_SIZE,
        )

        stderr = _truncate(
            _read(
                err_path,
                config.MAX_OUTPUT_SIZE,
            ),
            config.MAX_OUTPUT_SIZE,
        )

        # Hide temporary server paths in tracebacks.
        stderr = _sanitize(
            stderr,
            temp_path,
        )

        # ----------------------------------------------------
        # Timeout response
        # ----------------------------------------------------

        if timed_out:

            return jsonify({
                "success": False,

                "output": stdout,

                "error": (
                    "Program execution timed out.\n"
                    "Please check your code for an infinite loop "
                    "or missing input()."
                ),
            })

        # ----------------------------------------------------
        # Successful execution
        # ----------------------------------------------------

        if proc.returncode == 0:

            return jsonify({
                "success": True,
                "output": stdout,
                "error": "",
            })

        # ----------------------------------------------------
        # Program error
        # ----------------------------------------------------

        return jsonify({
            "success": False,

            "output": stdout,

            "error": (
                stderr
                or f"Program exited with code {proc.returncode}."
            ),
        })

    # --------------------------------------------------------
    # Unexpected server-side error
    # --------------------------------------------------------

    except Exception:

        app.logger.exception(
            "run failed"
        )

        return jsonify({
            "success": False,
            "output": "",
            "error": (
                "Internal server error. "
                "Please try again."
            ),
        }), 500

    # --------------------------------------------------------
    # Always clean up
    # --------------------------------------------------------

    finally:

        if (
            proc is not None
            and proc.poll() is None
        ):
            _kill(proc)

        if workdir:
            shutil.rmtree(
                workdir,
                ignore_errors=True,
            )

        _slots.release()

# ------------------------------------------------------------
#  Entry point
#
#  Local development only.
#  Render uses gunicorn.
# ------------------------------------------------------------

if __name__ == "__main__":

    app.run(
        host=config.HOST,
        port=config.PORT,
        debug=config.DEBUG,
    )
