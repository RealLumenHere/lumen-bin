"""Policy-matrix tests for fleet_exec_policy.py (wq#1332, WORKER_EXEC_CAPABILITY_GAP.md
§5.2): every `never` row, every metacharacter shape, tampered/expired/foreign grant.
Pure-function tests — no subprocess, no ssh, no live fleet. fleet-exec's own I/O
(ssh/scp/roster) is exercised separately by test_fleet_exec_cli.py with subprocess
monkeypatched.

Run: python3 -m pytest tests/test_fleet_exec_policy.py -q
"""
import hashlib
import json
import os
import sys
import time
from datetime import datetime, timedelta, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, ".."))
import fleet_exec_policy as P

POLICY = {
    "never": {
        "nodes": ["thor"],
        "power": ["thor", "lumen", "maat", "sobek", "bes"],
        "restart": {
            "lumen": ["vaultwarden*", "cloudflared", "desktop-commander-mcp",
                      "postgresql*", "tailscaled", "sshd"],
            "maat": ["*"],
            "sobek": ["postgresql*", "cloudflared"],
        },
        "sudo_argv_deny": ["ip", "macchanger", "dd", "mkfs*", "wipefs", "visudo",
                           "passwd", "useradd", "chpasswd", "tailscale up*", "systemctl mask*"],
    },
    "run_readonly_cmds": [
        "true", "uptime", "hostname", "uname", "cat", "ls", "stat", "df", "free",
        "systemctl status|is-active|is-enabled|show|cat|list-*",
        "journalctl", "ss", "ip addr", "tailscale status", "docker ps", "docker logs",
    ],
    "run_readonly_python_probes": ["import shutil; print(shutil.disk_usage('/'))"],
    "sudo_cmds": [
        "systemctl restart|reload|start <unit>",
        "apt-get install -y <pkg>",
    ],
    "copy_dirs": {
        "thor": ["/home/thor/lumen-drop"],
        "isis": ["/home/isis/lumen-drop"],
        "lumen": ["/home/lumen/lumen-drop"],
        "maat": ["/home/maat/lumen-drop"],
        "sobek": ["/home/sobek/lumen-drop"],
    },
    "bench_tools": {"thor": ["adb", "fastboot"]},
}


# ---------------------------------------------------------------------------
# never.nodes — thor gets no verb except readonly run + copy into its own lumen-drop
# ---------------------------------------------------------------------------

def test_thor_run_readonly_allowed():
    ok, rule, _ = P.check_run("thor", ["uptime"], POLICY)
    assert ok, rule


def test_thor_run_nonreadonly_denied():
    ok, rule, _ = P.check_run("thor", ["reboot"], POLICY)
    assert not ok
    assert rule == "policy:shell_escape_basename" or rule == "policy:not_readonly"


def test_thor_copy_into_lumen_drop_allowed():
    ok, rule, _ = P.check_copy("thor", "/home/thor/lumen-drop", POLICY)
    assert ok, rule


def test_thor_copy_elsewhere_denied():
    ok, _, _ = P.check_copy("thor", "/home/thor/other-dir", POLICY)
    assert not ok


def test_thor_restart_always_denied():
    ok, rule, _ = P.check_restart("thor", "anything.service", POLICY)
    assert not ok
    assert rule == "policy:never_nodes"


def test_thor_hardlock_survives_missing_policy_row():
    """Even if fleet-exec-policy.yaml's never.nodes row were edited away, the code
    constant still locks thor down (defense in depth, instinct tier 0 #1)."""
    policy_without_thor_row = json.loads(json.dumps(POLICY))
    policy_without_thor_row["never"]["nodes"] = []
    ok, rule, _ = P.check_restart("thor", "anything.service", policy_without_thor_row)
    assert not ok
    assert rule == "policy:never_nodes"


def test_thor_power_denied():
    ok, rule, _ = P.check_power("thor", POLICY)
    assert not ok and rule == "policy:never_power"


def test_thor_sudo_denied():
    ok, rule, _ = P.check_sudo("thor", ["systemctl", "restart", "foo"], POLICY)
    assert not ok and rule == "policy:never_nodes"


# ---------------------------------------------------------------------------
# never.power — crown-jewel + primary/standby pg, every listed node
# ---------------------------------------------------------------------------

def test_never_power_every_row():
    for node in ("thor", "lumen", "maat", "sobek", "bes"):
        ok, rule, _ = P.check_power(node, POLICY)
        assert not ok, f"{node} should be power-denied"
        assert rule == "policy:never_power"


def test_power_allowed_for_node_not_in_never_list():
    ok, rule, _ = P.check_power("isis", POLICY)
    assert ok, rule


# ---------------------------------------------------------------------------
# never.restart — per-node unit globs
# ---------------------------------------------------------------------------

def test_never_restart_lumen_every_glob():
    for unit in ("vaultwarden", "vaultwarden-web", "cloudflared", "desktop-commander-mcp",
                 "postgresql", "postgresql@14-main", "tailscaled", "sshd"):
        ok, rule, _ = P.check_restart("lumen", unit, POLICY)
        assert not ok, f"lumen:{unit} should be restart-denied"
        assert rule == "policy:never_restart"


def test_never_restart_lumen_allows_other_units():
    ok, rule, _ = P.check_restart("lumen", "ollama", POLICY)
    assert ok, rule


def test_never_restart_maat_wildcard_denies_everything():
    for unit in ("ollama", "anything", "postgresql", "a.service"):
        ok, rule, _ = P.check_restart("maat", unit, POLICY)
        assert not ok, f"maat:{unit} should be restart-denied by wildcard"
        assert rule == "policy:never_restart"


def test_never_restart_sobek_every_glob():
    for unit in ("postgresql", "postgresql@main", "cloudflared"):
        ok, rule, _ = P.check_restart("sobek", unit, POLICY)
        assert not ok
        assert rule == "policy:never_restart"


def test_never_restart_sobek_allows_other_units():
    ok, rule, _ = P.check_restart("sobek", "node_exporter", POLICY)
    assert ok, rule


def test_restart_allowed_for_node_with_no_never_row():
    ok, rule, _ = P.check_restart("isis", "anything.service", POLICY)
    assert ok, rule


# ---------------------------------------------------------------------------
# allow.restart / allow.ssh — broker enforces allow, not only the lint (wq#1484)
# ---------------------------------------------------------------------------

def test_allow_restart_hit():
    policy = json.loads(json.dumps(POLICY))
    policy["allow"] = {"restart": {"isis": ["ollama", "node_exporter*"]}}
    ok, rule, _ = P.check_restart("isis", "node_exporter", policy)
    assert ok, rule


def test_allow_restart_miss():
    policy = json.loads(json.dumps(POLICY))
    policy["allow"] = {"restart": {"isis": ["ollama"]}}
    ok, rule, _ = P.check_restart("isis", "anything.service", policy)
    assert not ok
    assert rule == "policy:restart_not_allowed"


def test_allow_restart_miss_node_absent_from_allow_map():
    policy = json.loads(json.dumps(POLICY))
    policy["allow"] = {"restart": {"sobek": ["node_exporter"]}}
    ok, rule, _ = P.check_restart("isis", "anything.service", policy)
    assert not ok
    assert rule == "policy:restart_not_allowed"


def test_never_beats_allow_restart():
    policy = json.loads(json.dumps(POLICY))
    policy["allow"] = {"restart": {"lumen": ["vaultwarden*"]}}
    ok, rule, _ = P.check_restart("lumen", "vaultwarden", policy)
    assert not ok
    assert rule == "policy:never_restart"


def test_no_allow_section_restart_unchanged():
    assert "allow" not in POLICY
    ok, rule, _ = P.check_restart("isis", "anything.service", POLICY)
    assert ok, rule


def test_allow_ssh_hit():
    policy = json.loads(json.dumps(POLICY))
    policy["allow"] = {"ssh": ["isis", "sobek"]}
    ok, rule, _ = P.check_ssh_node("isis", policy)
    assert ok, rule


def test_allow_ssh_miss():
    policy = json.loads(json.dumps(POLICY))
    policy["allow"] = {"ssh": ["sobek"]}
    ok, rule, _ = P.check_ssh_node("isis", policy)
    assert not ok
    assert rule == "policy:ssh_not_allowed"


def test_never_beats_allow_ssh():
    policy = json.loads(json.dumps(POLICY))
    policy["allow"] = {"ssh": ["thor"]}
    ok, rule, _ = P.check_ssh_node("thor", policy)
    assert not ok
    assert rule == "policy:never_nodes"


def test_no_allow_section_ssh_unchanged():
    assert "allow" not in POLICY
    ok, rule, _ = P.check_ssh_node("isis", POLICY)
    assert ok, rule


# ---------------------------------------------------------------------------
# sudo_argv_deny — v2, but the policy matrix is provable now
# ---------------------------------------------------------------------------

def test_sudo_argv_deny_every_row():
    denied = [
        ["ip", "link", "set", "eth0", "down"],
        ["macchanger", "-r", "eth0"],
        ["dd", "if=/dev/zero", "of=/dev/sda"],
        ["mkfs.ext4", "/dev/sda1"],
        ["wipefs", "-a", "/dev/sda"],
        ["visudo"],
        ["passwd", "root"],
        ["useradd", "evil"],
        ["chpasswd"],
        ["tailscale", "up", "--force-reauth"],
        ["systemctl", "mask", "sshd"],
    ]
    for argv in denied:
        ok, rule, reason = P.check_sudo("isis", argv, POLICY)
        assert not ok, f"{argv} should be sudo-denied ({reason})"
        assert rule == "policy:sudo_argv_deny", f"{argv} got rule {rule}"


def test_sudo_allowed_shape():
    ok, rule, _ = P.check_sudo("isis", ["systemctl", "restart", "ollama"], POLICY)
    assert ok, rule


# ---------------------------------------------------------------------------
# run: argv shape / metacharacter / escape-hatch rejection
# ---------------------------------------------------------------------------

def test_run_rejects_shell_metachars():
    for argv in (["true;", "rm", "-rf", "/"], ["true", "|", "cat"], ["true", "&&", "ls"],
                 ["true", "$(whoami)"], ["true", "`id`"], ["true", ">", "/tmp/x"]):
        ok, rule, _ = P.check_run("isis", argv, POLICY)
        assert not ok, f"{argv} should be rejected"
        assert rule == "argv:argv_metachar"


def test_run_rejects_dash_c_via_bash():
    ok, rule, _ = P.check_run("isis", ["bash", "-c", "id"], POLICY)
    assert not ok
    assert rule == "policy:shell_escape_basename"


def test_run_rejects_dash_c_for_non_python_basename():
    """-c on a basename that isn't itself a shell-escape name (so it reaches the
    dash-c check) and isn't python3 (so the probe exception can't apply)."""
    ok, rule, _ = P.check_run("isis", ["uptime", "-c"], POLICY)
    assert not ok
    assert rule == "policy:dash_c_denied"


def test_run_rejects_bash_sh_eval():
    for base in ("bash", "sh", "zsh", "eval", "perl", "ruby", "node"):
        ok, rule, _ = P.check_run("isis", [base], POLICY)
        assert not ok, f"{base} should never be a run target"
        assert rule == "policy:shell_escape_basename"


def test_run_rejects_argv0_with_path():
    ok, rule, _ = P.check_run("isis", ["/bin/true"], POLICY)
    assert not ok
    assert rule == "argv0_not_basename" or rule.startswith("argv:")


def test_run_rejects_empty_argv():
    ok, rule, _ = P.check_run("isis", [], POLICY)
    assert not ok
    assert rule == "argv:argv_empty"


def test_run_allows_readonly_basics():
    for argv in (["true"], ["uptime"], ["hostname"], ["uname"], ["df", "/"],
                 ["free"], ["journalctl", "-n", "50"], ["ss", "-tlnp"]):
        ok, rule, reason = P.check_run("isis", argv, POLICY)
        assert ok, f"{argv} should be allowed ({reason})"


def test_run_allows_systemctl_subcommand_glob():
    ok, rule, _ = P.check_run("isis", ["systemctl", "status", "ollama"], POLICY)
    assert ok, rule
    ok, rule, _ = P.check_run("isis", ["systemctl", "list-units"], POLICY)
    assert ok, rule


def test_run_denies_systemctl_restart():
    ok, rule, _ = P.check_run("isis", ["systemctl", "restart", "ollama"], POLICY)
    assert not ok
    assert rule == "policy:not_readonly"


def test_run_denies_ip_link_set():
    ok, rule, _ = P.check_run("isis", ["ip", "link", "set", "eth0", "down"], POLICY)
    assert not ok


def test_run_allows_ip_addr_only():
    ok, rule, _ = P.check_run("isis", ["ip", "addr"], POLICY)
    assert ok, rule


def test_run_blocks_credential_paths():
    for argv in (["cat", "/home/lumen/.ssh/id_ed25519"], ["cat", "/home/lumen/.env"],
                 ["ls", "-la", "/home/lumen/.ssh"], ["stat", "/home/lumen/.secrets/token"]):
        ok, rule, _ = P.check_run("isis", argv, POLICY)
        assert not ok, f"{argv} should be blocked as a credential path"
        assert rule == "policy:credential_path_blocked"


def test_run_python_probe_exact_match_allowed():
    ok, rule, _ = P.check_run(
        "isis", ["python3", "-c", "import shutil; print(shutil.disk_usage('/'))"], POLICY)
    assert ok, rule


def test_run_python_probe_arbitrary_code_denied():
    ok, rule, _ = P.check_run(
        "isis", ["python3", "-c", "import os; os.system('id')"], POLICY)
    assert not ok
    assert rule == "policy:python_probe_no_match"


# ---------------------------------------------------------------------------
# copy: only the declared dirs, per node
# ---------------------------------------------------------------------------

def test_copy_allowed_dir():
    ok, rule, _ = P.check_copy("isis", "/home/isis/lumen-drop", POLICY)
    assert ok, rule


def test_copy_denied_undeclared_dir():
    ok, rule, _ = P.check_copy("isis", "/etc", POLICY)
    assert not ok
    assert rule == "policy:copy_dir_not_allowed"


def test_copy_denied_unknown_node():
    ok, rule, _ = P.check_copy("nonexistent-node", "/home/nonexistent-node/lumen-drop", POLICY)
    assert not ok


# ---------------------------------------------------------------------------
# bench — v2, testable now
# ---------------------------------------------------------------------------

def test_bench_thor_allows_declared_tools():
    ok, rule, _ = P.check_bench("thor", "adb", POLICY)
    assert ok, rule


def test_bench_thor_denies_undeclared_tool():
    ok, rule, _ = P.check_bench("thor", "nc", POLICY)
    assert not ok


def test_bench_undeclared_node_denied():
    ok, rule, _ = P.check_bench("isis", "adb", POLICY)
    assert not ok


# ---------------------------------------------------------------------------
# load_policy — fail-closed on unreadable/malformed file
# ---------------------------------------------------------------------------

def test_load_policy_missing_file_raises(tmp_path):
    try:
        P.load_policy(str(tmp_path / "nope.yaml"))
        assert False, "expected PolicyError"
    except P.PolicyError:
        pass


def test_load_policy_chmod_000_raises(tmp_path):
    p = tmp_path / "policy.yaml"
    p.write_text("never:\n  nodes: []\n")
    os.chmod(p, 0o000)
    try:
        try:
            P.load_policy(str(p))
            assert False, "expected PolicyError"
        except P.PolicyError:
            pass
    finally:
        os.chmod(p, 0o644)


def test_load_policy_malformed_missing_never_raises(tmp_path):
    p = tmp_path / "policy.yaml"
    p.write_text("run_readonly_cmds: [true]\n")
    try:
        P.load_policy(str(p))
        assert False, "expected PolicyError"
    except P.PolicyError:
        pass


# ---------------------------------------------------------------------------
# load_grant — tampered sha, expired, foreign ticket, missing worker match
# ---------------------------------------------------------------------------

def _write_grant(tmp_path, **overrides):
    grant = {
        "ticket": 1332,
        "attempt": 1,
        "expires": (datetime.now(timezone.utc) + timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "grants": [{"kind": "ssh", "node": "isis", "verbs": ["run"]}],
    }
    grant.update(overrides)
    raw = json.dumps(grant, sort_keys=True).encode()
    path = tmp_path / "grant.json"
    path.write_bytes(raw)
    return str(path), hashlib.sha256(raw).hexdigest()


def test_grant_valid_roundtrip(tmp_path):
    path, sha = _write_grant(tmp_path)
    grant, err = P.load_grant(path, sha, "1332", "fworker-wq1332")
    assert err is None
    assert grant["ticket"] == 1332


def test_grant_missing_env_path():
    grant, err = P.load_grant(None, "deadbeef", "1332", "fworker-wq1332")
    assert grant is None and err == "grant_missing_env"


def test_grant_unreadable_path(tmp_path):
    grant, err = P.load_grant(str(tmp_path / "nope.json"), "deadbeef", "1332", "w")
    assert grant is None and err == "grant_unreadable"


def test_grant_sha_env_missing(tmp_path):
    path, _sha = _write_grant(tmp_path)
    grant, err = P.load_grant(path, None, "1332", "w")
    assert grant is None and err == "grant_sha_env_missing"


def test_grant_tampered_sha_mismatch(tmp_path):
    """The classic tamper case: file edited after the sha was minted (or the sha
    the worker supplies just doesn't match what's on disk)."""
    path, sha = _write_grant(tmp_path)
    with open(path, "a") as f:
        f.write("\n")  # one byte appended after sha256_bytes was computed
    grant, err = P.load_grant(path, sha, "1332", "fworker-wq1332")
    assert grant is None and err == "grant_sha_mismatch"


def test_grant_expired(tmp_path):
    grant_expires_past = (datetime.now(timezone.utc) - timedelta(minutes=1)).strftime(
        "%Y-%m-%dT%H:%M:%SZ")
    path, sha = _write_grant(tmp_path, expires=grant_expires_past)
    grant, err = P.load_grant(path, sha, "1332", "fworker-wq1332")
    assert grant is None and err == "grant_expired"


def test_grant_foreign_ticket_mismatch(tmp_path):
    """A grant minted for wq#1332, presented by a worker claiming to be wq#9999."""
    path, sha = _write_grant(tmp_path)
    grant, err = P.load_grant(path, sha, "9999", "fworker-wq9999")
    assert grant is None and err == "grant_ticket_mismatch"


def test_grant_worker_mismatch(tmp_path):
    path, sha = _write_grant(tmp_path, worker="fworker-wq1332")
    grant, err = P.load_grant(path, sha, "1332", "fworker-wqOTHER")
    assert grant is None and err == "grant_worker_mismatch"


def test_grant_invalid_json(tmp_path):
    path = tmp_path / "grant.json"
    path.write_bytes(b"not json{{{")
    sha = hashlib.sha256(b"not json{{{").hexdigest()
    grant, err = P.load_grant(str(path), sha, "1332", "w")
    assert grant is None and err == "grant_invalid_json"


def test_grant_malformed_no_grants_key(tmp_path):
    raw = json.dumps({"ticket": 1332, "expires": "2099-01-01T00:00:00Z"}).encode()
    path = tmp_path / "grant.json"
    path.write_bytes(raw)
    sha = hashlib.sha256(raw).hexdigest()
    grant, err = P.load_grant(str(path), sha, "1332", "w")
    assert grant is None and err == "grant_malformed"


def test_grant_expiry_unparseable(tmp_path):
    path, sha = _write_grant(tmp_path, expires="not-a-date")
    grant, err = P.load_grant(path, sha, "1332", "w")
    assert grant is None and err == "grant_expiry_unparseable"


# ---------------------------------------------------------------------------
# find_grants
# ---------------------------------------------------------------------------

def test_find_grants_matches_kind_and_node():
    grant = {"grants": [
        {"kind": "ssh", "node": "isis", "verbs": ["run"]},
        {"kind": "restart", "node": "isis", "unit": "ollama"},
        {"kind": "restart", "node": "maat", "unit": "ollama"},
    ]}
    hits = P.find_grants(grant, "restart", "isis")
    assert len(hits) == 1
    assert hits[0]["unit"] == "ollama"
