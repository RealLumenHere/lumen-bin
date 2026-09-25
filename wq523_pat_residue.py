#!/usr/bin/env python3
"""wq523_pat_residue.py — remediate credential residue. --dry-run by default.

NEVER prints a token value. Replaces each token literal in a NON-CANONICAL file with a
marker naming its sha256 fingerprint, so the file's other content survives and the
redaction is auditable without the secret.

CANONICAL homes are never touched: /home/lumen/agent/.env, /home/lumen/.git-credentials,
/home/lumen/.secrets/*.token  — those are where credentials are SUPPOSED to live.

  wq523_pat_residue.py            # dry run, prints the remediation plan
  wq523_pat_residue.py --apply    # do it (backs up everything it touches first)
  wq523_pat_residue.py --scan     # STANDING CHECK: exit 1 if any token literal lives
                                  # outside a canonical home. Safe on a timer; changes
                                  # nothing; never prints a value.

wq#523 deliverable 3 asked for the leaked-transcript path to be added to "the scrub
tooling's standing scan". THERE IS NO SUCH TOOLING — the 2026-08-10 scrub was ad hoc and
left only its backup directory behind. So --scan IS that standing check, and it covers the
Claude-transcript paths by construction rather than by an allowlist someone has to
remember to extend: it walks /home/lumen, /etc, /opt, /srv and reports anything holding a
token literal that is not one of the four canonical homes or a verified false positive.
"""
import os, re, sys, json, stat, shutil, hashlib, subprocess, datetime, fnmatch

# wq#2480: brought to parity with the token-engine broker's MASK_PATTERNS (ghp_/gho_/ghu_/
# ghs_/ghr_) and read_dir_sweep.py's full union (foreman/harness/read_dir_sweep.py:114-115,
# wq#2114/PR#897) which additionally covers glpat-. The three scanners disagreed on which
# prefixes count as a token; this is the redaction tool, so it stays narrow (still only the
# GH/GL PAT shapes, not read_dir_sweep's general secret classes like sk-/AKIA/slack).
PAT = re.compile(rb'ghp_[A-Za-z0-9]{30,}|github_pat_[A-Za-z0-9_]{30,}|gho_[A-Za-z0-9]{30,}|'
                  rb'ghs_[A-Za-z0-9]{30,}|ghr_[A-Za-z0-9]{30,}|ghu_[A-Za-z0-9]{30,}|'
                  rb'glpat-[A-Za-z0-9_-]{20,}')
APPLY = "--apply" in sys.argv
TS = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
# wq#1029: pre-redaction backups used to live at /home/lumen/backups/wq523_pre_redaction_*
# — UNDER a scanned root (/home/lumen, see ROOTS below). That copy still holds the live
# token by design (it's the pre-redaction original), so the very next --scan/--alert run
# flagged its own backup CRITICAL. Root-owned /var/backups/lumen is off every scanned root
# by construction (not just excluded by a prefix someone has to remember to keep in sync),
# mode 700, chowned to lumen so this script (runs unprivileged, wq#1009) can write it
# directly with no sudo in the hot path. One-time setup: `sudo chown lumen:lumen
# /var/backups/lumen` (already done 2026-08-27; directory pre-existed root-owned from a
# manual wq#1009 relocation).
BACKUP = f"/var/backups/lumen/wq523_pre_redaction_{TS}"

# wq#1009: BOUND THE SCAN. The old code fed `grep -rlI` over four roots and then did
# open(f,"rb").read() on every matched file — both unbounded. Roots hold multi-GB files
# (17.8G eMMC image, trivy DBs, .so, 463M SQL dumps), so a single large NUL-free file spiked
# the unit to 12.5G peak RSS. The fix is to STREAM: read_tokens() below reads any file in 1MB
# chunks at ~1MB peak regardless of file size, so memory no longer scales with the largest
# file — which means we do NOT have to drop coverage to bound memory. Logs and DB dumps (the
# 4 files a 64MB cap would have skipped) are exactly where a leaked token lands, so they stay
# in scope and are streamed in full. MAX_FILE_BYTES is now only a RUNTIME backstop against a
# pathological multi-GB single-line text file (2 GiB — larger than any real text file here;
# the GB-scale binaries are NUL-bearing and bail after the first chunk). Anything above it is
# reported, never silently dropped. Belt-and-braces: MemoryMax on the unit.
ROOTS = ("/home/lumen", "/etc", "/opt", "/srv")
PRUNE_DIRS = {"node_modules", ".git", "__pycache__", "worktrees"}   # wq#1642: worktrees
# added -- /home/lumen/worktrees and /home/lumen/scratch/worktrees are ephemeral fworker
# checkouts (test fixtures with token-shaped literals, e.g. worktrees/wq{869,879,913}-a*/
# .../tests/test_*.py, WORLD-READABLE only because a worktree is; nothing there is a
# canonical credential home). Pruning the dir name kills both the false CRITICALs and
# the 2m35s/run walk cost in one fix, same as node_modules/.git already do; a real leak
# committed from a worktree still lands on main and is caught there, so this isn't a gap.
MAX_FILE_BYTES = 2 * 1024 * 1024 * 1024   # 2 GiB runtime backstop (memory is bounded by CHUNK)
CHUNK = 1 << 20        # 1 MiB streaming read — sets the real per-file memory ceiling
OVERLAP = 4096         # > any real token, so a token straddling a chunk edge is still caught

CANONICAL = {
    "/home/lumen/agent/.env",
    "/home/lumen/.git-credentials",
}
# wq#565: the fleet convention is one credential per file under /home/lumen/.secrets, and
# this module's own docstring says "/home/lumen/.secrets/*.token" is canonical. The code
# said something narrower — two hardcoded filenames — so wq#524's github-frank.token was
# reported STRAY the moment it was created, in the correct location, by the convention.
# That is precisely the allowlist-someone-must-remember-to-extend failure the docstring
# claims this tool avoids BY CONSTRUCTION, sitting inside the tool. Glob the directory so
# the next correctly-placed secret does not have to be added by hand either.
CANONICAL_GLOBS = ("/home/lumen/.secrets/*.token",)


def canonical_set():
    import glob as _glob
    out = set(CANONICAL)
    for g in CANONICAL_GLOBS:
        out.update(_glob.glob(g))
    return out

# Verified false positives — a placeholder in vendored source, and a base64 fragment in a
# pip RECORD file that merely matches the regex. Redacting either would be vandalism.
FALSE_POSITIVE = {
    "/home/lumen/sim-studio-src/apps/sim/connectors/github/github.ts",   # ghp_xxxxxxxx… literal placeholder
    "/home/lumen/sandbox/wq298-neutts/venv/lib/python3.14/site-packages/"
    "vector_quantize_pytorch-1.17.8.dist-info/RECORD",                    # pip hash, not a credential
}
# wq#1096: gosec's own G101 hardcoded-credential linter ships FAKE sample tokens as test
# fixtures (e.g. testutils/g101_samples.go) inside the vendored, read-only go module cache —
# third-party linter source, never fleet credentials. A single exact path breaks the moment
# `go get -u` bumps the version in its dirname (.../gosec/v2@v2.26.1/...), so this is a glob
# on the vendored-fixture shape (any go module's testutils/ dir) rather than one gosec
# version pinned by hand — same allowlist-someone-must-remember-to-extend failure the
# CANONICAL_GLOBS comment above already called out, avoided the same way.
FALSE_POSITIVE_GLOBS = ("*/go/pkg/mod/*/testutils/*",)


# wq#1642: fake GH-token literals used as test fixtures for masking/audit logic (e.g.
# foreman/harness/tests/test_read_dir_sweep.py, foreman/ops/wq1302-n8n-eval/tests/
# test_cron_migration_audit.py). Fingerprinted rather than path-allowlisted on purpose: the
# SAME literal reappears in ephemeral copies a path glob can't anticipate (fworker
# foreman/_runs/*/snapshots/*, foreman/events/fworker-*.jsonl transcripts) every time a
# ticket re-runs these tests, and a fingerprint match still requires the exact fake string
# -- a real token embedded anywhere, including inside one of those same log files, gets a
# different fingerprint and is still reported. Do NOT rewrite the fixtures' literals to
# dodge the regex instead: they exist to exercise real masking code against a real-shaped
# token.
FALSE_POSITIVE_FINGERPRINTS = {
    # Each entry is sha256(token)[:12] for a KNOWN fake fixture literal used by
    # foreman/harness/tests/test_read_dir_sweep.py and foreman/ops/wq1302-n8n-eval/tests/
    # test_cron_migration_audit.py (github_pat_11ABC... placeholder, and 3 ghp_ variants:
    # an alphabet+six-digit form, an all-lowercase-alphabet+digits form, and an all-z form)
    # plus one further variant (ghp_ + full alphabet + full 0-9 digit run) observed only in
    # session/tool-call transcripts from past work on these same tickets (never in a
    # committed source file) -- same class, added when --apply's dry run surfaced it.
    # DELIBERATELY not commented with the literal strings themselves: this script scans
    # /home/lumen, and a literal here would make the tool flag its own source.
    "fac8b3c8a54a", "4528f0bd2a4c", "03aafb028d53", "9d6133be2daf", "fc25e1a8a307",
}


def is_false_positive(path, tokens=None):
    if path in FALSE_POSITIVE or any(fnmatch.fnmatch(path, g) for g in FALSE_POSITIVE_GLOBS):
        return True
    if tokens:
        fps = {hashlib.sha256(t).hexdigest()[:12] for t in tokens}
        if fps and fps <= FALSE_POSITIVE_FINGERPRINTS:
            return True
    return False


# Pure duplicates of the live credential file from the completed 2026-08-13 Gitea rotation.
# Deleting a duplicate of a value that still exists in its canonical home loses nothing;
# the only non-duplicate content is Gitea creds that the rotation itself superseded.
DELETE = [
    "/home/lumen/.git-credentials.bak.20260813_062236_pre_gitea_rotate",
    "/home/lumen/.git-credentials.bak.20260813_062629_pre_gitea_rotate",
    "/home/lumen/.git-credentials.bak.20260813_062929",
    "/home/lumen/.git-credentials.bak.20260813_063222_pre_secrets_fold",
    "/home/lumen/.git-credentials.broken.20260813_062430",
    # wq#1096: rotation-backup duplicate sitting next to its canonical home. --alert's own
    # live_fingerprints() check confirms its token's sha256 matches the CURRENT
    # /home/lumen/.secrets/github-lumen-classic.token — same "duplicate of a value that
    # still exists in its canonical home" case as the git-credentials backups above, so the
    # same remediation applies. --apply backs it up to /var/backups/lumen before removing it.
    "/home/lumen/.secrets/github-lumen-classic.token.bak.1787963768",
    # wq#1096: sweep of the 11 dead-token mode-600 backups that --alert has reported
    # "noted" (never CRITICAL) since the 2026-08-10 scrub. The EXCLUDE_PREFIX comment below
    # deferred deleting this backup-of-record to "Frank's call, not mine" — wq#1096 is that
    # call: tokens were already verified dead (HTTP 401), so the safety net has no remaining
    # value and is pure future-noise. Listed explicitly (not by prefix) so the deletion is
    # auditable file-by-file, same as every other DELETE entry.
    "/home/lumen/backups/pat_scrub_20260810_170237/_home_lumen_bets_interview-prep-uplift_frozen-ref_.git_config",
    "/home/lumen/backups/pat_scrub_20260810_170237/_home_lumen_bets_interview-prep-uplift_work_iprep-pr-api-scenarios_.git_config",
    "/home/lumen/backups/pat_scrub_20260810_170237/_home_lumen_bets_interview-prep-uplift_work_iprep-pr-autocomplete_.git_config",
    "/home/lumen/backups/pat_scrub_20260810_170237/_home_lumen_bets_interview-prep-uplift_work_iprep-pr-concurrency-parallel_.git_config",
    "/home/lumen/backups/pat_scrub_20260810_170237/_home_lumen_bets_interview-prep-uplift_work_iprep-pr-dependency-injection_.git_config",
    "/home/lumen/backups/pat_scrub_20260810_170237/_home_lumen_bets_interview-prep-uplift_work_iprep-pr-pagination_.git_config",
    "/home/lumen/backups/pat_scrub_20260810_170237/_home_lumen_bets_interview-prep-uplift_work_iprep-pr-security_.git_config",
    "/home/lumen/backups/pat_scrub_20260810_170237/_home_lumen_build_agent-orchestrator_.git_config",
    "/home/lumen/backups/pat_scrub_20260810_170237/_home_lumen_simpill-scratch-preserved_simpill-pr-middleware_.git_config",
    "/home/lumen/backups/pat_scrub_20260810_170237/_home_lumen_simpill-scratch-preserved_simpill-pr-zustand_.git_config",
    "/home/lumen/backups/pat_scrub_20260810_170237/chat_history.json",
]
TIGHTEN = {"/home/lumen/agent/MEMORY.bak.20260328_0821": 0o600,
           "/home/lumen/backups/pat_scrub_20260810_170237": 0o700}
# The 2026-08-10 scrub's OWN pre-scrub backup. EXCLUDED FROM REDACTION ON PURPOSE:
# redacting a backup-of-record makes it not a backup. Its tokens are dead (HTTP 401
# verified) and the directory is already mode 700, so it is not an exposure — what it is
# is a retention question, and deleting Frank's safety net is Frank's call, not mine.
# Reported in wq#523's report rather than actioned here.
EXCLUDE_PREFIX = ("/home/lumen/backups/pat_scrub_20260810_170237/",)


def read_tokens(path):
    """Stream a file in CHUNK blocks; return the set of token literals it holds. Peak ~1MB
    regardless of file size. A NUL in any block => binary => non-matching (grep -I semantics),
    returns empty. OVERLAP carry catches a token straddling a block boundary. The set is used
    only for fingerprinting — a token value is never returned to a caller that prints it."""
    toks = set()
    tail = b""
    read = 0
    try:
        with open(path, "rb") as fh:
            try:
                while read < MAX_FILE_BYTES:
                    chunk = fh.read(CHUNK)
                    if not chunk:
                        break
                    read += len(chunk)
                    if b"\x00" in chunk:
                        return set()        # binary: grep -I treats as non-matching
                    window = tail + chunk
                    for m in PAT.finditer(window):
                        toks.add(m.group(0))
                    tail = window[-OVERLAP:]
                    try:                    # drop already-scanned pages each chunk so the
                        os.posix_fadvise(   # cgroup's file-cache charge stays ~one chunk,
                            fh.fileno(), 0, read, os.POSIX_FADV_DONTNEED)  # not whole-file
                    except OSError:
                        pass
            finally:
                # Drop these one-shot reads from the page cache. Streaming already bounds
                # anonymous RSS to ~1MB, but read() still populates the kernel page cache,
                # which cgroup v2 charges to this unit's memory.current/MemoryPeak. Scanning
                # the SQL dumps and logs would otherwise pin ~MemoryHigh in reclaimable cache
                # and evict useful cache host-wide. fadvise(DONTNEED) releases it now so the
                # unit's memory footprint reflects the real working set. (wq#1009)
                try:
                    os.posix_fadvise(fh.fileno(), 0, 0, os.POSIX_FADV_DONTNEED)
                except OSError:
                    pass
    except OSError:
        return set()
    return toks


def build_index(roots=ROOTS):
    """Bounded walk of roots. Returns (index, oversize):
      index    = {path: set(token_bytes)} for every regular file <= cap holding >=1 token
      oversize = sorted paths OVER the cap whose head is NUL-free (text-ish) — files the old
                 grep would have scanned; surfaced so a size skip is never silent. Over-cap
                 binaries are omitted (old grep -I skipped them too — not a coverage gap).
    Correctness rests on the per-file size+binary+content filters; the dir prune equals the
    old grep --exclude-dir set exactly, so the candidate set is otherwise unchanged. grep -r
    did not follow symlinks, so neither do we."""
    index, oversize = {}, []
    for root in roots:
        for dirpath, dirnames, filenames in os.walk(root, onerror=lambda e: None):
            dirnames[:] = [d for d in dirnames if d not in PRUNE_DIRS]
            for name in filenames:
                p = os.path.join(dirpath, name)
                if os.path.islink(p):
                    continue
                try:
                    st = os.stat(p)
                except OSError:
                    continue
                if not stat.S_ISREG(st.st_mode):
                    continue
                if st.st_size > MAX_FILE_BYTES:
                    try:
                        with open(p, "rb") as fh:
                            if b"\x00" not in fh.read(CHUNK):
                                oversize.append(p)   # text-ish + over cap = real coverage gap
                    except OSError:
                        pass
                    continue
                toks = read_tokens(p)
                if toks:
                    index[p] = toks
    return index, sorted(oversize)


def scan():
    """Sorted list of files under ROOTS holding >=1 token literal (bounded, streaming)."""
    index, _ = build_index()
    return sorted(index)


def cmd_scan():
    index, _ = build_index()
    files = sorted(index)
    canonical = canonical_set()
    stray = [f for f in files if f not in canonical and not is_false_positive(f, index[f])]
    exposed = [f for f in stray if os.stat(f).st_mode & 0o007]
    fp_count = len([f for f in files if is_false_positive(f, index[f])])
    print(f"pat-scan: {len(files)} file(s) hold a token literal; "
          f"{len(canonical & set(files))} canonical, {fp_count} "
          f"known false positive(s), {len(stray)} STRAY")
    for f in stray:
        mode = oct(os.stat(f).st_mode)[-3:]
        print(f"  STRAY {mode} {f}" + ("   <== WORLD-READABLE" if f in exposed else ""))
    if not stray:
        print("  clean — every token literal is in a canonical home")
    return 1 if stray else 0


def live_fingerprints():
    """sha256 prefixes of the tokens currently in a canonical home — i.e. the values that
    would still authenticate today. Never returns a token, only its fingerprint."""
    live = set()
    for f in sorted(canonical_set()):
        for t in read_tokens(f):            # bounded/streaming, same as everywhere else
            live.add(hashlib.sha256(t).hexdigest()[:12])
    return live


def cmd_alert():
    """STANDING CHECK for the timer (wq#565).

    --scan is the full inventory and exits 1 on ANY stray, which on a daily timer is 29
    lines of known backup residue every morning — an alert that fires every day is one
    nobody reads. But the ticket is explicit that simply excluding backup dirs is wrong: a
    LIVE credential in a backup is exactly what wq#523 found. So severity is a property of
    the file, not of its directory:

      CRITICAL  world-readable (any mode bit in 0o007), or holds a token that STILL
                authenticates (fingerprint matches one in a canonical home)
      noted     everything else — counted, never suppressed, but does not alert

    Exit 1 only on CRITICAL. Never prints a token value.
    """
    index, oversize = build_index()
    files = sorted(index)
    canonical = canonical_set()
    live = live_fingerprints()
    stray = [f for f in files if f not in canonical and not is_false_positive(f, index[f])]

    critical, noted = [], []
    for f in stray:
        try:
            st = os.stat(f)
        except OSError:
            continue
        world = bool(st.st_mode & 0o007)
        fps = {hashlib.sha256(t).hexdigest()[:12] for t in index[f]}
        hot = fps & live
        why = []
        if world:
            why.append("WORLD-READABLE")
        if hot:
            why.append("LIVE token " + ",".join(sorted(hot)))
        (critical if why else noted).append((f, oct(st.st_mode)[-3:], "; ".join(why)))

    print(f"pat-alert: {len(stray)} stray file(s) — {len(critical)} CRITICAL, "
          f"{len(noted)} noted (dead tokens in mode-600 backups)")
    for f, mode, why in critical:
        print(f"  CRITICAL {mode} {f}  [{why}]")
    if critical:
        # wq#2572: self-alert independent of systemd state -- see DESIGN.md in
        # ops/wq2572-guard-crash-vs-finding for why this can no longer rely on
        # OnFailure=/lumen-failed-units once this unit moves to SuccessExitStatus=1.
        _key = hashlib.sha256(",".join(sorted(f for f, _, _ in critical)).encode()).hexdigest()[:16]
        _title = f"pat-scan: {len(critical)} CRITICAL token residue file(s)"
        _detail = "; ".join(f"{f} [{why}]" for f, _, why in critical)[:1500]
        try:
            subprocess.run(["/home/lumen/bin/aq", "add", "pat-scan", _key, _title, _detail,
                            "--owner", "claude"], capture_output=True, text=True, timeout=20)
        except Exception:
            pass
        try:
            subprocess.run(["/home/lumen/bin/ntfy-send", "-t", "lumen-alerts", "-p", "high",
                            "-T", _title, _detail], capture_output=True, text=True, timeout=15)
        except Exception:
            pass
    if not critical:
        print("  no stray file is world-readable or holds a live token")
    if oversize:
        print(f"  note: {len(oversize)} text file(s) over the {MAX_FILE_BYTES>>20}MB cap NOT "
              f"scanned (bounded for memory; listed so the skip is visible):")
        for f in oversize:
            print(f"    OVERSIZE {f}")
    return 1 if critical else 0


def main():
    # wq#2572: an unhandled exception here used to fall through to Python's default
    # exit code 1 -- IDENTICAL to cmd_alert()'s "found a CRITICAL" exit 1, so a crash
    # and a real finding were indistinguishable from the outside. Give a crash its
    # own code so SuccessExitStatus=1 (finding only) never also swallows a real
    # failure. See ops/wq2572-guard-crash-vs-finding/DESIGN.md.
    try:
        if "--scan" in sys.argv:
            sys.exit(cmd_scan())
        if "--alert" in sys.argv:
            sys.exit(cmd_alert())
    except SystemExit:
        raise
    except Exception as e:
        print(f"wq523_pat_residue: internal error: {e}", file=sys.stderr)
        sys.exit(2)
    index, oversize = build_index()
    files = sorted(index)
    canonical = canonical_set()
    todo = [f for f in files
            if f not in canonical and not is_false_positive(f, index[f]) and f not in DELETE
            and not f.startswith(EXCLUDE_PREFIX)]
    excluded = [f for f in files if f.startswith(EXCLUDE_PREFIX)]
    print(f"{'APPLY' if APPLY else 'DRY RUN'} — {len(files)} file(s) carry a token literal\n")
    if oversize:
        print(f"  {len(oversize)} text file(s) over the {MAX_FILE_BYTES>>20}MB cap NOT scanned "
              f"(bounded for memory): " + ", ".join(oversize))
    print(f"  {len(canonical & set(files))} canonical (untouched)")
    print(f"  {len([f for f in files if is_false_positive(f, index[f])])} verified false positives (untouched)")
    print(f"  {len([f for f in DELETE if os.path.exists(f)])} duplicate credential files -> DELETE")
    print(f"  {len(excluded)} in the 08-10 scrub backup-of-record (untouched; dead tokens, mode 700 — retention is Frank's call)")
    print(f"  {len(todo)} residue files -> REDACT in place\n")

    if APPLY:
        # wq#1029: defensive — refuse to ever write the pre-redaction backup (which holds
        # the live token by design) under a path this same tool scans, no matter how BACKUP
        # gets defined in the future. Cheaper to assert than to re-discover the trap.
        assert not any(BACKUP == r or BACKUP.startswith(r.rstrip("/") + "/") for r in ROOTS), \
            f"BACKUP {BACKUP!r} is under a scanned root {ROOTS} — would re-flag itself CRITICAL"
        os.makedirs(BACKUP, mode=0o700, exist_ok=True)

    manifest = []
    for f in todo + [f for f in DELETE if os.path.exists(f)]:
        try:
            with open(f, "rb") as _fh:
                data = _fh.read()
                try:
                    os.posix_fadvise(_fh.fileno(), 0, 0, os.POSIX_FADV_DONTNEED)
                except OSError:
                    pass
        except OSError as e:
            print(f"  !! unreadable, skipped: {f} ({e})")
            continue
        toks = set(PAT.findall(data))
        action = "DELETE" if f in DELETE else "REDACT"
        fps = ",".join(sorted(hashlib.sha256(t).hexdigest()[:12] for t in toks))
        mode = oct(os.stat(f).st_mode)[-3:]
        print(f"  {action:6} {mode}  {f}  [{fps}]")
        manifest.append({"path": f, "action": action, "mode": mode,
                         "fingerprints": sorted(hashlib.sha256(t).hexdigest()[:12] for t in toks)})
        if not APPLY:
            continue
        dest = os.path.join(BACKUP, f.lstrip("/").replace("/", "_"))
        shutil.copy2(f, dest)
        os.chmod(dest, 0o600)
        if action == "DELETE":
            os.remove(f)
        else:
            new = data
            for t in toks:
                fp = hashlib.sha256(t).hexdigest()[:12]
                new = new.replace(t, f"<REDACTED-wq523 sha256:{fp}>".encode())
            st = os.stat(f)
            open(f, "wb").write(new)
            os.chmod(f, st.st_mode & 0o777)

    for path, mode in TIGHTEN.items():
        if not os.path.exists(path):
            continue
        cur = os.stat(path).st_mode & 0o777
        if cur == mode:
            print(f"  ok     {oct(cur)[-3:]} already correct: {path}")
            continue
        print(f"  CHMOD  {oct(cur)[-3:]} -> {oct(mode)[-3:]}  {path}")
        manifest.append({"path": path, "action": "CHMOD",
                         "from": oct(cur)[-3:], "to": oct(mode)[-3:]})
        if APPLY:
            os.chmod(path, mode)

    if APPLY:
        with open(os.path.join(BACKUP, "MANIFEST.json"), "w") as fh:
            json.dump(manifest, fh, indent=2)
        os.chmod(os.path.join(BACKUP, "MANIFEST.json"), 0o600)
        print(f"\nbackups + manifest: {BACKUP} (mode 700)")
    print("\nre-run the repro grep afterwards to confirm the count dropped.")


if __name__ == "__main__":
    main()
