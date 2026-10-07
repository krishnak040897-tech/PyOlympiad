import os
import re
import sys
import json
import time
import hmac
import uuid
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

# NEW: reject oversized request bodies early (code+input limits are far below this)
app.config["MAX_CONTENT_LENGTH"] = 512 * 1024

# Render sits behind a proxy: trust one hop so request.remote_addr
# is the real client IP.
app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1)

_slots = threading.BoundedSemaphore(config.MAX_CONCURRENT_RUNS)
_hits = defaultdict(deque)
_hits_lock = threading.Lock()

# ------------------------------------------------------------
#  Runtime package store (pip --target directories)
# ------------------------------------------------------------
#
# Every user-installed package lives in its own directory:
#
#       PKGS_DIR / <normalized-name> /  (the package + its deps)
#
# - Installing:  pip install --target PKGS_DIR/<name>-tmp <spec>
#                then atomically rename into place.
# - Uninstalling: shutil.rmtree(PKGS_DIR/<name>).
# - Running:      all existing package dirs are joined into
#                 PYTHONPATH for the student interpreter.
#
# NOTE: the store is shared by every user of this server, and
# student code runs as the same OS user. Treat it as
# classroom-trusted (the sandbox model of this app already is).
#
# NOTE: on Render the filesystem is ephemeral - installed
# packages last for the lifetime of the current server process.
# Permanent dependencies belong in requirements.txt.
#
PKGS_DIR = os.path.join(
    tempfile.gettempdir(),
    "pyolympiad_site_packages",
)

try:
    os.makedirs(PKGS_DIR, exist_ok=True)
except OSError:
    pass

# One pip operation at a time (per worker process).
_pip_lock = threading.Lock()

# Separate, stricter rate limiting for package operations.
_pkg_hits = defaultdict(deque)

# ------------------------------------------------------------
#  Isolated Linux launcher
# ------------------------------------------------------------
#
# Applies resource limits, then replaces itself with the student's
# Python process. The resource limits survive exec().
#
# IMPORTANT:
# The student interpreter intentionally does NOT use "-I"
# (isolated mode). Isolated mode ignores PYTHONPATH, which would
# hide the user-installed package directories. Isolation is
# preserved by:
#   - "-s"      : never load user site-packages
#   - a minimal server-controlled environment (no secrets)
#   - the launcher itself still runs with "-I"
#
# UTF-8 must still be explicitly enabled with "-X utf8=1"
# because PYTHONIOENCODING-style env settings are best-effort.
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

# NOTE: no "-I" here (it would ignore PYTHONPATH for
# user-installed packages). "-s" keeps user site-packages out.
os.execv(
    sys.executable,
    [
        sys.executable,
        "-s",
        "-B",
        "-X", "utf8=1",
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

def _package_site_dirs():
    """
    Return the site directories of every user-installed package.
    Temporary (.tmp-*) directories are skipped. Sorted so that
    PYTHONPATH order is stable across runs.
    """
    try:
        entries = os.listdir(PKGS_DIR)
    except OSError:
        return []

    dirs = []
    for name in sorted(entries):
        if name.startswith("."):
            continue
        full = os.path.join(PKGS_DIR, name)
        if os.path.isdir(full):
            dirs.append(full)
    return dirs

def _clean_env(workdir):
    """
    Pass only a minimal environment to student code.

    This prevents server secrets, credentials, and unrelated
    environment variables from being inherited.

    NEW: PYTHONPATH is set to the user-installed package
    directories so `import <package>` works inside the sandbox.
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

    pkg_dirs = _package_site_dirs()
    if pkg_dirs:
        env["PYTHONPATH"] = os.pathsep.join(pkg_dirs)

    # Windows Python needs SYSTEMROOT for normal operation.
    if os.name == "nt":
        env["SYSTEMROOT"] = os.environ.get(
            "SYSTEMROOT",
            "",
        )

    return env

def _pip_env():
    """
    Minimal environment for the pip subprocess.
    No PYTHONPATH, no server secrets.
    """
    env = {
        "PATH": os.environ.get(
            "PATH",
            "/usr/local/bin:/usr/bin:/bin",
        ),

        "HOME": PKGS_DIR,

        "LANG": "C.UTF-8",

        "PIP_DISABLE_PIP_VERSION_CHECK": "1",
    }

    if os.name == "nt":
        env["SYSTEMROOT"] = os.environ.get(
            "SYSTEMROOT",
            "",
        )

    return env

def _rate_limited(ip):
    """
    Basic per-IP rate limiter (code runs).
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

def _pkg_rate_limited(ip):
    """
    Stricter per-IP rate limiter for package operations.
    """
    now = time.time()

    with _hits_lock:
        q = _pkg_hits[ip]

        while q and now - q[0] > config.PKG_RATE_WINDOW:
            q.popleft()

        if len(q) >= config.PKG_RATE_MAX:
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
#  Package manager helpers (NEW)
# ------------------------------------------------------------

_NAME_RE = re.compile(r"^[A-Za-z0-9]([A-Za-z0-9._-]{0,98}[A-Za-z0-9])?$")
_VERSION_RE = re.compile(r"^[0-9][A-Za-z0-9._+!-]{0,49}$")

def _normalize_pkg(name):
    """PEP 503 style normalization, used as the package directory name."""
    return re.sub(r"[-_.]+", "-", name).lower()

def _package_allowed(norm_name):
    """
    If PACKAGES_ALLOWLIST is configured, only those packages may be
    installed. An empty list allows every name that passes the
    strict regex.
    """
    allowed = getattr(config, "ALLOWED_PACKAGES", None) or []
    if not allowed:
        return True
    allowed_norms = {_normalize_pkg(a) for a in allowed}
    return norm_name in allowed_norms

def _parse_spec(text):
    """
    Parse 'name' or 'name==1.2.3' -> (name, pip_spec), else None.

    Deliberately strict: no extras, no URLs, no comparators other
    than '==', no path separators. The spec is passed to pip as a
    single argv element (never through a shell).
    """
    if not isinstance(text, str):
        return None

    text = text.strip()
    if not text or len(text) > 120:
        return None

    if "==" in text:
        name, _, version = text.partition("==")
        name = name.strip()
        version = version.strip()

        if not _NAME_RE.match(name):
            return None
        if not _VERSION_RE.match(version):
            return None

        return name, f"{name}=={version}"

    if not _NAME_RE.match(text):
        return None

    return text, text

def _dist_version(pkg_dir):
    """Best-effort installed version from *.dist-info directory names."""
    try:
        for entry in os.listdir(pkg_dir):
            m = re.match(r"^(.+)-([^-]+)\.dist-info$", entry)
            if m:
                return m.group(2)
    except OSError:
        pass
    return ""

def _read_meta(pkg_dir):
    """Read the small meta.json written at install time."""
    try:
        with open(
            os.path.join(pkg_dir, "meta.json"),
            "r",
            encoding="utf-8",
        ) as f:
            data = json.load(f)
            if isinstance(data, dict):
                return data
    except Exception:
        pass
    return {}

def _dir_size(path):
    total = 0
    for root, _dirs, files in os.walk(path):
        for name in files:
            try:
                total += os.path.getsize(os.path.join(root, name))
            except OSError:
                pass
    return total

def _pip_tail(text, lines=40):
    """Keep only the last pip output lines for error responses."""
    if not text:
        return ""
    parts = text.strip().splitlines()
    if len(parts) <= lines:
        return text.strip()
    return "\n".join(parts[-lines:])

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

# Return JSON (not HTML) when the body limit is exceeded,
# so the frontend can display a readable message.
@app.errorhandler(413)
def _too_large(_e):
    return jsonify({
        "success": False,
        "output": "",
        "error": "Request is too large.",
    }), 413

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
        # "-X utf8=1" is specified directly on the Python command
        # line so Unicode output such as emoji and box-drawing
        # characters is emitted as UTF-8.
        #
        # The student interpreter uses "-s" (not "-I") so that
        # PYTHONPATH - which carries the user-installed package
        # directories - is honored. The environment is fully
        # controlled by the server, so isolation is preserved.
        #
        if os.name == "posix":

            cmd = [
                sys.executable,

                # Isolated Python environment (launcher only).
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

                # No user site-packages.
                "-s",

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
        # The frontend ANSI renderer converts them into safe
        # styled HTML.
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
#  Package manager routes (NEW)
# ------------------------------------------------------------

@app.route("/packages", methods=["GET"])
def packages_list():
    """
    List user-installed packages.
    """
    items = []

    try:
        entries = sorted(os.listdir(PKGS_DIR))
    except OSError:
        entries = []

    for entry in entries:
        if entry.startswith("."):
            continue

        full = os.path.join(PKGS_DIR, entry)
        if not os.path.isdir(full):
            continue

        meta = _read_meta(full)

        items.append({
            "name": meta.get("name") or entry,
            "version": _dist_version(full),
            "size": _dir_size(full),
            "installed_at": meta.get("installed_at", ""),
        })

    return jsonify({
        "success": True,
        "packages": items,
    })

@app.route("/packages/install", methods=["POST"])
def packages_install():
    """
    Install a PyPI package into its own site directory.
    Body: {"name": "requests"} or {"name": "requests==2.31.0"}
    """

    if not config.ENABLE_PACKAGES:
        return jsonify({
            "success": False,
            "error": "Package management is disabled on this server.",
        }), 503

    data = request.get_json(silent=True) or {}

    parsed = _parse_spec(data.get("name", ""))

    if not parsed:
        return jsonify({
            "success": False,
            "error": (
                "Invalid package name. Use a plain PyPI name such as "
                "'requests' or a pinned form such as 'requests==2.31.0'."
            ),
        }), 400

    name, spec = parsed
    norm = _normalize_pkg(name)

    if not _package_allowed(norm):
        return jsonify({
            "success": False,
            "error": "This package is not on the allowlist for this server.",
        }), 400

    if _pkg_rate_limited(request.remote_addr or "unknown"):
        return jsonify({
            "success": False,
            "error": (
                "Too many package operations. "
                "Please wait a few minutes and try again."
            ),
        }), 429

    if not _pip_lock.acquire(blocking=False):
        return jsonify({
            "success": False,
            "error": (
                "Another package operation is already running. "
                "Please try again shortly."
            ),
        }), 503

    dest = os.path.join(PKGS_DIR, norm)
    tmp = os.path.join(PKGS_DIR, ".tmp-" + uuid.uuid4().hex)
    out_path = tmp + ".log"
    err_path = tmp + ".err"

    proc = None

    try:

        os.makedirs(tmp, exist_ok=False)

        cmd = [
            sys.executable,
            "-m", "pip",
            "install",
            "--no-cache-dir",
            "--disable-pip-version-check",
            "--no-input",
            "--target", tmp,
            spec,
        ]

        if config.PIP_INDEX_URL:
            cmd += ["--index-url", config.PIP_INDEX_URL]

        with open(out_path, "wb") as fout, open(err_path, "wb") as ferr:

            proc = subprocess.Popen(
                cmd,
                stdout=fout,
                stderr=ferr,
                cwd=PKGS_DIR,
                env=_pip_env(),
                start_new_session=(os.name == "posix"),
            )

            try:
                proc.wait(timeout=config.PIP_TIMEOUT)
                timed_out = False
            except subprocess.TimeoutExpired:
                _kill(proc)
                timed_out = True

        pip_output = _truncate(
            _read(out_path, config.MAX_OUTPUT_SIZE),
            config.MAX_OUTPUT_SIZE,
        )
        pip_errors = _read(err_path, 4000)

        if timed_out:
            return jsonify({
                "success": False,
                "output": pip_output,
                "error": (
                    "Package installation timed out. "
                    "Try again, or ask the administrator to raise PIP_TIMEOUT."
                ),
            }), 400

        if proc.returncode != 0:
            detail = (
                pip_errors.strip()
                or pip_output.strip()
                or f"pip exited with code {proc.returncode}."
            )
            return jsonify({
                "success": False,
                "output": pip_output,
                "error": _pip_tail(detail),
            }), 400

        if not os.path.isdir(tmp) or not os.listdir(tmp):
            return jsonify({
                "success": False,
                "output": pip_output,
                "error": "pip finished but installed no files.",
            }), 400

        # Reinstall / upgrade: replace any existing directory.
        if os.path.isdir(dest):
            shutil.rmtree(dest, ignore_errors=True)

        os.rename(tmp, dest)

        meta = {
            "name": name,
            "spec": spec,
            "installed_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        }

        try:
            with open(
                os.path.join(dest, "meta.json"),
                "w",
                encoding="utf-8",
            ) as f:
                json.dump(meta, f)
        except OSError:
            pass

        version = _dist_version(dest)
        message = "Installed " + spec
        if version:
            message += " (" + version + ")"
        message += "."

        return jsonify({
            "success": True,
            "output": pip_output,
            "message": message,
        })

    except Exception:

        app.logger.exception("package install failed")

        return jsonify({
            "success": False,
            "error": "Internal server error while installing the package.",
        }), 500

    finally:

        if proc is not None and proc.poll() is None:
            _kill(proc)

        shutil.rmtree(tmp, ignore_errors=True)

        for p in (out_path, err_path):
            try:
                os.remove(p)
            except OSError:
                pass

        _pip_lock.release()

@app.route("/packages/uninstall", methods=["POST"])
def packages_uninstall():
    """
    Remove an installed package directory (including the
    dependencies that were bundled into it).
    Body: {"name": "requests"}
    """

    if not config.ENABLE_PACKAGES:
        return jsonify({
            "success": False,
            "error": "Package management is disabled on this server.",
        }), 503

    data = request.get_json(silent=True) or {}

    raw = str(data.get("name", "")).strip()

    # Tolerate 'name==1.2.3' from the terminal.
    if "==" in raw:
        raw = raw.split("==", 1)[0].strip()

    if not _NAME_RE.match(raw):
        return jsonify({
            "success": False,
            "error": "Invalid package name.",
        }), 400

    if _pkg_rate_limited(request.remote_addr or "unknown"):
        return jsonify({
            "success": False,
            "error": (
                "Too many package operations. "
                "Please wait a few minutes and try again."
            ),
        }), 429

    if not _pip_lock.acquire(blocking=False):
        return jsonify({
            "success": False,
            "error": (
                "Another package operation is already running. "
                "Please try again shortly."
            ),
        }), 503

    try:

        norm = _normalize_pkg(raw)
        dest = os.path.join(PKGS_DIR, norm)

        if not os.path.isdir(dest):
            return jsonify({
                "success": False,
                "error": "'" + raw + "' is not installed.",
            }), 400

        shutil.rmtree(dest, ignore_errors=True)

        if os.path.isdir(dest):
            return jsonify({
                "success": False,
                "error": "Could not fully remove '" + raw + "'.",
            }), 400

        return jsonify({
            "success": True,
            "message": (
                "Removed '" + raw + "' "
                "(and any dependencies bundled with it)."
            ),
        })

    except Exception:

        app.logger.exception("package uninstall failed")

        return jsonify({
            "success": False,
            "error": "Internal server error while removing the package.",
        }), 500

    finally:

        _pip_lock.release()

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
