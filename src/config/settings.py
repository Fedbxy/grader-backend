"""Worker configuration, all from the environment.

Defaults match the current single-host deployment, so the worker runs with
nothing set but DATABASE_URL and the S3_* credentials.
"""

import os
import socket

DATABASE_URL = os.environ.get("DATABASE_URL", "")

S3_ENDPOINT = os.environ.get("S3_ENDPOINT", "localhost")
S3_PORT = os.environ.get("S3_PORT", "9001")
S3_ACCESS_KEY = os.environ.get("S3_ACCESS_KEY", "")
S3_SECRET_KEY = os.environ.get("S3_SECRET_KEY", "")
S3_BUCKET_NAME = os.environ.get("S3_BUCKET_NAME", "")
S3_USE_SSL = os.environ.get("S3_USE_SSL") == "true"

# The testcase volume. The cache of archives extracted from MinIO lives under
# {TESTCASE_ROOT}/.cache and is disposable: anything missing is refetched on
# demand (DESIGN.md section 3).
TESTCASE_ROOT = os.environ.get("TESTCASE_ROOT", "testcases")

# Concurrent submissions. Default 1 — raising this trades timing fidelity for
# throughput, and wants taskset pinning per lane (DESIGN.md section 4).
LANES = int(os.environ.get("LANES", "1"))

# Fallback poll interval. NOTIFY normally wakes the worker sooner; this is what
# makes correctness independent of notification delivery.
POLL_INTERVAL = float(os.environ.get("POLL_INTERVAL", "2.0"))

# A submission that kills the worker is retried this many times before being
# marked failed, so it cannot occupy the queue forever (DESIGN.md section 2).
MAX_ATTEMPTS = int(os.environ.get("MAX_ATTEMPTS", "3"))

WORKER_ID = os.environ.get("WORKER_ID", socket.gethostname())

NOTIFY_CHANNEL = "submission_queued"
