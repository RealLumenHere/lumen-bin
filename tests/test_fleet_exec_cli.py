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
    monkeypatch.setattr(fleet_exec, "known_nodes", lambda: {"isis", "thor", "lumen", "maat"})
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
# restart: holddown gates the actual systemctl call
# ---------------------------------------------------------------------------

def test_restart_holddown_failure_blocks_systemctl(env, monkeypatch, tmp_path):
    set_grant_env(monkeypatch, tmp_path,
                   grants=[{"kind": "restart", "node": "isis", "unit": "ollama"}])

    class FailedHolddown:
        returncode = 1
        stdout = ""
        stderr = "holddown: ollama already held by someone-else"

    calls = []

    def fake_run_ssh(node, remote_argv, **kw):
        calls.append(remote_argv)
        return FailedHolddown()

    monkeypatch.setattr(fleet_exec, "run_ssh", fake_run_ssh)
    with pytest.raises(SystemExit) as e:
        fleet_exec.main(["restart", "isis", "ollama"])
    assert e.value.code == 1
    assert len(calls) == 1  # holddown attempted, systemctl NEVER called
    assert calls[0][0] == fleet_exec.HOLDDOWN_BIN
    records = read_jsonl(env["audit_path"])
    assert records[-1]["rule"] == "holddown_failed"


def test_restart_holddown_success_then_systemctl(env, monkeypatch, tmp_path):
    set_grant_env(monkeypatch, tmp_path,
                   grants=[{"kind": "restart", "node": "isis", "unit": "ollama"}])

    class OkResult:
        def __init__(self, out=""):
            self.returncode = 0
            self.stdout = out
            self.stderr = ""

    calls = []

    def fake_run_ssh(node, remote_argv, **kw):
        calls.append(remote_argv)
        return OkResult()

    monkeypatch.setattr(fleet_exec, "run_ssh", fake_run_ssh)
    with pytest.raises(SystemExit) as e:
        fleet_exec.main(["restart", "isis", "ollama"])
    assert e.value.code == 0
    assert len(calls) == 2
    assert calls[0][0] == fleet_exec.HOLDDOWN_BIN
    assert calls[1] == ["systemctl", "restart", "ollama"]
