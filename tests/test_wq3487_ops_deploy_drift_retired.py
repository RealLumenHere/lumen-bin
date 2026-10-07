"""wq#3487: ops-deploy-drift honors a RETIRED marker — the lightweight alternative to
hand-writing an inverted --verify handler for every retired bundle (ops/VERIFY_CONTRACT.md).
A marked bundle reports verdict RETIRED (never VERIFY_FAIL/ABSENT) and --aq auto-resolves any
open ops-drift:<bundle> row for it, the same way a VERIFIED run already does.
"""
import importlib.machinery
import importlib.util
import json
import os

import pytest

PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                     "ops-deploy-drift")


def load():
    loader = importlib.machinery.SourceFileLoader("ops_deploy_drift_under_test_wq3487", PATH)
    spec = importlib.util.spec_from_loader(loader.name, loader)
    mod = importlib.util.module_from_spec(spec)
    loader.exec_module(mod)
    return mod


@pytest.fixture
def m(tmp_path, monkeypatch):
    mod = load()
    monkeypatch.setattr(mod, "OPS", str(tmp_path))
    monkeypatch.setattr(mod, "index_roots", lambda: set())  # RETIRED bypasses this; keep it cheap
    return mod


def make_bundle(tmp_path, name, retired_reason=None):
    bd = tmp_path / name
    bd.mkdir()
    (bd / "install.sh").write_text(
        '#!/usr/bin/env bash\nif [ "${1:-}" = --verify ]; then echo "MISS absent: x"; exit 1; fi\n')
    if retired_reason is not None:
        (bd / "RETIRED").write_text(retired_reason + "\n")
    return bd


class _FakeCursor:
    def __init__(self, store):
        self.store = store

    def execute(self, sql, params=()):
        self.store.append((sql, params))
        return self

    def commit(self):
        pass

    def close(self):
        pass


class _FakeAq:
    def __init__(self):
        self.ingested = []
        self.executed = []

    def ingest(self, **kw):
        self.ingested.append(kw)

    def db(self):
        return _FakeCursor(self.executed)

    def now(self):
        return "2026-10-07T00:00:00Z"


def test_retired_bundle_reports_retired_not_verify_fail(tmp_path, m, monkeypatch, capsys):
    make_bundle(tmp_path, "wq1406-paid-fallback-guard", retired_reason="superseded by live timers")
    monkeypatch.setattr(m.sys, "argv", ["ops-deploy-drift", "--json"])
    m.main()
    out = json.loads(capsys.readouterr().out)
    assert len(out) == 1
    r = out[0]
    assert r["bundle"] == "wq1406-paid-fallback-guard"
    assert r["verdict"] == "RETIRED"
    assert r["authoritative"] is True
    assert r["reason"] == "superseded by live timers"


def test_non_retired_failing_bundle_still_reports_verify_fail(tmp_path, m, monkeypatch, capsys):
    """Can-fail probe (wq#2630): a bundle with the exact same failing --verify handler but no
    RETIRED marker must still read VERIFY_FAIL — the marker, not the failure, is what changes
    the verdict."""
    make_bundle(tmp_path, "wq0001-not-retired")
    monkeypatch.setattr(m.sys, "argv", ["ops-deploy-drift", "--json"])
    m.main()
    out = json.loads(capsys.readouterr().out)
    assert out[0]["verdict"] == "VERIFY_FAIL"


def test_aq_resolves_open_row_for_retired_bundle_instead_of_filing(tmp_path, m, monkeypatch):
    make_bundle(tmp_path, "wq2500-rsshub-bridge", retired_reason="replaced by wq2501")
    fake_aq = _FakeAq()
    monkeypatch.setitem(m.sys.modules, "aq", fake_aq)
    monkeypatch.setattr(m.sys, "argv", ["ops-deploy-drift", "--aq"])
    m.main()
    assert fake_aq.ingested == [], "a RETIRED bundle must never open an aq row"
    assert len(fake_aq.executed) == 1
    sql, params = fake_aq.executed[0]
    assert "resolved" in sql
    assert "RETIRED marker" in params[0]
    assert params[2] == "ops-drift:wq2500-rsshub-bridge"
