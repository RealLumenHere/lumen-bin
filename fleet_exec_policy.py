"""fleet_exec_policy — pure policy-matrix logic for /home/lumen/bin/fleet-exec
(wq#1332, foreman/research/WORKER_EXEC_CAPABILITY_GAP.md §2.4).

Kept dependency-free of ssh/subprocess/network so the policy matrix (every `never`
row, every metacharacter shape, tampered/expired/foreign grants) is unit-testable
without touching the live fleet. `fleet-exec` (the CLI) is the only thing that does
I/O: it loads a policy dict with load_policy(), loads+verifies a grant with
load_grant(), then calls the check_* functions here before it ever opens an ssh
connection.

THOR_HARDLOCK is deliberately NOT derived from the policy file: instinct tier 0 #1
(THOR IS THE ONLY HARD RULE) is a code-level constant so a corrupted or badly edited
copy of fleet-exec-policy.yaml can never open Thor beyond the two narrow exceptions
(read-only `run`, `copy` into thor's lumen-drop). The yaml's `never.nodes: [thor]`
row is the documented, reviewable copy of the same rule — belt and suspenders, not
the single point of truth.
"""
import fnmatch
import hashlib
import json
import os
import re
import time
from datetime import datetime, timezone

THOR_HARDLOCK = frozenset({"thor"})

# argv[0] basenames that are never a valid probe/target regardless of policy —
# these are the shell/interpreter escape hatches the whole design exists to close.
_SHELL_ESCAPE_BASENAMES = frozenset({
    "bash", "sh", "zsh", "ksh", "dash", "csh", "tcsh", "fish", "eval", "env",
    "python", "python3", "perl", "ruby", "node", "nodejs", "xargs", "find",
    "awk", "socat", "nc", "ncat", "telnet",
})
# argv[0]=="python3" is the one member of _SHELL_ESCAPE_BASENAMES that gets a
# narrow, exact-match exception below (run_readonly_python_probes) — never widened
# to the rest of the set.
_PYTHON_PROBE_BASENAME = "python3"

_METACHAR_RE = re.compile(r"[|;&$`<>\n]")

# cat/ls/stat are on run_readonly_cmds unqualified (matches the design doc's literal
# list) but take a path argument, and a "read-only" probe that can `cat` an ssh key
# is not read-only in any sense instinct #3 cares about. Block the obvious credential
# shapes as defense-in-depth on top of the doc's list, not instead of it.
_CREDENTIAL_PATH_MARKERS = (
    ".ssh", "id_ed25519", "id_rsa", ".env", "credentials", ".secrets",
    "authorized_keys", "known_hosts", ".git-credentials", "token",
)


class PolicyError(RuntimeError):
    """Raised for a fail-closed condition: caller must deny, never fall through."""


def load_policy(path):
    import yaml
    try:
        with open(path, "rb") as f:
            raw = f.read()
    except OSError as e:
        raise PolicyError(f"policy_unreadable: {e}") from e
    try:
        data = yaml.safe_load(raw) or {}
    except yaml.YAMLError as e:
        raise PolicyError(f"policy_invalid_yaml: {e}") from e
    if not isinstance(data, dict) or "never" not in data:
        raise PolicyError("policy_malformed: missing top-level 'never' section")
    return data


def sha256_bytes(data):
    return hashlib.sha256(data).hexdigest()


def load_grant(path, expected_sha, expected_ticket, expected_worker, *, now=None):
    """(grant_dict, None) on success, (None, reason_code) on any fail-closed
    condition. Every branch is a DENY, never an exception the CLI has to remember
    to catch — grant handling is the one place a missed except would fail open."""
    now = now or datetime.now(timezone.utc)
    if not path:
        return None, "grant_missing_env"
    try:
        with open(path, "rb") as f:
            raw = f.read()
    except OSError:
        return None, "grant_unreadable"
    actual_sha = sha256_bytes(raw)
    if not expected_sha:
        return None, "grant_sha_env_missing"
    if actual_sha != expected_sha:
        return None, "grant_sha_mismatch"
    try:
        grant = json.loads(raw)
    except json.JSONDecodeError:
        return None, "grant_invalid_json"
    if not isinstance(grant, dict) or "grants" not in grant:
        return None, "grant_malformed"
    try:
        expires = datetime.fromisoformat(grant["expires"].replace("Z", "+00:00"))
    except (KeyError, ValueError, AttributeError):
        return None, "grant_expiry_unparseable"
    if expires.tzinfo is None:
        expires = expires.replace(tzinfo=timezone.utc)
    if now >= expires:
        return None, "grant_expired"
    if str(grant.get("ticket")) != str(expected_ticket):
        return None, "grant_ticket_mismatch"
    if expected_worker and grant.get("worker") not in (None, expected_worker):
        return None, "grant_worker_mismatch"
    return grant, None


def find_grants(grant, kind, node):
    return [g for g in grant.get("grants", [])
            if g.get("kind") == kind and g.get("node") == node]


def _basename(argv0):
    return os.path.basename(argv0 or "")


def check_argv_shape(argv):
    """Shape rules that apply to every verb before any allowlist is consulted:
    non-empty, no shell metacharacters in any element, argv[0] is a bare basename
    (no path separators — a grant/policy allowlist is matched on the *name*, not on
    an attacker-chosen path that happens to end in an allowed name). `-c` is NOT
    banned here — check_run's python3 probe is the one narrow, exact-match exception
    to "no -c", so the ban lives in check_run/check_sudo where that exception can be
    applied first, not in the shared shape check every verb goes through."""
    if not argv or not argv[0]:
        return "argv_empty"
    for tok in argv:
        if _METACHAR_RE.search(tok):
            return "argv_metachar"
    if argv[0] != _basename(argv[0]):
        return "argv0_not_basename"
    return None


def _pattern_ok(basename, pattern):
    return fnmatch.fnmatchcase(basename, pattern)


def check_run(node, argv, policy):
    """(ok, rule, reason). rule is a short machine-readable tag for the audit log."""
    if not argv or not argv[0]:
        return False, "argv:argv_empty", "argv shape rejected before allowlist"
    base = _basename(argv[0])
    # The python3 probe is checked for an EXACT match before the generic shape/
    # metachar check runs, deliberately: a probe string like "import shutil; ..."
    # legitimately contains a ';' that is inert here (ssh transmits it as one
    # shlex-quoted argv element to python3, never through a remote shell), but the
    # generic check below has to keep denying that same character for every other
    # command. Exact-match-against-a-reviewed-list IS the validation for this one
    # basename; nothing here widens to a prefix/glob match.
    if base == _PYTHON_PROBE_BASENAME and argv[1:2] == ["-c"]:
        probes = policy.get("run_readonly_python_probes", [])
        if " ".join(argv[2:]) in probes:
            return True, "policy:run_readonly_python_probes", "exact probe match"
        return False, "policy:python_probe_no_match", f"argv {argv[1:]!r} not an allowlisted probe"
    shape_err = check_argv_shape(argv)
    if shape_err:
        return False, f"argv:{shape_err}", "argv shape rejected before allowlist"
    # thor (and any future never.nodes entry): run is the ONE verb still reachable
    # here, but only through this same readonly allowlist — nothing special-cased,
    # the allowlist below is the entire exception.
    if base in _SHELL_ESCAPE_BASENAMES and base != _PYTHON_PROBE_BASENAME:
        return False, "policy:shell_escape_basename", f"{base} is never a run target"
    if base == _PYTHON_PROBE_BASENAME:
        return False, "policy:python_probe_no_match", f"argv {argv[1:]!r} not an allowlisted probe"
    if "-c" in argv[1:]:
        return False, "policy:dash_c_denied", "-c is only ever allowed for an exact python3 probe match"
    if base in ("cat", "ls", "stat") and any(
            m in a.lower() for a in argv[1:] for m in _CREDENTIAL_PATH_MARKERS):
        return False, "policy:credential_path_blocked", f"{argv[1:]!r} looks like a credential path"
    for entry in policy.get("run_readonly_cmds", []):
        parts = entry.split(" ", 1)
        entry_base = parts[0]
        if base != entry_base:
            continue
        if len(parts) == 1:
            # single-token entries (true/uptime/hostname/uname/cat/ls/stat/df/free/
            # journalctl/ss) — cat/ls/df/stat legitimately take a path argument
            # (e.g. `df /`), the rest are argument-free probes; either way any
            # remaining argv already passed the shape + credential-path checks above.
            return True, "policy:run_readonly_cmds", entry
        subpatterns = parts[1].split("|")
        if len(argv) >= 2 and any(_pattern_ok(argv[1], p) for p in subpatterns):
            return True, "policy:run_readonly_cmds", entry
    return False, "policy:not_readonly", f"{' '.join(argv)!r} not on run_readonly_cmds"


def check_copy(node, remote_dir, policy):
    dirs = policy.get("copy_dirs", {}).get(node, [])
    if remote_dir not in dirs:
        return False, "policy:copy_dir_not_allowed", f"{remote_dir!r} not in copy_dirs[{node}]"
    if node in THOR_HARDLOCK or node in policy["never"].get("nodes", []):
        # thor's exception is exactly this: copy into its own lumen-drop and
        # nowhere else — already enforced by the dirs check above, nothing extra.
        pass
    return True, "policy:copy_dirs", remote_dir


def check_restart(node, unit, policy):
    if node in THOR_HARDLOCK or node in policy["never"].get("nodes", []):
        return False, "policy:never_nodes", f"{node} allows no verb except readonly run/copy"
    globs = policy.get("never", {}).get("restart", {}).get(node, [])
    for g in globs:
        if _pattern_ok(unit, g):
            return False, "policy:never_restart", f"{unit} matches never.restart[{node}]={g!r}"
    allow_restart = policy.get("allow", {}).get("restart")
    if allow_restart is not None:
        node_allow = allow_restart.get(node, [])
        if not any(_pattern_ok(unit, g) for g in node_allow):
            return False, "policy:restart_not_allowed", \
                f"{unit} not in allow.restart[{node}]={node_allow!r}"
    return True, "policy:restart_allowed", unit


def check_ssh_node(node, policy):
    """Broker-side allow.ssh gate — opt-in: only called by a caller that wants an
    allowlist narrower than never.nodes. Absent allow.ssh, behaviour is unchanged."""
    if node in THOR_HARDLOCK or node in policy["never"].get("nodes", []):
        return False, "policy:never_nodes", f"{node} allows no verb except readonly run/copy"
    allow_ssh = policy.get("allow", {}).get("ssh")
    if allow_ssh is not None:
        if not any(_pattern_ok(node, g) for g in allow_ssh):
            return False, "policy:ssh_not_allowed", f"{node} not in allow.ssh={allow_ssh!r}"
    return True, "policy:ssh_allowed", node


def check_power(node, policy):
    """v2 — fleet-exec has no `power` verb yet; kept testable now so the policy
    matrix has one source of truth before the CLI verb lands."""
    never_power = set(policy.get("never", {}).get("power", [])) | THOR_HARDLOCK
    if node in never_power:
        return False, "policy:never_power", f"{node} is in never.power"
    return True, "policy:power_allowed", node


def check_sudo(node, argv, policy):
    """v2 — fleet-exec has no `sudo` verb yet; kept testable now for the same
    reason as check_power."""
    shape_err = check_argv_shape(argv)
    if shape_err:
        return False, f"argv:{shape_err}", "argv shape rejected before allowlist"
    if "-c" in argv[1:]:
        return False, "policy:dash_c_denied", "-c is never allowed for sudo"
    if node in THOR_HARDLOCK or node in policy["never"].get("nodes", []):
        return False, "policy:never_nodes", f"{node} allows no sudo verb"
    base = _basename(argv[0])
    joined = " ".join(argv)
    for pattern in policy.get("never", {}).get("sudo_argv_deny", []):
        # two-token patterns ("tailscale up*", "systemctl mask*") match the whole
        # joined argv; single-token patterns match argv[0]'s basename only.
        target = joined if " " in pattern else base
        if _pattern_ok(target, pattern):
            return False, "policy:sudo_argv_deny", f"{target!r} matches deny pattern {pattern!r}"
    for entry in policy.get("sudo_cmds", []):
        entry_base = entry.split(" ", 1)[0]
        if base == entry_base:
            return True, "policy:sudo_cmds", entry
    return False, "policy:sudo_not_allowlisted", f"{' '.join(argv)!r} not on sudo_cmds"


def check_bench(node, tool, policy):
    """v2 — fleet-exec has no `bench` verb yet; kept testable for the same reason."""
    if node in THOR_HARDLOCK and tool not in policy.get("bench_tools", {}).get(node, []):
        return False, "policy:never_nodes", f"{node} allows no bench verb outside bench_tools"
    tools = policy.get("bench_tools", {}).get(node, [])
    if tool not in tools:
        return False, "policy:bench_tool_not_allowed", f"{tool!r} not in bench_tools[{node}]"
    return True, "policy:bench_tools", tool


# --- peer verb (wq#3200): the ONE reviewed path to start a process on a second ---
# node. `fleet-exec peer <node> <name>` runs EXACTLY policy.peer_cmds[node][name].argv
# — no worker argv reaches the remote node at all, which is why this needs no change
# to _SHELL_ESCAPE_BASENAMES (that set guards worker-chosen argv[0]; a peer entry's
# argv is authored in fleet-exec-policy.yaml, PR-reviewed entry by entry) and no
# weakening of THOR_HARDLOCK (same carve-out shape check_bench() already has: thor
# stays fully denied unless the exact name was reviewed into peer_cmds[thor]).

_PEER_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")


def _peer_entry_error(node, entry, policy):
    """One reason string if the reviewed entry is malformed or violates an invariant,
    else None. Fail closed: a bad entry is a deny, never a best-effort run."""
    if not isinstance(entry, dict):
        return "peer entry must be a mapping with argv + sha256"
    argv = entry.get("argv")
    if not isinstance(argv, list) or not argv or not all(
            isinstance(a, str) and a for a in argv):
        return "peer entry argv must be a non-empty list of non-empty strings"
    for tok in argv:
        if _METACHAR_RE.search(tok):
            return f"peer entry argv element {tok!r} contains a shell metacharacter"
    if not argv[0].startswith("/"):
        return f"peer entry point {argv[0]!r} must be an absolute path"
    if _basename(argv[0]) in _SHELL_ESCAPE_BASENAMES:
        return (f"peer entry point basename {_basename(argv[0])!r} is an interpreter "
                f"— pin a wrapper script instead; the sha256 must cover what runs")
    for d in policy.get("copy_dirs", {}).get(node, []):
        if argv[0] == d or argv[0].startswith(d.rstrip("/") + "/"):
            return (f"peer entry point {argv[0]!r} is inside worker-writable "
                    f"copy_dirs[{node}] ({d}) — a worker could copy over it")
    sha = entry.get("sha256")
    if not isinstance(sha, str) or not _PEER_SHA256_RE.match(sha):
        return "peer entry sha256 must be 64 lowercase hex chars (sha256sum of argv[0])"
    return None


def check_peer(node, name, policy):
    """(ok, rule, reason) — same tuple shape as every other check_*."""
    entries = policy.get("peer_cmds", {}).get(node, {})
    if not isinstance(entries, dict):
        return False, "policy:peer_entry_invalid", f"peer_cmds[{node}] is not a mapping"
    if (node in THOR_HARDLOCK or node in policy.get("never", {}).get("nodes", [])) \
            and name not in entries:
        return False, "policy:never_nodes", f"{node} allows no peer verb outside peer_cmds"
    if name not in entries:
        return False, "policy:peer_not_allowed", f"{name!r} not in peer_cmds[{node}]"
    err = _peer_entry_error(node, entries[name], policy)
    if err:
        return False, "policy:peer_entry_invalid", err
    return True, "policy:peer_cmds", name


def peer_entry(node, name, policy):
    """The validated entry dict for an ALLOWED (node, name), else None. The CLI calls
    this only after check_peer() said yes; re-running the check here keeps a
    time-of-check/time-of-use gap from ever executing an unvalidated entry."""
    ok, _, _ = check_peer(node, name, policy)
    if not ok:
        return None
    return policy["peer_cmds"][node][name]


def audit_record(*, ticket, attempt, worker, node, verb, argv, decision, rule,
                  exit_code=None, elapsed_s=None, out_sha256=None, out_head=None,
                  ts=None, before=None, after=None, verified=None, grant_sha=None):
    """before/after/verified/grant_sha are BROKER_WIRING.md §2.4.6: the unit state
    the broker read on each side of the action, whether it came back active, and the
    sha that joins this line to grant.json and events/<worker>.jsonl."""
    rec = {
        "ts": ts if ts is not None else time.time(),
        "ticket": ticket,
        "attempt": attempt,
        "worker": worker,
        "node": node,
        "verb": verb,
        "argv": argv,
        "decision": decision,
        "rule": rule,
    }
    if exit_code is not None:
        rec["exit"] = exit_code
    if elapsed_s is not None:
        rec["elapsed_s"] = round(elapsed_s, 3)
    if out_sha256 is not None:
        rec["out_sha256"] = out_sha256
    if out_head is not None:
        rec["out_head"] = out_head
    if before is not None:
        rec["before"] = before
    if after is not None:
        rec["after"] = after
    if verified is not None:
        rec["verified"] = bool(verified)
    if grant_sha is not None:
        rec["grant_sha"] = grant_sha
    return rec


def append_jsonl(path, record):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    line = json.dumps(record, sort_keys=True)
    with open(path, "a") as f:
        f.write(line + "\n")
