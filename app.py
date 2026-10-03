import os
import sys
import socket
import signal
import tempfile
import subprocess

from flask import Flask, render_template, request, jsonify

import config

app = Flask(__name__)
app.config["SECRET_KEY"] = getattr(config, "SECRET_KEY", "python-editor-secret-key-change-me")


# ------------------------------------------------------------
#  Helpers
# ------------------------------------------------------------

def _get_local_ip():
    """Detect LAN IP address for easy mobile testing."""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        # Does not actually create a connection, just resolves routing
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
    except Exception:
        ip = "127.0.0.1"
    finally:
        s.close()
    return ip


def _apply_resource_limits():
    """Apply CPU + memory limits to the child process (POSIX only)."""
    try:
        import resource
        # CPU seconds
        resource.setrlimit(
            resource.RLIMIT_CPU,
            (config.CPU_LIMIT, config.CPU_LIMIT),
        )
        # Address space (memory)
        resource.setrlimit(
            resource.RLIMIT_AS,
            (config.MEMORY_LIMIT, config.MEMORY_LIMIT),
        )
        # No core dumps
        resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
    except Exception:
        # Limits unavailable (e.g. Windows) – continue without them.
        pass


def _child_preexec():
    """Prepare child execution environment: process group & resource limits."""
    if hasattr(os, "setsid"):
        os.setsid()
    _apply_resource_limits()


def _sanitize(text, temp_path):
    """Replace the temporary file path with 'main.py' in tracebacks."""
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


# ------------------------------------------------------------
#  Routes
# ------------------------------------------------------------

@app.route("/")
def index():
    return render_template("index.html")


@app.route("/run", methods=["POST"])
def run_code():
    data = request.get_json(silent=True) or {}

    code = data.get("code", "")
    user_input = data.get("input", "")

    if not isinstance(code, str):
        return jsonify({"success": False, "output": "", "error": "Invalid code."}), 400
    if not isinstance(user_input, str):
        user_input = ""

    # ---------- Size validation ----------
    if len(code) > config.MAX_CODE_SIZE:
        return jsonify({"success": False, "output": "", "error": "Code is too large."}), 400
    if len(user_input) > config.MAX_INPUT_SIZE:
        return jsonify({"success": False, "output": "", "error": "Input is too large."}), 400
    if not code.strip():
        return jsonify({"success": False, "output": "", "error": "Please enter some Python code."}), 400

    temp_path = None
    proc = None

    try:
        # ---------- Write code to a temp file ----------
        fd, temp_path = tempfile.mkstemp(suffix=".py", text=True)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(code)

        # ---------- Spawn an isolated Python process ----------
        # -I : isolated mode (no user site, no env imports)
        # -B : don't write .pyc files
        cmd = [sys.executable, "-I", "-B", temp_path]

        proc = subprocess.Popen(
            cmd,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            cwd=os.path.dirname(temp_path),
            preexec_fn=_child_preexec if sys.platform != "win32" else None,
        )

        # ---------- Communicate (with timeout) ----------
        try:
            stdout, stderr = proc.communicate(
                input=user_input,
                timeout=config.CODE_TIMEOUT,
            )
        except subprocess.TimeoutExpired:
            # Kill the process or process group on timeout
            try:
                if hasattr(os, "killpg") and hasattr(os, "getpgid"):
                    os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
                else:
                    proc.kill()
            except Exception:
                proc.kill()

            try:
                proc.wait(timeout=2)
            except Exception:
                pass

            return jsonify({
                "success": False,
                "output": "",
                "error": (
                    "Program execution timed out.\n"
                    "Please check your code for an infinite loop "
                    "or missing input()."
                ),
            })

        # ---------- Truncate & sanitize ----------
        stdout = _truncate(stdout, config.MAX_OUTPUT_SIZE)
        stderr = _truncate(stderr, config.MAX_OUTPUT_SIZE)
        stderr = _sanitize(stderr, temp_path)

        # ---------- Return response ----------
        if proc.returncode == 0:
            return jsonify({
                "success": True,
                "output": stdout,
                "error": "",
            })

        return jsonify({
            "success": False,
            "output": stdout,
            "error": stderr or f"Program exited with code {proc.returncode}.",
        })

    except Exception as error:
        return jsonify({
            "success": False,
            "output": "",
            "error": str(error),
        })

    finally:
        if temp_path and os.path.exists(temp_path):
            try:
                os.remove(temp_path)
            except Exception:
                pass


# ------------------------------------------------------------
#  Entry point
# ------------------------------------------------------------

if __name__ == "__main__":
    host = "0.0.0.0"
    port = getattr(config, "PORT", 5000)
    debug = getattr(config, "DEBUG", True)
    local_ip = _get_local_ip()

    print("\n" + "=" * 54)
    print(" Python Web Compiler Ready")
    print(f" Local Machine : http://127.0.0.1:{port}")
    print(f" Mobile / LAN  : http://{local_ip}:{port}")
    print("=" * 54 + "\n")

    app.run(host=host, port=port, debug=debug)