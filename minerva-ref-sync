#!/usr/bin/env python3
"""minerva_ref_sync — sweep-gated code-only mirror of /home/lumen/minerva.

Builds /home/lumen/research/minerva-ref (a granted read root, fleet-exec-policy.yaml)
from the live minerva tree WITHOUT its credential surface, and refuses to publish any
mirror that carries a secret.

Two layers, both required (research/MINERVA_EVAL_GRANT_DECISION.md §5, Frank 2026-09-13):
  FILTER    — rsync exclusions (EXCLUDES below): context/, tests/, archival/, *.db, *.bak*
              and the rest of the deny basenames the capability lint refuses.
  GUARANTEE — after the staging copy is built, harness/read_dir_sweep.py runs on the
              RESULT. hits == 0 publishes (atomic rename swap); anything else ABORTS,
              leaves the previous good mirror untouched, and raises an aq alert naming
              the offending file. A sweep tool error is an abort too (fail closed).

Exit codes: 0 published / unchanged, 2 refused (hits), 3 sweep tool error, 4 rsync error.
"""
import argparse
import datetime as dt
import json
import os
import shutil
import subprocess
import sys
import tempfile

SRC = "/home/lumen/minerva"
DST = "/home/lumen/research/minerva-ref"
SWEEP = "/home/lumen/foreman/harness/read_dir_sweep.py"
AQ = "/home/lumen/bin/aq"
MANIFEST = ".minerva-ref-sync.json"

# The filter. Anchored dirs are relative to SRC. Keep this list boring and wide; the
# sweep below is what actually keeps secrets out.
EXCLUDES = [
    "/context/",        # credential sections mirror (wq#1467), 7 sweep hits 2026-09-13
    "/tests/",          # fixture sk- keys, 2 hits
    "/archival/",       # 1 hit
    "/archive/", "/backups/", "/logs/", "/staging/", "/retention/", "/gate-redteam/",
    "/.git/",           # history could carry anything the working tree no longer does
    "__pycache__/",
    "*.db", "*.db-*", "*.sqlite*",
    "*.bak*", "*.pyc", "*.hwm",
    "*.env", "*.token", "*.key", "*.pem", "id_*",   # capability_lint DIR_DENY_BASENAME_GLOBS
]

README = """minerva-ref — worker-readable, sweep-gated mirror of /home/lumen/minerva (code + eval only)

Produced by foreman ops/minerva-ref-sync/minerva_ref_sync.py on a timer. NOT a git checkout:
files are byte-identical copies of the live working tree at sync time; see {manifest} for the
source commit, timestamp, file count and sweep result. Excluded on purpose: context/, tests/,
archival/, archive/, backups/, logs/, staging/, retention/, gate-redteam/, .git/, *.db, *.bak*
and every deny basename. Every publish is preceded by harness/read_dir_sweep.py on the result;
a mirror with any hit is never published (decision: research/MINERVA_EVAL_GRANT_DECISION.md §5).

Edit the real tree, never this one — the next sync overwrites it.
"""


def log(msg):
    ts = dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    print(f"[minerva-ref-sync {ts}] {msg}", flush=True)


def git_head(src):
    try:
        return subprocess.run(["git", "-C", src, "rev-parse", "--short", "HEAD"],
                              capture_output=True, text=True, timeout=20).stdout.strip() or None
    except Exception:
        return None


def rsync_to(src, staging, dry=False):
    cmd = ["rsync", "-a", "--delete", "--delete-excluded"]
    for e in EXCLUDES:
        cmd += ["--exclude", e]
    if dry:
        cmd.append("-n")
    cmd += [src.rstrip("/") + "/", staging.rstrip("/") + "/"]
    r = subprocess.run(cmd, capture_output=True, text=True)
    if r.returncode != 0:
        raise RuntimeError(f"rsync rc={r.returncode}: {r.stderr.strip()[:400]}")


def sweep(path, sweep_py):
    """(files, hits:list) from read_dir_sweep.py --json. Raises on tool failure."""
    r = subprocess.run([sys.executable, sweep_py, path, "--json"],
                       capture_output=True, text=True, timeout=900)
    # rc 1 == hits present (still valid JSON); anything else is a tool failure.
    if r.returncode not in (0, 1):
        raise RuntimeError(f"sweep rc={r.returncode}: {r.stderr.strip()[:400]}")
    data = json.loads(r.stdout)
    entry = data.get(path) or next(iter(data.values()), {})
    hits = entry.get("hits") or []
    return int(entry.get("files", 0)), hits


def alert(key, title, msg, aq_bin):
    if not os.path.exists(aq_bin):
        log(f"aq not found at {aq_bin}; alert not raised: {title}")
        return
    try:
        subprocess.run([aq_bin, "add", "minerva-ref-sync", key, title, msg, "--owner", "orch"],
                       capture_output=True, text=True, timeout=30)
    except Exception as e:  # never let alerting mask the refusal
        log(f"aq add failed: {e}")


def count_files(path):
    n = 0
    for _, _, files in os.walk(path):
        n += len(files)
    return n


def publish(staging, dst, manifest):
    """Atomic-enough swap: rename old aside, rename staging in, drop old."""
    with open(os.path.join(staging, MANIFEST), "w") as fh:
        json.dump(manifest, fh, indent=1)
    with open(os.path.join(staging, "README.md"), "w") as fh:
        fh.write(README.format(manifest=MANIFEST))
    prev = dst.rstrip("/") + ".prev"
    if os.path.isdir(prev):
        shutil.rmtree(prev)
    if os.path.isdir(dst):
        os.rename(dst, prev)
    os.rename(staging, dst)
    if os.path.isdir(prev):
        shutil.rmtree(prev)


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--src", default=SRC)
    ap.add_argument("--dst", default=DST)
    ap.add_argument("--sweep", default=SWEEP)
    ap.add_argument("--aq", default=AQ)
    ap.add_argument("--dry-run", action="store_true", help="build + sweep staging, never publish")
    ap.add_argument("--json", action="store_true", help="print a result object on the last line")
    a = ap.parse_args()

    if not os.path.isdir(a.src):
        log(f"source {a.src} missing"); return 4
    parent = os.path.dirname(a.dst.rstrip("/"))
    os.makedirs(parent, exist_ok=True)
    staging = tempfile.mkdtemp(prefix=".minerva-ref.staging.", dir=parent)
    os.chmod(staging, 0o755)
    result = {"src": a.src, "dst": a.dst, "src_head": git_head(a.src),
              "ts": dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")}
    rc = 0
    try:
        try:
            rsync_to(a.src, staging)
        except RuntimeError as e:
            log(f"ABORT: {e}"); result.update(status="rsync_error", error=str(e)); rc = 4
            alert("rsync", "minerva-ref sync: rsync failed", str(e)[:300], a.aq)
            return rc
        try:
            files, hits = sweep(staging, a.sweep)
        except Exception as e:
            log(f"ABORT: sweep tool failed, refusing to publish unverified mirror: {e}")
            result.update(status="sweep_error", error=str(e)); rc = 3
            alert("sweep-error", "minerva-ref sync: sweep tool failed (fail closed)", str(e)[:300], a.aq)
            return rc
        result.update(files=files, hits=len(hits))
        if hits:
            # Name the files, never the matched text. Paths are relative to the mirror.
            named = sorted({str(h.get("path", h) if isinstance(h, dict) else h)
                            .replace(staging.rstrip("/") + "/", "") for h in hits})
            log(f"REFUSED: sweep found {len(hits)} hit(s) in staging; previous mirror left in place")
            for p in named[:20]:
                log(f"  hit: {p}")
            result.update(status="refused", hit_files=named)
            alert("gate", f"minerva-ref sync REFUSED: {len(hits)} credential hit(s)",
                  "files: " + ", ".join(named[:10]), a.aq)
            rc = 2
            return rc
        if a.dry_run:
            log(f"dry-run: staging swept clean ({files} files, 0 hits); not publishing")
            result.update(status="dry_run_clean"); return 0
        manifest = dict(result, status="published")
        publish(staging, a.dst, manifest)
        staging = None  # consumed by rename
        log(f"published {a.dst}: {files} files, 0 hits, src HEAD {result['src_head']}")
        result.update(status="published")
        return 0
    finally:
        if staging and os.path.isdir(staging):
            shutil.rmtree(staging, ignore_errors=True)
        if a.json:
            print(json.dumps(result))


if __name__ == "__main__":
    sys.exit(main())
