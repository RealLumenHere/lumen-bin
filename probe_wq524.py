#!/usr/bin/env python3
"""probe_wq524 — red/green for folding the live PAT behind /home/lumen/.secrets.

The contract: resolve_github_token walks THREE rungs — per-repo identity-map entry,
then the default /home/lumen/.secrets/github-frank.token, then the DEPRECATED
GITHUB_TOKEN in /home/lumen/agent/.env (which warns). Every rung falls THROUGH on
missing/unreadable/empty rather than failing, so moving a secret can never be a
fleet-wide auth outage on its own.

Section D is the one that earns its keep: removing `. /home/lumen/agent/.env` from
lumen-pr's Gitea branch also removed GITEA_URL, which that sourcing had been silently
supplying. FORGE_HOST drops the port by design, so the API base became port 80 and
every Gitea call returned HTTP 000. D asserts the port survives.

Usage: probe_wq524.py [DIR]   where DIR holds the lumen-pr / identity script under test
"""
import os, sys, subprocess, tempfile, shutil, stat

D = os.path.abspath(sys.argv[1]) if len(sys.argv) > 1 else os.path.dirname(os.path.abspath(__file__))
IDENT = os.path.join(D, "lumen-github-identity.sh")
LPR = os.path.join(D, "lumen-pr")
TMP = tempfile.mkdtemp(prefix="wq524-")
PASS, FAIL = [], []

REAL_TOK = "ghp_" + "R" * 36      # fixtures only — never a live credential
MAP_TOK = "ghp_" + "M" * 36
ENV_TOK = "ghp_" + "E" * 36


def check(cid, ok, detail=""):
    (PASS if ok else FAIL).append(cid)
    print(("  ok   " if ok else "  FAIL ") + cid + ("  " + detail if detail else ""))


def wr(name, body, mode=0o600):
    p = os.path.join(TMP, name)
    with open(p, "w") as f:
        f.write(body)
    os.chmod(p, mode)
    return p


def resolve(slug, mapf=None, deff=None, envf=None):
    """Source the identity script and resolve one slug. Returns (rc, source, token, stderr)."""
    script = (
        f'. "{IDENT}" 2>/dev/null || exit 90\n'
        f'resolve_github_token "{slug}"; rc=$?\n'
        f'printf "RC=%s\\nSRC=%s\\nTOK=%s\\n" "$rc" "${{TOKEN_SOURCE:-}}" "${{GITHUB_TOKEN:-}}"\n'
    )
    e = dict(os.environ)
    for k in ("GITHUB_IDENTITY_MAP", "GITHUB_DEFAULT_TOKEN_FILE", "GITHUB_ENV_FILE"):
        e.pop(k, None)
    if mapf is not None:
        e["GITHUB_IDENTITY_MAP"] = mapf
    if deff is not None:
        e["GITHUB_DEFAULT_TOKEN_FILE"] = deff
    if envf is not None:
        e["GITHUB_ENV_FILE"] = envf
    p = subprocess.run(["bash", "-c", script], capture_output=True, text=True, env=e, timeout=60)
    out = {}
    for line in (p.stdout or "").splitlines():
        if "=" in line:
            k, _, v = line.partition("=")
            out[k] = v
    return out.get("RC", "?"), out.get("SRC", ""), out.get("TOK", ""), (p.stderr or "")


print(f"probe_wq524 — under test: {D}\n")

SLUG = "frankstupak/lumen-plays-pokemon"
mapped = wr("mapped.token", MAP_TOK + "\n")
default = wr("github-frank.token", REAL_TOK + "\n")
envfile = wr("agent.env", f"GROQ_API_KEY=gsk_x\nGITHUB_TOKEN={ENV_TOK}\nGITEA_TOKEN=abc\n")
mapf = wr("identity-map.conf", f"# comment\n{SLUG} = {mapped}\n", 0o644)

print("A. the resolution ladder: map -> .secrets default -> agent/.env")
rc, src, tok, err = resolve(SLUG, mapf, default, envfile)
check("A1", rc == "0" and tok == MAP_TOK and src == mapped,
      "a per-repo identity-map entry still wins outright (wq#525 not regressed)")

rc, src, tok, err = resolve("frankstupak/other", mapf, default, envfile)
check("A2", rc == "0" and tok == REAL_TOK and src == default,
      "*** an unmapped repo resolves from .secrets, NOT agent/.env — the whole ticket ***")

rc, src, tok, err = resolve("frankstupak/other", mapf, os.path.join(TMP, "nope.token"), envfile)
check("A3", rc == "0" and tok == ENV_TOK and src == envfile,
      "agent/.env still answers when the default file is absent (one release of grace)")

empty = wr("empty.token", "")
rc, src, tok, err = resolve("frankstupak/other", mapf, empty, envfile)
check("A4", rc == "0" and tok == ENV_TOK,
      "an EMPTY default falls through instead of failing — a secrets move is not an outage")

unread = wr("unreadable.token", REAL_TOK, 0o000)
rc, src, tok, err = resolve("frankstupak/other", mapf, unread, envfile)
check("A5", rc == "0" and tok == ENV_TOK,
      "an UNREADABLE default falls through the same way")

rc, src, tok, err = resolve("frankstupak/other", mapf, os.path.join(TMP, "nope.token"),
                            os.path.join(TMP, "nope.env"))
check("A6", rc != "0" and tok == "", "with no rung left it fails closed, and says so")

check("A7", resolve("frankstupak/other", mapf, default, envfile)[2] == REAL_TOK,
      "the trailing newline is stripped — a token with \\n authenticates as garbage")


print("\nB. the deprecation is LOUD, and never leaks the credential")
absent = os.path.join(TMP, "nope.token")
_, _, _, err_env = resolve("frankstupak/other", mapf, absent, envfile)
_, _, _, err_def = resolve("frankstupak/other", mapf, default, envfile)
check("B1", "DEPRECATED" in err_env and envfile in err_env,
      "resolving from agent/.env warns and names the file being deprecated")
check("B2", absent in err_env and "600" in err_env,
      "and the warning names the destination to fold it TO, with the mode — not just what is wrong")
check("B3", "DEPRECATED" not in err_def,
      "no warning on the happy path — a warning that always fires is noise")
check("B4", ENV_TOK not in err_env and REAL_TOK not in err_env and REAL_TOK not in err_def,
      "no token material on stderr in either case")

print("\nC. agent/.env is no longer SOURCED — blast radius, not just tidiness")
src_txt = open(LPR).read()
# Comments are stripped first: the fix DOCUMENTS the old `. agent/.env` in a comment
# explaining why it went, and a naive substring search matches its own explanation.
code_lines = [l for l in src_txt.splitlines() if not l.lstrip().startswith("#")]
sourced = [l.strip() for l in code_lines
           if l.strip().startswith((". /home/lumen/agent/.env", "source /home/lumen/agent/.env"))
           or " . /home/lumen/agent/.env" in l]
check("C1", not sourced,
      "*** lumen-pr no longer sources agent/.env, which used to drag EVERY key "
      f"(model providers, n8n, ghost, webapp creds) into every git/curl/jq child ***  {sourced}")
check("C2", "gitea-lumen.token" in src_txt,
      "the Gitea credential reads from the .secrets convention too")
check("C3", "GITEA_TOKEN=" in src_txt or "GITEA_TOKEN'" in src_txt or "'^GITEA_TOKEN=" in src_txt,
      "agent/.env GITEA_TOKEN is still honoured as the deprecated fallback")

print("\nD. the coupling that sourcing HID: the forge base URL keeps its port")
check("D1", "FORGE_BASE_URL" in src_txt,
      "the API base is derived from the origin, not from a var agent/.env happened to set")
env_noglobal = dict(os.environ)
env_noglobal.pop("GITEA_URL", None)          # the var the old sourcing supplied
env_noglobal["LUMEN_PR_SCOPE_CHECK"] = "0"   # the sanctioned probe hatch; never a workaround
p = subprocess.run([LPR, "check", "/home/lumen/bin"], capture_output=True, text=True,
                   env=env_noglobal, timeout=120)
d_out = (p.stdout or "") + (p.stderr or "")
check("D2", p.returncode == 0,
      "*** `lumen-pr check` on the live Gitea repo succeeds with GITEA_URL UNSET ***")
check("D3", "open PRs:" in d_out and "HTTP 000" not in d_out,
      f"the API answers instead of failing to connect on port 80 (rc={p.returncode})")


print("\nE. the live fleet state this ticket was filed about")
SEC = "/home/lumen/.secrets/github-frank.token"
try:
    st = os.stat(SEC)
    check("E1", stat.S_IMODE(st.st_mode) == 0o600,
          f"{SEC} exists at mode 600 like every other fleet secret")
    live = open(SEC).read()
    check("E2", live == live.strip() and live != "",
          "stored with no trailing newline — it is read verbatim by curl")
except FileNotFoundError:
    check("E1", False, f"{SEC} does not exist")
    check("E2", False, "cannot check trailing newline")

print()
shutil.rmtree(TMP, ignore_errors=True)
try:
    os.chmod(os.path.join(TMP, "unreadable.token"), 0o600)
except Exception:
    pass
print(f"probe_wq524: {len(PASS)} passed / {len(FAIL)} failed")
if FAIL:
    print("FAILED: " + ", ".join(FAIL))
sys.exit(1 if FAIL else 0)
