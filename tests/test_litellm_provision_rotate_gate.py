"""wq#3865: litellm-provision-keys --rotate sits behind the vault-backup gate. Dummy keys, no network:
gen_key / delete_key / save_key are replaced, so a test that gets past the gate cannot touch the live gateway."""
import importlib.machinery
import importlib.util
import os
import sys

import pytest

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BIN = os.path.join(HERE, "litellm-provision-keys")
sys.path.insert(0, HERE)


def load():
    loader = importlib.machinery.SourceFileLoader("lpk_under_test", BIN)
    spec = importlib.util.spec_from_loader("lpk_under_test", loader)
    mod = importlib.util.module_from_spec(spec)
    loader.exec_module(mod)
    return mod


@pytest.fixture
def lpk(tmp_path, monkeypatch):
    m = load()
    sd = tmp_path / "keys"
    sd.mkdir()
    (sd / "duet.key").write_text("sk-dummy-old-0001\n")
    bk = tmp_path / "bk"
    bk.mkdir()
    calls = []
    monkeypatch.setattr(m, "SECRETS_DIR", sd)
    monkeypatch.setattr(m, "_load_manifest", lambda: (tmp_path / "manifest.json", {}))
    monkeypatch.setattr(m, "gen_key", lambda *a, **k: calls.append("gen_key"))      # returns None = "generate failed"
    monkeypatch.setattr(m, "delete_key", lambda *a, **k: calls.append("delete_key") or True)
    monkeypatch.setattr(m, "save_key", lambda *a, **k: calls.append("save_key"))
    monkeypatch.setenv("RVB_BACKUP_DIRS", str(bk))
    monkeypatch.setenv("RVB_LOG", str(tmp_path / "rvb.log"))
    return {"m": m, "sd": sd, "bk": bk, "calls": calls, "log": tmp_path / "rvb.log"}


def test_rotate_refused_without_backup_changes_nothing(lpk):
    assert lpk["m"].rotate("duet") == 2
    assert lpk["calls"] == []                                   # gate fired before any gateway call
    assert (lpk["sd"] / "duet.key").read_text() == "sk-dummy-old-0001\n"
    log = lpk["log"].read_text()
    assert "REFUSED" in log and "sk-dummy-old" not in log


def test_rotate_refused_when_backup_is_a_stale_value(lpk):
    (lpk["bk"] / "litellm-duet.key.bak.20261014_000000").write_text("sk-dummy-older-0000\n")
    assert lpk["m"].rotate("duet") == 2 and lpk["calls"] == []


def test_rotate_proceeds_past_gate_with_matching_backup(lpk):
    (lpk["bk"] / "litellm-duet.key.bak.20261014_000000").write_text("sk-dummy-old-0001\n")
    rc = lpk["m"].rotate("duet")
    assert rc == 1                       # got past the gate; stubbed gen_key then failed, so nothing was burned
    assert "gen_key" in lpk["calls"] and "delete_key" not in lpk["calls"] and "save_key" not in lpk["calls"]
    assert (lpk["sd"] / "duet.key").read_text() == "sk-dummy-old-0001\n"
    assert "ALLOWED" in lpk["log"].read_text()
