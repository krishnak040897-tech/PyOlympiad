# ============================================================
#  PyOlympiad - configuration
#  Values can be overridden with environment variables
#  (set them in the Render dashboard -> Environment).
# ============================================================
import os


def _int(name, default):
    try:
        return int(os.environ.get(name, default))
    except ValueError:
        return default


SECRET_KEY = os.environ.get("SECRET_KEY", "dev-only-change-me")

DEBUG = os.environ.get("DEBUG", "false").lower() == "true"
HOST  = os.environ.get("HOST", "0.0.0.0")
PORT  = _int("PORT", 5000)          # Render injects PORT automatically

# Limits for user code / input / output
MAX_CODE_SIZE   = 20000             # characters
MAX_INPUT_SIZE  = 10000             # characters
MAX_OUTPUT_SIZE = 50000             # characters (per stream)

# Execution sandbox limits
CODE_TIMEOUT   = _int("CODE_TIMEOUT", 5)             # seconds (wall clock)
CPU_LIMIT      = _int("CPU_LIMIT", 10)               # seconds (CPU time)
MEMORY_LIMIT   = _int("MEMORY_MB", 256) * 1024 * 1024
MAX_FILE_BYTES = 1024 * 1024                         # biggest file/output a program may write

# Protect the shared server
MAX_CONCURRENT_RUNS = _int("MAX_CONCURRENT_RUNS", 3)  # programs running at the same time
RATE_LIMIT_MAX      = _int("RATE_LIMIT_MAX", 120)     # runs allowed per IP ...
RATE_LIMIT_WINDOW   = _int("RATE_LIMIT_WINDOW", 60)   # ... per this many seconds

# Optional password for the whole site (leave empty to disable)
BASIC_AUTH_USER = os.environ.get("BASIC_AUTH_USER", "")
BASIC_AUTH_PASS = os.environ.get("BASIC_AUTH_PASS", "")

# ------------------------------------------------------------
#  Runtime package manager (pip) - NEW
#  Lets students install/uninstall libraries from the built-in
#  Packages terminal. Installs are stored under the OS temp dir
#  and last for the lifetime of the server process (ephemeral
#  on Render). Permanent dependencies belong in requirements.txt.
#
#  Set ENABLE_PACKAGES=false to hide/disable this feature, and
#  PACKAGES_ALLOWLIST="requests,numpy,pandas" to restrict which
#  packages may be installed (comma or space separated).
#  PIP_INDEX_URL can point at a mirror.
# ------------------------------------------------------------
ENABLE_PACKAGES  = os.environ.get("ENABLE_PACKAGES", "true").lower() == "true"
PIP_TIMEOUT      = _int("PIP_TIMEOUT", 120)         # seconds per pip install
PIP_INDEX_URL    = os.environ.get("PIP_INDEX_URL", "")
PKG_RATE_MAX     = _int("PKG_RATE_MAX", 12)         # package operations per IP ...
PKG_RATE_WINDOW  = _int("PKG_RATE_WINDOW", 300)     # ... per this many seconds
ALLOWED_PACKAGES = [
    p
    for p in os.environ.get("PACKAGES_ALLOWLIST", "").replace(",", " ").split()
    if p
]
