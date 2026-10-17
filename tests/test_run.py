"""Runs (#67): verdict mapping, output capping, CE short-circuit, claim order.

isolate is stubbed at subprocess.Popen, so run_once's real command building, meta
parsing and output reading are exercised. Run from a writable directory:

    docker run --rm -w /tmp -v $PWD:/app:ro grader-backend:main python /app/tests/test_run.py
"""
import os, sys, tempfile
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
import judge
from config import settings

FAILED = []
def check(cond, msg):
    print(f"  {'PASS' if cond else 'FAIL'}  {msg}")
    if not cond: FAILED.append(msg)

# run_once keeps its meta and stdin files under ./tmp, relative to the cwd.
os.chdir(tempfile.mkdtemp())
BOX = tempfile.mkdtemp()
CMDS = []
PROGRESS = []

def fake_isolate(meta, out=b"", err=b""):
    """A Popen stand-in: writes what isolate would have, records the command."""
    class FakePopen:
        def __init__(self, cmd, **kw):
            CMDS.append((cmd, kw["stdin"].read()))
            arg = lambda name: next(c.split("=", 1)[1] for c in cmd if c.startswith(f"--{name}="))
            with open(arg("meta"), "w") as f:
                f.write("".join(f"{k}:{v}\n" for k, v in meta.items()))
            open(f"{BOX}/{arg('stdout')}", "wb").write(out)
            open(f"{BOX}/{arg('stderr')}", "wb").write(err)
        def communicate(self, timeout=None):
            return "", ""
        def kill(self):
            pass
    judge.subprocess.Popen = FakePopen

def run(meta, out=b"", err=b"", stdin="1 2\n", lang="cpp", tl=1000, ml=32):
    judge.compile = lambda box, language: None
    fake_isolate(meta, out, err)
    CMDS.clear(); PROGRESS.clear()
    return judge.run_once(BOX, 7, stdin, tl, ml, lang, onProgress=PROGRESS.append)

OK = {"time": "0.123", "max-rss": "2048"}

print("clean exit:")
r = run(OK, out=b"3\n", err=b"warn\n")
check(r.verdict == "OK" and r.errorCode is None, f"verdict OK (got {r.verdict})")
check(r.stdout == "3\n" and r.stderr == "warn\n", "stdout and stderr captured separately")
check(abs(r.time - 123.0) < 1e-6 and r.memory == 2048.0, f"time in ms, memory in KB ({r.time}, {r.memory})")
check(r.exitCode is None and r.exitSignal is None, "no exit fields on OK")
check(PROGRESS == ["Compiling", "Running"], f"progress {PROGRESS}")
check(CMDS[0][1] == "1 2\n", "the input reached the program as stdin")
cmd = CMDS[0][0]
check("--wall-time=6.0" in cmd and f"--fsize={settings.OUTPUT_LIMIT_KB}" in cmd and "--time=1.0" in cmd,
      "same time/wall-time/fsize flags as grading")
check(not os.path.exists("tmp"), "stdin and meta files cleaned up, tmp dir removed")

print("\ncommand is shared with execute():")
cmd = judge.isolateRunCommand(3, "tmp/3.meta", "3.output", "3.error", 4000, 64, "py")
check(cmd == ["isolate", "--box-id=3", "--meta=tmp/3.meta", "--stdout=3.output", "--stderr=3.error",
              "--time=4.0", "--wall-time=9.0", f"--mem={64 * 1024}", f"--fsize={settings.OUTPUT_LIMIT_KB}",
              "--run", "--", "/usr/local/bin/python3", "-B", "3.py"], f"unchanged command layout: {cmd}")

print("\nlanguage multipliers apply:")
run(OK, lang="py")
check("--time=4.0" in CMDS[0][0], "python gets 4x the time limit")
run(OK, lang="pypy")
check(f"--mem={32 * 2 * 1024}" in CMDS[0][0], "pypy gets 2x the memory limit")

print("\nverdict mapping:")
r = run({**OK, "status": "TO"})
check(r.verdict == "TLE" and r.time == 1000, f"TLE reports the limit as time ({r.verdict}, {r.time})")
r = run({**OK, "status": "RE", "exitsig": "6"})
check(r.verdict == "MLE" and r.memory == 32 * 1024, f"exitsig 6 is MLE with the limit as memory ({r.verdict}, {r.memory})")
r = run({**OK, "status": "RE", "exitcode": "3"})
check(r.verdict == "RE" and r.exitCode == 3 and r.exitSignal is None, f"RE with exit code ({r.verdict}, {r.exitCode}, {r.exitSignal})")
r = run({**OK, "status": "SG", "exitsig": "11"})
check(r.verdict == "RE" and r.exitSignal == 11 and r.exitCode is None, f"SG with signal ({r.verdict}, {r.exitCode}, {r.exitSignal})")
r = run({**OK, "status": "TO", "exitsig": "6"})
check(r.verdict == "TLE", "TO outranks exitsig 6, as in execute()")
try:
    run({**OK, "status": "XX"})
    check(False, "XX raises")
except Exception as e:
    check("Isolate failed" in str(e), f"XX raises a judge failure ({e})")
check(not os.path.exists("tmp"), "files cleaned up even when isolate failed")

print("\noutput capping:")
cap = settings.RUN_OUTPUT_LIMIT_BYTES
check(cap == 64 * 1024, "default cap is 64 KiB")
r = run(OK, out=b"a" * cap)
check(len(r.stdout) == cap and not r.stdoutTruncated, "exactly at the cap is not truncated")
r = run(OK, out=b"a" * (cap + 1), err=b"b" * (cap * 3))
check(len(r.stdout) == cap and r.stdoutTruncated, f"one byte over truncates ({len(r.stdout)}, {r.stdoutTruncated})")
check(len(r.stderr) == cap and r.stderrTruncated, "stderr is capped independently")
r = run(OK, out=b"a" * (cap + 1))
check(not r.stderrTruncated, "stderr flag stays false when stderr was small")

reads = []
real_open = open
def spy_open(path, mode="r", *a, **kw):
    f = real_open(path, mode, *a, **kw)
    if str(path).endswith(".output") and "b" in mode and "r" in mode:
        real_read = f.read
        class Spy:
            def __enter__(s): return s
            def __exit__(s, *x): f.close()
            def read(s, n=-1):
                reads.append(n); return real_read(n)
        return Spy()
    return f
import builtins; builtins.open = spy_open
try:
    run(OK, out=b"a" * (cap * 4))
finally:
    builtins.open = real_open
check(reads == [cap + 1], f"the file is read as a bounded head, never whole (read sizes {reads})")

print("\nencoding:")
r = run(OK, out=b"ab\x00cd\x00\n", err=b"\x00\x00")
check(r.stdout == "abcd\n" and r.stderr == "", f"NUL bytes stripped ({r.stdout!r}, {r.stderr!r})")
r = run(OK, out=b"ok \xff\xfe bytes")
check(r.stdout == "ok �� bytes", f"invalid UTF-8 replaced ({r.stdout!r})")
r = run(OK, out="é".encode() * (cap // 2 + 1))
check(r.stdoutTruncated and len(r.stdout.encode()) <= cap + 2, "a multibyte char split at the cap still decodes")

print("\ncompile error short-circuits:")
judge.compile = lambda box, language: "main.cpp:1: error: oops"
judge.subprocess.Popen = lambda *a, **k: (_ for _ in ()).throw(AssertionError("executed after CE"))
PROGRESS.clear()
r = judge.run_once(BOX, 7, "x", 1000, 32, "cpp", onProgress=PROGRESS.append)
check(r.errorCode == "CE" and r.error == "main.cpp:1: error: oops", f"CE with the compiler output ({r.errorCode})")
check(r.verdict is None and PROGRESS == ["Compiling"], f"nothing ran after CE (progress {PROGRESS})")

print("\nlane claim order (needs psycopg):")
try:
    import worker, jobs
except ImportError as e:
    print(f"  SKIP  worker not importable here ({e})")
else:
    from contextlib import contextmanager
    class Pool:
        @contextmanager
        def connection(self): yield object()
    worker.db.pool = Pool()
    import logging; logging.disable(logging.CRITICAL)  # the lane logs the deliberate failure below

    def drive(submissions, runs):
        """Run lane_loop until both queues drain; returns the order handled."""
        order = []
        jobs.claim = lambda conn: submissions.pop(0) if submissions else None
        jobs.claim_run = lambda conn: runs.pop(0) if runs else None
        def handle(lane, job):
            order.append(("sub", job.id))
            if not submissions and not runs: worker.stop.set()
        def handle_run(lane, job):
            order.append(("run", job.id))
            if not submissions and not runs: worker.stop.set()
        worker.handle, worker.handle_run = handle, handle_run
        worker.stop.clear()
        worker.settings.POLL_INTERVAL = 0.01
        worker.lane_loop(0)
        return order

    sub = lambda i: jobs.Job(i, "", "cpp", 1, 1, 1000, 32, 1, None)
    run_ = lambda i: jobs.RunJob(i, "", "cpp", "", 1, 1, 1000, 32)
    order = drive([sub(10)], [run_(1)])
    check(order == [("sub", 10), ("run", 1)], f"a pending submission beats an older run {order}")
    order = drive([sub(10), sub(11)], [run_(1), run_(2)])
    check(order == [("sub", 10), ("sub", 11), ("run", 1), ("run", 2)], f"runs only after the submissions drain {order}")

    print("\nstranded hand-back releases the right table:")
    released = []
    jobs.release = lambda conn, i: released.append(("sub", i))
    jobs.release_run = lambda conn, i: released.append(("run", i))
    calls = {"n": 0}
    def flaky_claim_run(conn):
        calls["n"] += 1
        if calls["n"] == 1: return run_(5)
        worker.stop.set(); return None
    jobs.claim = lambda conn: None
    jobs.claim_run = flaky_claim_run
    def boom(lane, job): raise RuntimeError("database unavailable")
    worker.handle_run = boom
    worker.stop.clear()
    worker.lane_loop(0)
    check(released == [("run", 5)], f"a stranded run is released via release_run {released}")

print("\nFAILURES:", FAILED if FAILED else "none")
sys.exit(1 if FAILED else 0)
