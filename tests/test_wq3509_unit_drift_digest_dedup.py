"""wq#3509: --digest must ntfy once per distinct drift state, not once per 15-min cycle.
Unchanged change set -> suppressed; changed set -> new ntfy; failed send -> retry next
cycle; CLEAN -> state cleared so the next drift episode notifies again."""
import importlib.machinery, importlib.util, os, subprocess
import pytest

PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "unit-drift")


def load(sysdir, state):
    os.environ["UNIT_DRIFT_SYS"] = str(sysdir)
    os.environ["UNIT_DRIFT_DIGEST_STATE"] = str(state)
    loader = importlib.machinery.SourceFileLoader("unit_drift_under_test_3509", PATH)
    spec = importlib.util.spec_from_loader(loader.name, loader); m = importlib.util.module_from_spec(spec); loader.exec_module(m)
    return m


@pytest.fixture
def m(tmp_path, monkeypatch):
    (tmp_path / "sys").mkdir()
    (tmp_path / "sys" / "a.service").write_text("[Unit]\n")
    mod = load(tmp_path / "sys", tmp_path / "digest.state")
    calls = []
    def fake(args, **kw):
        calls.append(list(args))
        return subprocess.CompletedProcess(args, 0, stdout="", stderr="")
    monkeypatch.setattr(mod.subprocess, "run", fake)
    monkeypatch.setattr(mod, "dangling_target_lint", lambda: [])
    monkeypatch.setattr(mod, "guard_lint", lambda: [])
    monkeypatch.setattr(mod, "unexplained_masks", lambda units: [])
    mod.calls = calls
    return mod


def set_baseline(m, monkeypatch, units):
    monkeypatch.setattr(m, "load_manifest",
                        lambda u=units: {"units": u, "dropins": {}, "enable": {}})


def ntfys(m):
    return [c for c in m.calls if c[0] == m.NTFY]


def test_unchanged_drift_notifies_once(m, monkeypatch):
    set_baseline(m, monkeypatch, {})
    assert m.cmd_check(digest=True) == 1
    assert m.cmd_check(digest=True) == 1
    assert len(ntfys(m)) == 1


def test_changed_drift_notifies_again_with_new_hash(m, monkeypatch):
    set_baseline(m, monkeypatch, {})
    assert m.cmd_check(digest=True) == 1
    set_baseline(m, monkeypatch, {"b.service": "h"})
    assert m.cmd_check(digest=True) == 1
    sent = ntfys(m)
    assert len(sent) == 2
    hashes = [c[-1].split("[")[1].split("]")[0] for c in sent]
    assert hashes[0] != hashes[1]


def test_failed_send_retries_next_cycle(m, monkeypatch):
    set_baseline(m, monkeypatch, {})
    def failing(args, **kw):
        m.calls.append(list(args))
        return subprocess.CompletedProcess(args, 1, stdout="", stderr="")
    monkeypatch.setattr(m.subprocess, "run", failing)
    assert m.cmd_check(digest=True) == 1
    assert m.cmd_check(digest=True) == 1
    assert len(ntfys(m)) == 2  # nothing recorded, so the send is retried


def test_clean_clears_state_so_next_episode_notifies(m, monkeypatch):
    set_baseline(m, monkeypatch, {})
    assert m.cmd_check(digest=True) == 1
    cur = m.scan()
    set_baseline(m, monkeypatch, cur["units"])
    assert m.cmd_check(digest=True) == 0
    assert not os.path.exists(m.DIGEST_STATE)
    set_baseline(m, monkeypatch, {})
    assert m.cmd_check(digest=True) == 1
    assert len(ntfys(m)) == 2


def test_no_digest_never_touches_state(m, monkeypatch):
    set_baseline(m, monkeypatch, {})
    assert m.cmd_check(digest=False) == 1
    assert not ntfys(m)
    assert not os.path.exists(m.DIGEST_STATE)
