"""End-to-end tests for the fleet-exec CLI (wq#1332 §5.2): grant + policy enforced
together, unknown node, tampered/expired/foreign grant via the real env-var path,
holddown-gates-restart, and the audit trail (both the shared jsonl and the worker's
own copy). subprocess is monkeypatched throughout — no real ssh/scp/minerva calls.

Run: python3 -m pytest tests/test_fleet_exec_cli.py -q
"""
import hashlib
import importlib.util
import json
import os
import sys
from datetime import datetime, timedelta, timezone
from importlib.machinery import SourceFileLoader

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
BIN_DIR = os.path.join(HERE, "..")
sys.path.insert(0, BIN_DIR)

_loader = SourceFileLoader("fleet_exec_cli", os.path.join(BIN_DIR, "fleet-exec"))
_spec = importlib.util.spec_from_loader(_loader.name, _loader)
fleet_exec = importlib.util.module_from_spec(_spec)
_loader.exec_module(fleet_exec)

POLICY_YAML = """
never:
  nodes: [thor]
  power: [thor, lumen, maat, sobek, bes]
  restart:
    lumen: ["vaultwarden*", "postgresql*"]
    maat: ["*"]
  sudo_argv_deny: [ip]
run_readonly_cmds:
  - "true"
  - uptime
  - cat
run_readonly_python_probes: []
copy_dirs:
  isis: [/home/isis/lumen-drop]
  thor: [/home/thor/lumen-drop]
bench_tools: {}
"""


def write_grant(tmp_path, **overrides):
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


@pytest.fixture
def env(tmp_path, monkeypatch):
    policy_path = tmp_path / "policy.yaml"
    policy_path.write_text(POLICY_YAML)
    audit_path = tmp_path / "audit.jsonl"
    worker_audit_path = tmp_path / "worker-audit.jsonl"
    monkeypatch.setattr(fleet_exec, "POLICY_PATH", str(policy_path))
    monkeypatch.setattr(fleet_exec, "AUDIT_PATH", str(audit_path))
    monkeypatch.setattr(fleet_exec, "WORKER_AUDIT_PATH", str(worker_audit_path))
    monkeypatch.setenv("WQ_AGENT", "fworker-wq1332")
    monkeypatch.setattr(fleet_exec, "known_nodes",
                        lambda: {"isis", "thor", "lumen", "maat", "thoth"})
    return {"tmp_path": tmp_path, "audit_path": audit_path, "worker_audit_path": worker_audit_path}


def set_grant_env(monkeypatch, tmp_path, **overrides):
    path, sha = write_grant(tmp_path, **overrides)
    monkeypatch.setenv("FOREMAN_GRANT_FILE", path)
    monkeypatch.setenv("FOREMAN_GRANT_SHA", sha)
    monkeypatch.setenv("FOREMAN_ATTEMPT", "1")
    return path, sha


def read_jsonl(path):
    if not os.path.exists(path):
        return []
    with open(path) as f:
        return [json.loads(line) for line in f if line.strip()]


# ---------------------------------------------------------------------------
# grant verification wired through the real env vars
# ---------------------------------------------------------------------------

def test_no_grant_file_env_denies(env, monkeypatch):
    monkeypatch.delenv("FOREMAN_GRANT_FILE", raising=False)
    monkeypatch.delenv("FOREMAN_GRANT_SHA", raising=False)
    with pytest.raises(SystemExit) as e:
        fleet_exec.main(["run", "isis", "--", "true"])
    assert e.value.code == 2
    records = read_jsonl(env["audit_path"])
    assert records[-1]["decision"] == "deny"
    assert "grant_missing_env" in records[-1]["rule"]


def test_tampered_grant_sha_denies(env, monkeypatch, tmp_path):
    path, sha = set_grant_env(monkeypatch, tmp_path)
    with open(path, "a") as f:
        f.write("\n")
    with pytest.raises(SystemExit) as e:
        fleet_exec.main(["run", "isis", "--", "true"])
    assert e.value.code == 2
    records = read_jsonl(env["audit_path"])
    assert "grant_sha_mismatch" in records[-1]["rule"]


def test_expired_grant_denies(env, monkeypatch, tmp_path):
    past = (datetime.now(timezone.utc) - timedelta(minutes=5)).strftime("%Y-%m-%dT%H:%M:%SZ")
    set_grant_env(monkeypatch, tmp_path, expires=past)
    with pytest.raises(SystemExit) as e:
        fleet_exec.main(["run", "isis", "--", "true"])
    assert e.value.code == 2
    records = read_jsonl(env["audit_path"])
    assert "grant_expired" in records[-1]["rule"]


def test_foreign_ticket_grant_denies(env, monkeypatch, tmp_path):
    monkeypatch.setenv("WQ_AGENT", "fworker-wq9999")
    set_grant_env(monkeypatch, tmp_path)  # minted for ticket 1332
    with pytest.raises(SystemExit) as e:
        fleet_exec.main(["run", "isis", "--", "true"])
    assert e.value.code == 2
    records = read_jsonl(env["audit_path"])
    assert "grant_ticket_mismatch" in records[-1]["rule"]


def test_unknown_node_denied(env, monkeypatch, tmp_path):
    set_grant_env(monkeypatch, tmp_path,
                   grants=[{"kind": "ssh", "node": "not-a-real-node", "verbs": ["run"]}])
    with pytest.raises(SystemExit) as e:
        fleet_exec.main(["run", "not-a-real-node", "--", "true"])
    assert e.value.code == 2
    records = read_jsonl(env["audit_path"])
    assert records[-1]["rule"] == "unknown_node"


def test_grant_present_but_no_ssh_grant_for_node_denies(env, monkeypatch, tmp_path):
    set_grant_env(monkeypatch, tmp_path,
                   grants=[{"kind": "ssh", "node": "lumen", "verbs": ["run"]}])
    with pytest.raises(SystemExit) as e:
        fleet_exec.main(["run", "isis", "--", "true"])
    assert e.value.code == 2
    records = read_jsonl(env["audit_path"])
    assert records[-1]["rule"] == "grant:no_ssh_run"


# ---------------------------------------------------------------------------
# argv shape / policy denial even with a valid, matching grant
# ---------------------------------------------------------------------------

def test_grant_ok_but_policy_denies_metachar(env, monkeypatch, tmp_path):
    set_grant_env(monkeypatch, tmp_path)
    with pytest.raises(SystemExit) as e:
        fleet_exec.main(["run", "isis", "--", "true;", "rm", "-rf", "/"])
    assert e.value.code == 2
    records = read_jsonl(env["audit_path"])
    assert records[-1]["rule"] == "argv:argv_metachar"


def test_grant_ok_but_policy_denies_not_readonly(env, monkeypatch, tmp_path):
    set_grant_env(monkeypatch, tmp_path)
    with pytest.raises(SystemExit) as e:
        fleet_exec.main(["run", "isis", "--", "systemctl", "restart", "ollama"])
    assert e.value.code == 2
    records = read_jsonl(env["audit_path"])
    assert records[-1]["rule"] == "policy:not_readonly"


def test_thor_never_reachable_beyond_run_copy(env, monkeypatch, tmp_path):
    """Restart grant for thor still denies — thor's hardlock, not just a missing
    grant. Grant itself contains a restart entry (simulating a corrupted/tampered
    dispatcher) so this proves the DENIAL comes from policy, not from grant absence."""
    set_grant_env(monkeypatch, tmp_path,
                   grants=[{"kind": "restart", "node": "thor", "unit": "sshd"}])
    with pytest.raises(SystemExit) as e:
        fleet_exec.main(["restart", "thor", "sshd"])
    assert e.value.code == 2
    records = read_jsonl(env["audit_path"])
    assert records[-1]["rule"] == "policy:never_nodes"


# ---------------------------------------------------------------------------
# successful run — allow audit before AND after, output propagated
# ---------------------------------------------------------------------------

def test_successful_run_writes_before_and_after_audit_and_propagates_output(env, monkeypatch, tmp_path, capsys):
    set_grant_env(monkeypatch, tmp_path)

    class FakeCompleted:
        returncode = 0
        stdout = "hello from isis\n"
        stderr = ""

    calls = []

    def fake_run_ssh(node, remote_argv, **kw):
        calls.append((node, remote_argv))
        return FakeCompleted()

    monkeypatch.setattr(fleet_exec, "run_ssh", fake_run_ssh)
    with pytest.raises(SystemExit) as e:
        fleet_exec.main(["run", "isis", "--", "true"])
    assert e.value.code == 0
    assert calls == [("isis", ["true"])]
    out = capsys.readouterr()
    assert "hello from isis" in out.out

    records = read_jsonl(env["audit_path"])
    assert len(records) == 2
    assert records[0]["decision"] == "allow" and "exit" not in records[0]
    assert records[1]["decision"] == "allow" and records[1]["exit"] == 0
    # worker's own copy gets the same two lines (guard.sh's stream joins on this)
    assert read_jsonl(env["worker_audit_path"]) == records


# ---------------------------------------------------------------------------
# restart (BROKER_WIRING.md §2.4.1-3): sudo -n, Lumen-side holddown, verify
# before and after. No fake node ever answers a holddown call over ssh, because
# the broker no longer makes one.
# ---------------------------------------------------------------------------

class Result:
    def __init__(self, returncode=0, stdout="", stderr=""):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


SHOW_LOADED = ("LoadState=loaded\nActiveState=active\nSubState=running\n"
               "ActiveEnterTimestampMonotonic=100\n")
SHOW_NOT_FOUND = ("LoadState=not-found\nActiveState=inactive\nSubState=dead\n"
                  "ActiveEnterTimestampMonotonic=0\n")


def fake_node(*, show=SHOW_LOADED, sudo_rc=0, action_rc=0, is_active="active"):
    """A node that answers exactly the probes cmd_restart makes, plus a recorder."""
    calls = []

    def fake_run_ssh(node, remote_argv, **kw):
        calls.append(remote_argv)
        if remote_argv[:2] == ["systemctl", "show"]:
            return Result(stdout=show)
        if remote_argv == ["sudo", "-n", "true"]:
            return Result(returncode=sudo_rc)
        if remote_argv[:2] == ["systemctl", "is-active"]:
            return Result(returncode=0 if is_active == "active" else 3,
                          stdout=is_active + "\n")
        return Result(returncode=action_rc)

    return fake_run_ssh, calls


@pytest.fixture
def local(monkeypatch):
    """Records the local holddown/ntfy/minerva side effects instead of running them."""
    seen = {"local": [], "ntfy": [], "facts": [], "rc": 0}

    def fake_run_local(argv, **kw):
        seen["local"].append(argv)
        return Result(returncode=seen["rc"], stderr="holddown: already held" if seen["rc"] else "")

    monkeypatch.setattr(fleet_exec, "run_local", fake_run_local)
    monkeypatch.setattr(fleet_exec, "notify",
                        lambda prio, title, msg: seen["ntfy"].append((prio, title)))
    monkeypatch.setattr(fleet_exec, "ingest_fact",
                        lambda s, p, v: seen["facts"].append((s, p)))
    monkeypatch.setattr(fleet_exec, "VERIFY_POLL_S", 0)
    monkeypatch.setattr(fleet_exec, "RESCUE_POLL_S", 0)
    return seen


def restart_grant(monkeypatch, tmp_path, node="isis", unit="ollama"):
    return set_grant_env(monkeypatch, tmp_path,
                          grants=[{"kind": "restart", "node": node, "unit": unit}])


def test_restart_denies_unit_not_found_before_any_holddown(env, monkeypatch, tmp_path, local):
    """§2.4.3: a typo must not create a holddown — LoadState is read first."""
    restart_grant(monkeypatch, tmp_path, unit="ollamma")
    ssh, calls = fake_node(show=SHOW_NOT_FOUND)
    monkeypatch.setattr(fleet_exec, "run_ssh", ssh)
    with pytest.raises(SystemExit) as e:
        fleet_exec.main(["restart", "isis", "ollamma"])
    assert e.value.code == 2
    assert local["local"] == []          # no holddown registered
    assert calls == [["systemctl", "show", "-p", fleet_exec.SHOW_PROPS, "ollamma"]]
    last = read_jsonl(env["audit_path"])[-1]
    assert last["rule"] == "unit_not_found" and last["decision"] == "deny"
    assert last["before"]["load"] == "not-found"


def test_restart_denies_when_passwordless_sudo_missing(env, monkeypatch, tmp_path, local):
    """§2.4.1: `sudo -n true` fails -> deny, no restart, no holddown, one alert."""
    restart_grant(monkeypatch, tmp_path)
    ssh, calls = fake_node(sudo_rc=1)
    monkeypatch.setattr(fleet_exec, "run_ssh", ssh)
    with pytest.raises(SystemExit) as e:
        fleet_exec.main(["restart", "isis", "ollama"])
    assert e.value.code == 2
    assert local["local"] == []
    assert ["sudo", "-n", "true"] in calls
    assert not any(c[:1] == ["sudo"] and "systemctl" in c for c in calls)
    assert local["ntfy"] and local["ntfy"][0][0] == "high"
    assert read_jsonl(env["audit_path"])[-1]["rule"] == "sudo_nopasswd_missing"


def test_restart_holddown_is_local_namespaced_and_verified(env, monkeypatch, tmp_path, local):
    """§2.4.1-3 happy path: sudo -n argv, Lumen-side <unit>@<node> holddown with a
    fleet-exec-restore restore-cmd, verified=true, flag dropped."""
    restart_grant(monkeypatch, tmp_path)
    ssh, calls = fake_node()
    monkeypatch.setattr(fleet_exec, "run_ssh", ssh)
    with pytest.raises(SystemExit) as e:
        fleet_exec.main(["restart", "isis", "ollama"])
    assert e.value.code == 0
    # the broker never speaks holddown over ssh any more
    assert not any(fleet_exec.HOLDDOWN_BIN in c for c in calls)
    assert ["sudo", "-n", "systemctl", "restart", "ollama"] in calls
    hd_set = local["local"][0]
    assert hd_set[:3] == [fleet_exec.HOLDDOWN_BIN, "set", "ollama@isis"]
    assert hd_set[-2:] == ["--restore-cmd", f"{fleet_exec.RESTORE_BIN} isis ollama"]
    assert local["local"][-1] == [fleet_exec.HOLDDOWN_BIN, "drop", "ollama@isis"]
    last = read_jsonl(env["audit_path"])[-1]
    assert last["verified"] is True and last["exit"] == 0
    assert last["before"]["active"] == "active" and last["after"]["sub"] == "running"


def test_restart_on_lumen_keeps_plain_local_holddown(env, monkeypatch, tmp_path, local):
    """§2.4.2: node=lumen is the unchanged local case — no @node, no restore-cmd."""
    restart_grant(monkeypatch, tmp_path, node="lumen", unit="ollama")
    ssh, _ = fake_node()
    monkeypatch.setattr(fleet_exec, "run_ssh", ssh)
    with pytest.raises(SystemExit) as e:
        fleet_exec.main(["restart", "lumen", "ollama"])
    assert e.value.code == 0
    hd_set = local["local"][0]
    assert hd_set[:3] == [fleet_exec.HOLDDOWN_BIN, "set", "ollama"]
    assert "--restore-cmd" not in hd_set


def test_restart_holddown_failure_blocks_systemctl(env, monkeypatch, tmp_path, local):
    restart_grant(monkeypatch, tmp_path)
    local["rc"] = 1
    ssh, calls = fake_node()
    monkeypatch.setattr(fleet_exec, "run_ssh", ssh)
    with pytest.raises(SystemExit) as e:
        fleet_exec.main(["restart", "isis", "ollama"])
    assert e.value.code == 1
    assert not any("systemctl" in c and c[0] == "sudo" for c in calls)
    assert read_jsonl(env["audit_path"])[-1]["rule"] == "holddown_failed"


def test_restart_not_active_leaves_holddown_active_and_alerts(env, monkeypatch, tmp_path, local):
    """§2.4.3: still not active after the rescue start -> exit 1, verified=false,
    flag NOT dropped (the healer keeps restoring), one urgent alert."""
    restart_grant(monkeypatch, tmp_path)
    ssh, calls = fake_node(is_active="failed")
    monkeypatch.setattr(fleet_exec, "run_ssh", ssh)
    with pytest.raises(SystemExit) as e:
        fleet_exec.main(["restart", "isis", "ollama"])
    assert e.value.code == 1
    assert calls.count(["sudo", "-n", "systemctl", "start", "ollama"]) == 1  # exactly one rescue
    assert not any(c[1:2] == ["drop"] for c in local["local"])
    assert ("urgent", "fleet-exec: ollama on isis not active after restart") in local["ntfy"]
    last = read_jsonl(env["audit_path"])[-1]
    assert last["verified"] is False and last["exit"] == 1


# ---------------------------------------------------------------------------
# §2.4.4 offline node, §2.4.5 copy target, §2.4.6 audit completeness
# ---------------------------------------------------------------------------

def test_offline_node_fails_closed_with_alert_and_fact(env, monkeypatch, tmp_path, local):
    """ssh's own 255 is a transport verdict, not a remote exit code."""
    set_grant_env(monkeypatch, tmp_path,
                   grants=[{"kind": "ssh", "node": "thoth", "verbs": ["run"]}])
    monkeypatch.setattr(fleet_exec, "run_ssh", lambda node, argv, **kw: Result(
        returncode=255, stderr="ssh: connect to host 100.75.1.108 port 22: Connection timed out"))
    with pytest.raises(SystemExit) as e:
        fleet_exec.main(["run", "thoth", "--", "true"])
    assert e.value.code == 2
    assert local["facts"] == [("thoth", "broker_last_transport_error")]
    assert local["ntfy"] and local["ntfy"][0][0] == "high"
    last = read_jsonl(env["audit_path"])[-1]
    assert last["rule"] == "ssh_transport_error" and last["exit"] == 2


def test_remote_exit_255_is_not_a_transport_error(env, monkeypatch, tmp_path, local):
    """A remote command may legitimately exit 255; only ssh's own diagnostics make
    it a transport failure."""
    set_grant_env(monkeypatch, tmp_path)
    monkeypatch.setattr(fleet_exec, "run_ssh",
                        lambda node, argv, **kw: Result(returncode=255, stdout="nope\n"))
    with pytest.raises(SystemExit) as e:
        fleet_exec.main(["run", "isis", "--", "true"])
    assert e.value.code == 255
    assert local["facts"] == []
    assert read_jsonl(env["audit_path"])[-1]["rule"] == "policy:run_readonly_cmds"


def test_copy_denies_when_drop_dir_missing(env, monkeypatch, tmp_path, local):
    """§2.4.5: the precheck denies BEFORE the allow line, so scp never runs."""
    set_grant_env(monkeypatch, tmp_path,
                   grants=[{"kind": "copy", "node": "isis", "dir": "/home/isis/lumen-drop"}])
    payload = tmp_path / "payload.txt"
    payload.write_text("x")
    monkeypatch.setattr(fleet_exec, "run_ssh", lambda node, argv, **kw: Result(returncode=1))
    monkeypatch.setattr(fleet_exec.subprocess, "run",
                        lambda *a, **k: pytest.fail("scp must not run"))
    with pytest.raises(SystemExit) as e:
        fleet_exec.main(["copy", "isis", str(payload), "/home/isis/lumen-drop"])
    assert e.value.code == 2
    records = read_jsonl(env["audit_path"])
    assert records[-1]["rule"] == "copy_dir_missing" and records[-1]["decision"] == "deny"
    assert not any(r["decision"] == "allow" for r in records)


def test_every_audit_line_carries_the_grant_sha(env, monkeypatch, tmp_path, local):
    """§2.4.6: the sha is what joins grant.json to fleet-exec.jsonl offline."""
    _, sha = restart_grant(monkeypatch, tmp_path)
    ssh, _ = fake_node()
    monkeypatch.setattr(fleet_exec, "run_ssh", ssh)
    with pytest.raises(SystemExit):
        fleet_exec.main(["restart", "isis", "ollama"])
    records = read_jsonl(env["audit_path"])
    assert records and all(r["grant_sha"] == sha for r in records)
