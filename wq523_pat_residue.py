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
import os, re, sys, json, shutil, hashlib, subprocess, datetime

PAT = re.compile(rb'ghp_[A-Za-z0-9]{30,}|github_pat_[A-Za-z0-9_]{30,}|gho_[A-Za-z0-9]{30,}')
APPLY = "--apply" in sys.argv
TS = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
BACKUP = f"/home/lumen/backups/wq523_pre_redaction_{TS}"

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
# Pure duplicates of the live credential file from the completed 2026-08-13 Gitea rotation.
# Deleting a duplicate of a value that still exists in its canonical home loses nothing;
# the only non-duplicate content is Gitea creds that the rotation itself superseded.
DELETE = [
    "/home/lumen/.git-credentials.bak.20260813_062236_pre_gitea_rotate",
    "/home/lumen/.git-credentials.bak.20260813_062629_pre_gitea_rotate",
    "/home/lumen/.git-credentials.bak.20260813_062929",
    "/home/lumen/.git-credentials.bak.20260813_063222_pre_secrets_fold",
    "/home/lumen/.git-credentials.broken.20260813_062430",
]
TIGHTEN = {"/home/lumen/agent/MEMORY.bak.20260328_0821": 0o600,
           "/home/lumen/backups/pat_scrub_20260810_170237": 0o700}
# The 2026-08-10 scrub's OWN pre-scrub backup. EXCLUDED FROM REDACTION ON PURPOSE:
# redacting a backup-of-record makes it not a backup. Its tokens are dead (HTTP 401
# verified) and the directory is already mode 700, so it is not an exposure — what it is
# is a retention question, and deleting Frank's safety net is Frank's call, not mine.
# Reported in wq#523's report rather than actioned here.
EXCLUDE_PREFIX = ("/home/lumen/backups/pat_scrub_20260810_170237/",)


def scan():
    out = subprocess.run(["bash", "-c",
        "grep -rlIE 'ghp_[A-Za-z0-9]{30,}|github_pat_[A-Za-z0-9_]{30,}|gho_[A-Za-z0-9]{30,}' "
        "/home/lumen /etc /opt /srv --exclude-dir=node_modules --exclude-dir=.git "
        "--exclude-dir=__pycache__ 2>/dev/null"], capture_output=True, text=True)
    return sorted(f for f in out.stdout.split() if f)


def cmd_scan():
    files = scan()
    canonical = canonical_set()
    stray = [f for f in files if f not in canonical and f not in FALSE_POSITIVE]
    exposed = [f for f in stray if os.stat(f).st_mode & 0o007]
    print(f"pat-scan: {len(files)} file(s) hold a token literal; "
          f"{len(canonical & set(files))} canonical, {len(FALSE_POSITIVE & set(files))} "
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
        try:
            blob = open(f, "rb").read()
        except OSError:
            continue
        for m in PAT.finditer(blob):
            live.add(hashlib.sha256(m.group(0)).hexdigest()[:12])
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
    files = scan()
    canonical = canonical_set()
    live = live_fingerprints()
    stray = [f for f in files if f not in canonical and f not in FALSE_POSITIVE]

    critical, noted = [], []
    for f in stray:
        try:
            st = os.stat(f)
            blob = open(f, "rb").read()
        except OSError:
            continue
        world = bool(st.st_mode & 0o007)
        fps = {hashlib.sha256(m.group(0)).hexdigest()[:12] for m in PAT.finditer(blob)}
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
    if not critical:
        print("  no stray file is world-readable or holds a live token")
    return 1 if critical else 0


def main():
    if "--scan" in sys.argv:
        sys.exit(cmd_scan())
    if "--alert" in sys.argv:
        sys.exit(cmd_alert())
    files = scan()
    canonical = canonical_set()
    todo = [f for f in files
            if f not in canonical and f not in FALSE_POSITIVE and f not in DELETE
            and not f.startswith(EXCLUDE_PREFIX)]
    excluded = [f for f in files if f.startswith(EXCLUDE_PREFIX)]
    print(f"{'APPLY' if APPLY else 'DRY RUN'} — {len(files)} file(s) carry a token literal\n")
    print(f"  {len(canonical & set(files))} canonical (untouched)")
    print(f"  {len(FALSE_POSITIVE & set(files))} verified false positives (untouched)")
    print(f"  {len([f for f in DELETE if os.path.exists(f)])} duplicate credential files -> DELETE")
    print(f"  {len(excluded)} in the 08-10 scrub backup-of-record (untouched; dead tokens, mode 700 — retention is Frank's call)")
    print(f"  {len(todo)} residue files -> REDACT in place\n")

    if APPLY:
        os.makedirs(BACKUP, mode=0o700, exist_ok=True)

    manifest = []
    for f in todo + [f for f in DELETE if os.path.exists(f)]:
        try:
            data = open(f, "rb").read()
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
