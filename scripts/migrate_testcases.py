#!/usr/bin/env python3
"""One-time migration: push testcase directories from the judge volume into MinIO
and record Problem.testcaseVersion.

See DESIGN.md section 3 (testcase distribution) and section 7 step 2.

Dry run by default; pass --apply to upload and write to the database. Safe to
re-run: a problem whose archive already hashes to its stored testcaseVersion is
skipped, so a partial run can simply be repeated.

Environment: DATABASE_URL, S3_ENDPOINT, S3_PORT, S3_ACCESS_KEY, S3_SECRET_KEY,
S3_BUCKET_NAME, and optionally TESTCASE_ROOT (default /testcases).
"""

import hashlib
import io
import os
import re
import sys
import zipfile
from pathlib import Path

import psycopg
from minio import Minio

TESTCASE_ROOT = Path(os.environ.get("TESTCASE_ROOT", "/testcases"))

# __MACOSX is an artifact of zips built on macOS. Everything else is preserved
# as uploaded, including helper files such as transform.py, since only the
# author knows whether they matter.
SKIP_NAMES = {"__MACOSX"}

# A worker built before the cache moved to .cache/ extracted archives into
# {problem_id}/{sha256}/ directly beside the legacy files. Excluding those, and
# dot-entries (.ready, .DS_Store), keeps this script idempotent on a volume the
# worker has already run against.
VERSION_DIR = re.compile(r"^[0-9a-f]{64}$")


def _is_legacy_content(relative: Path) -> bool:
    parts = relative.parts
    return not any(
        part in SKIP_NAMES or part.startswith(".") or VERSION_DIR.match(part)
        for part in parts
    )


def build_archive(problem_dir: Path):
    """Deterministic zip of a problem directory: sorted members, stable mtimes.

    Determinism matters because the sha256 is the cache-invalidation token. The
    same directory must always produce the same version, or every run would
    invalidate every judge's cache.
    """
    members = sorted(
        p for p in problem_dir.rglob("*")
        if p.is_file() and _is_legacy_content(p.relative_to(problem_dir))
    )
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as archive:
        for path in members:
            archive.write(path, path.relative_to(problem_dir).as_posix())
    data = buf.getvalue()
    return data, hashlib.sha256(data).hexdigest(), len(members)


def validate(problem_dir: Path, expected_cases: int):
    """Every case the problem claims must exist as an .in/.sol pair."""
    faults = []
    for n in range(1, expected_cases + 1):
        for suffix in ("in", "sol"):
            if not (problem_dir / f"{n}.{suffix}").exists():
                faults.append(f"{n}.{suffix} missing")
    beyond = sorted(
        int(p.stem) for p in problem_dir.glob("*.in")
        if p.stem.isdigit() and int(p.stem) > expected_cases
    )
    if beyond:
        faults.append(f"cases beyond testcases={expected_cases}: {beyond}")
    return faults


def main() -> int:
    apply = "--apply" in sys.argv
    bucket = os.environ["S3_BUCKET_NAME"]

    client = Minio(
        f"{os.environ['S3_ENDPOINT']}:{os.environ['S3_PORT']}",
        access_key=os.environ["S3_ACCESS_KEY"],
        secret_key=os.environ["S3_SECRET_KEY"],
        secure=os.environ.get("S3_USE_SSL") == "true",
    )

    conn = psycopg.connect(os.environ["DATABASE_URL"])
    with conn.cursor() as cur:
        cur.execute('SELECT id, title, testcases, "testcaseVersion" FROM problems ORDER BY id')
        problems = cur.fetchall()

    on_disk = {int(p.name) for p in TESTCASE_ROOT.iterdir() if p.is_dir() and p.name.isdigit()}
    known = {row[0] for row in problems}

    print(f"{'APPLY' if apply else 'DRY RUN'} — root={TESTCASE_ROOT} bucket={bucket}\n")

    failed = uploaded = skipped = 0

    for problem_id, title, testcases, current_version in problems:
        directory = TESTCASE_ROOT / str(problem_id)
        label = f"problem {problem_id} ({title})"

        if not directory.is_dir():
            print(f"  FAIL {label}: no testcase directory — judge would report JE")
            failed += 1
            continue

        faults = validate(directory, testcases)
        if faults:
            print(f"  FAIL {label}: " + "; ".join(faults))
            failed += 1
            continue

        data, version, member_count = build_archive(directory)
        size_mb = len(data) / 1024 / 1024

        if current_version == version:
            print(f"  SKIP {label}: already at {version[:12]}")
            skipped += 1
            continue

        print(f"  {'PUSH' if apply else 'WOULD PUSH'} {label}: "
              f"{testcases} cases, {member_count} files, {size_mb:.1f} MB, {version[:12]}")

        if apply:
            if not client.bucket_exists(bucket):
                client.make_bucket(bucket)
            client.put_object(
                bucket, f"problem/{problem_id}/testcase.zip",
                io.BytesIO(data), len(data), content_type="application/zip",
            )
            with conn.cursor() as cur:
                cur.execute(
                    'UPDATE problems SET "testcaseVersion" = %s, "updatedAt" = now() WHERE id = %s',
                    (version, problem_id),
                )
            conn.commit()
        uploaded += 1

    orphans = sorted(on_disk - known)
    if orphans:
        print(f"\n  {len(orphans)} orphaned director{'y' if len(orphans) == 1 else 'ies'} "
              f"(no matching problem row): {orphans}")
        print("  Not touched. Safe to delete once testcases are served from MinIO,")
        print("  since the volume is then only a cache.")

    print(f"\n{uploaded} uploaded, {skipped} unchanged, {failed} failed")
    conn.close()
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
