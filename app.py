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
# Render sits behind a proxy: trust one hop so request.remote_addr is the real client IP
app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1)

_slots = threading.BoundedSemaphore(config.MAX_CONCURRENT_RUNS)
_hits = defaultdict(deque)
_hits_lock = threading.Lock()

# Small launcher: applies resource limits, then replaces itself with the
# isolated Python process that runs the student's program (limits survive exec).
_LAUNCHER = """
import os, sys
try:
    import resource
    cpu, mem, fsize = (int(x) for x in sys.argv[2:5])
    resource.setrlimit(resource.RLIMIT_CPU,   (cpu, cpu))
    resource.setrlimit(resource.RLIMIT_AS,    (mem, mem))
    resource.setrlimit(resource.RLIMIT_FSIZE, (fsize, fsize))
    resource.setrlimit(resource.RLIMIT_CORE,  (0, 0))
except Exception:
    pass
os.execv(sys.executable, [sys.executable, "-I", "-B", sys.argv[1]])
"""


# ------------------------------------------------------------
#  Helpers
# ------------------------------------------------------------

def _sanitize(text, temp_path):
    """Show 'main.py' instead of the real temporary path in tracebacks."""
    if not text:
        return ""
    if temp_path:
        text = text.replace(temp_path, "main.py")
        parent = os.path.dirname(temp_path)
        if parent:
            text = text.replace(parent + os.sep, "")
    return text


def _truncate(text, limit):
    if text and len(text) > limit:
        return text[:limit] + "\n... [output truncated]"
    return text or ""


def _read(path, limit):
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            return f.read(limit + 1)
    except OSError:
        return ""


def _clean_env(workdir):
    """Pass NOTHING from the server's environment (SECRET_KEY, passwords...) to student code."""
    env = {
        "PATH": os.environ.get("PATH", "/usr/local/bin:/usr/bin:/bin"),
        "HOME": workdir,
        "TMPDIR": workdir,
        "LANG": "C.UTF-8",
        "PYTHONIOENCODING": "utf-8",
        "PYTHONDONTWRITEBYTECODE": "1",
    }
    if os.name == "nt":
        env["SYSTEMROOT"] = os.environ.get("SYSTEMROOT", "")
    return env


def _rate_limited(ip):
    now = time.time()
    with _hits_lock:
        if len(_hits) > 5000:                      # keep memory bounded
            for key in [k for k, q in _hits.items() if not q or now - q[-1] > config.RATE_LIMIT_WINDOW]:
                del _hits[key]
        q = _hits[ip]
        while q and now - q[0] > config.RATE_LIMIT_WINDOW:
            q.popleft()
        if len(q) >= config.RATE_LIMIT_MAX:
            return True
        q.append(now)
        return False


def _kill(proc):
    try:
        os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
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
#  Optional site-wide password (set BASIC_AUTH_USER / BASIC_AUTH_PASS)
# ------------------------------------------------------------

@app.before_request
def _require_login():
    if not (config.BASIC_AUTH_USER and config.BASIC_AUTH_PASS):
        return None
    if request.path == "/health":
        return None
    auth = request.authorization
    if auth:
        user_ok = hmac.compare_digest((auth.username or "").encode(), config.BASIC_AUTH_USER.encode())
        pass_ok = hmac.compare_digest((auth.password or "").encode(), config.BASIC_AUTH_PASS.encode())
        if user_ok and pass_ok:
            return None
    return Response("Login required", 401, {"WWW-Authenticate": 'Basic realm="PyOlympiad"'})


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

    code       = data.get("code", "")
    user_input = data.get("input", "")

    if not isinstance(code, str):
        return jsonify({"success": False, "output": "", "error": "Invalid code."}), 400
    if not isinstance(user_input, str):
        user_input = ""

    if len(code) > config.MAX_CODE_SIZE:
        return jsonify({"success": False, "output": "", "error": "Code is too large."}), 400
    if len(user_input) > config.MAX_INPUT_SIZE:
        return jsonify({"success": False, "output": "", "error": "Input is too large."}), 400
    if not code.strip():
        return jsonify({"success": False, "output": "", "error": "Please enter some Python code."}), 400

    if _rate_limited(request.remote_addr or "unknown"):
        return jsonify({"success": False, "output": "",
                        "error": "Too many runs in a short time. Please wait a few seconds and try again."}), 429

    if not _slots.acquire(blocking=False):
        return jsonify({"success": False, "output": "",
                        "error": "The server is busy running other programs. Please try again in a moment."}), 503

    workdir = None
    proc = None
    try:
        # Every run gets its own private folder, deleted afterwards
        workdir   = tempfile.mkdtemp(prefix="pyrun_")
        temp_path = os.path.join(workdir, "main.py")
        in_path   = os.path.join(workdir, ".stdin")
        out_path  = os.path.join(workdir, ".stdout")
        err_path  = os.path.join(workdir, ".stderr")

        with open(temp_path, "w", encoding="utf-8") as f:
            f.write(code)
        with open(in_path, "w", encoding="utf-8") as f:
            f.write(user_input)

        if os.name == "posix":
            cmd = [sys.executable, "-I", "-c", _LAUNCHER, temp_path,
                   str(config.CPU_LIMIT), str(config.MEMORY_LIMIT), str(config.MAX_FILE_BYTES)]
        else:
            cmd = [sys.executable, "-I", "-B", temp_path]

        # stdin/stdout/stderr are plain files (size-capped by RLIMIT_FSIZE),
        # so a program that prints forever cannot fill the server's memory.
        with open(in_path, "rb") as fin, open(out_path, "wb") as fout, open(err_path, "wb") as ferr:
            proc = subprocess.Popen(
                cmd,
                stdin=fin, stdout=fout, stderr=ferr,
                cwd=workdir,
                env=_clean_env(workdir),
                start_new_session=(os.name == "posix"),
            )
            try:
                proc.wait(timeout=config.CODE_TIMEOUT)
                timed_out = False
            except subprocess.TimeoutExpired:
                _kill(proc)
                timed_out = True

        stdout = _truncate(_read(out_path, config.MAX_OUTPUT_SIZE), config.MAX_OUTPUT_SIZE)
        stderr = _truncate(_read(err_path, config.MAX_OUTPUT_SIZE), config.MAX_OUTPUT_SIZE)
        stderr = _sanitize(stderr, temp_path)

        if timed_out:
            return jsonify({
                "success": False,
                "output": stdout,
                "error": ("Program execution timed out.\n"
                          "Please check your code for an infinite loop or missing input()."),
            })

        if proc.returncode == 0:
            return jsonify({"success": True, "output": stdout, "error": ""})

        return jsonify({
            "success": False,
            "output": stdout,
            "error": stderr or f"Program exited with code {proc.returncode}.",
        })

    except Exception:
        app.logger.exception("run failed")
        return jsonify({"success": False, "output": "", "error": "Internal server error. Please try again."}), 500

    finally:
        if proc is not None and proc.poll() is None:
            _kill(proc)
        if workdir:
            shutil.rmtree(workdir, ignore_errors=True)
        _slots.release()


# ------------------------------------------------------------
#  Entry point (local development only; Render uses gunicorn)
# ------------------------------------------------------------

if __name__ == "__main__":
    app.run(host=config.HOST, port=config.PORT, debug=config.DEBUG)
