"""docker_healthcheck.py -- run by Docker every 30 seconds inside the container. Exit 0 = alive, 1 = not."""
import os
import sys
import urllib.request

try:
    with urllib.request.urlopen(f"http://127.0.0.1:{os.getenv('RAG_PORT', '8000')}/healthz", timeout=4) as r:
        sys.exit(0 if r.status == 200 else 1)
except Exception:
    sys.exit(1)
