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
    "/home/lumen/.secrets/github-lumen.token",
    "/home/lumen/.secrets/github-lumen-classic.token",
}
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
    stray = [f for f in files if f not in CANONICAL and f not in FALSE_POSITIVE]
    exposed = [f for f in stray if os.stat(f).st_mode & 0o007]
    print(f"pat-scan: {len(files)} file(s) hold a token literal; "
          f"{len(CANONICAL & set(files))} canonical, {len(FALSE_POSITIVE & set(files))} "
          f"known false positive(s), {len(stray)} STRAY")
    for f in stray:
        mode = oct(os.stat(f).st_mode)[-3:]
        print(f"  STRAY {mode} {f}" + ("   <== WORLD-READABLE" if f in exposed else ""))
    if not stray:
        print("  clean — every token literal is in a canonical home")
    return 1 if stray else 0


def main():
    if "--scan" in sys.argv:
        sys.exit(cmd_scan())
    files = scan()
    todo = [f for f in files
            if f not in CANONICAL and f not in FALSE_POSITIVE and f not in DELETE
            and not f.startswith(EXCLUDE_PREFIX)]
    excluded = [f for f in files if f.startswith(EXCLUDE_PREFIX)]
    print(f"{'APPLY' if APPLY else 'DRY RUN'} — {len(files)} file(s) carry a token literal\n")
    print(f"  {len(CANONICAL & set(files))} canonical (untouched)")
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
