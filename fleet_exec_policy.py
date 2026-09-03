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
    return True, "policy:restart_allowed", unit


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


def audit_record(*, ticket, attempt, worker, node, verb, argv, decision, rule,
                  exit_code=None, elapsed_s=None, out_sha256=None, out_head=None,
                  ts=None):
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
    return rec


def append_jsonl(path, record):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    line = json.dumps(record, sort_keys=True)
    with open(path, "a") as f:
        f.write(line + "\n")
