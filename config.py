# ============================================================
#  Flask / Editor Configuration
# ============================================================

SECRET_KEY = "python-editor-secret-key-change-me"

DEBUG = True
HOST = "127.0.0.1"
PORT = 5000

# Limits for user code / input / output
MAX_CODE_SIZE   = 20000      # characters
MAX_INPUT_SIZE  = 10000      # characters
MAX_OUTPUT_SIZE = 50000      # characters (per stream)

# Execution sandbox limits
CODE_TIMEOUT   = 5                       # seconds (wall clock)
CPU_LIMIT      = 10                      # seconds (CPU time, Unix)
MEMORY_LIMIT   = 256 * 1024 * 1024       # 256 MB (Unix)