"""wq#3281: rotate-consumers-restart ordering, scoping and refusals. No root, no systemd:
audit and restart are replaced. Run: python3 -m pytest tests/test_rotate_consumers_restart.py"""
import importlib.util
import os
from importlib.machinery import SourceFileLoader

HERE = os.path.dirname(os.path.abspath(__file__))
_l = SourceFileLoader("rcr", os.path.join(HERE, "..", "rotate-consumers-restart"))
_s = importlib.util.spec_from_loader("rcr", _l)
rcr = importlib.util.module_from_spec(_s)
_l.exec_module(rcr)


def row(unit, names, kind="systemd"):
    return {"kind": kind, "unit": unit, "differs": names, "missing": []}


def drive(monkeypatch, first, second, restart_ok=True):
    reports = iter([first, second])
    monkeypatch.setattr(rcr, "run_audit", lambda: next(reports))
    calls = []
    monkeypatch.setattr(rcr, "restart", lambda u: (calls.append(u), (restart_ok, "active" if restart_ok else "failed"))[1])
    return calls


def test_order_workers_first_user_facing_last():
    got = rcr.order(["lumen-messenger.service", "n8n-watcher.service", "lumen-fleet-cerebras.service"])
    assert got == ["lumen-fleet-cerebras.service", "n8n-watcher.service", "lumen-messenger.service"]


def test_scopes_to_rotated_vars_and_restarts_in_order(monkeypatch):
    first = {"drift": [row("lumen-messenger.service", ["LANGFUSE_SECRET_KEY"]),
                       row("lumen-fleet-mistral.service", ["LANGFUSE_SECRET_KEY"]),
                       row("unrelated.service", ["OTHER"])]}
    calls = drive(monkeypatch, first, {"drift": [row("unrelated.service", ["OTHER"])]})
    assert rcr.main(["--vars", "LANGFUSE_SECRET_KEY"]) == 0
    assert calls == ["lumen-fleet-mistral.service", "lumen-messenger.service"]


def test_still_drifting_after_restart_fails(monkeypatch):
    d = {"drift": [row("lumen-fleet-mistral.service", ["K"])]}
    drive(monkeypatch, d, d)
    assert rcr.main(["--vars", "K"]) == 1


def test_control_plane_is_refused_not_restarted(monkeypatch):
    d = {"drift": [row("desktop-commander-mcp.service", ["K"]), row("lumen-fleet-mistral.service", ["K"])]}
    calls = drive(monkeypatch, d, d)
    assert rcr.main(["--vars", "K"]) == 3
    assert "desktop-commander-mcp.service" not in calls


def test_docker_drift_reports_exit_4(monkeypatch):
    d = {"drift": [row("litellm", ["K"], kind="docker")]}
    calls = drive(monkeypatch, d, d)
    assert rcr.main(["--vars", "K"]) == 4 and calls == []


def test_inconclusive_audit_is_exit_2(monkeypatch):
    monkeypatch.setattr(rcr, "run_audit", lambda: None)
    assert rcr.main(["--vars", "K"]) == 2


def test_dry_run_restarts_nothing(monkeypatch):
    d = {"drift": [row("lumen-fleet-mistral.service", ["K"])]}
    calls = drive(monkeypatch, d, d)
    assert rcr.main(["--vars", "K", "--dry-run"]) == 0 and calls == []
