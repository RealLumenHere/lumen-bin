"""rotate-provider-keys: rollback / journal / backup-shred / guard behavior against temp files (no network, no litellm)."""
import glob, importlib.machinery, importlib.util, os, signal, sys
import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
PATH = os.path.join(HERE, '..', 'rotate-provider-keys')


@pytest.fixture
def m(tmp_path, monkeypatch):
    loader = importlib.machinery.SourceFileLoader('rpk_under_test', os.path.abspath(PATH))
    mod = importlib.util.module_from_spec(importlib.util.spec_from_loader('rpk_under_test', loader))
    loader.exec_module(mod)
    a, b = tmp_path / 'agent.env', tmp_path / 'gemini.env'
    a.write_text('CEREBRAS_API_KEY=oldcerebras\nGEMINI_API_KEY=oldgem\nOTHER=1\n')
    b.write_text('GEMINI_API_KEY=oldgem\n')
    for k, v in dict(ENV_FILES=[str(a), str(b)], LITELLM_ENV_FILES=[str(a), str(b)], RAW_FILES=[], LOG=str(tmp_path / 'log'),
                     JOURNAL=str(tmp_path / 'journal'), LOCK_PATH=str(tmp_path / 'lock'), DROP=str(tmp_path / 'drop')).items():
        monkeypatch.setattr(mod, k, v)
    mod.calls = {'recreate': 0}
    def recreate():
        mod.calls['recreate'] += 1
        return True
    for k, v in dict(preflight=lambda new: True, recreate_litellm=recreate, verify=lambda provs: True, restart_consumers=lambda: True,
                     consumer_units=lambda: [], stale_env_gate=lambda: True, langfuse_trace_landed=lambda since, wait=45: True).items():
        monkeypatch.setattr(mod, k, v)
    mod.a, mod.b, mod.tmp = a, b, tmp_path
    yield mod
    for s in mod.GUARDED:
        signal.signal(s, signal.default_int_handler if s == signal.SIGINT else signal.SIG_DFL)


def leftovers(m): return glob.glob(str(m.tmp / '*.bak.*_rotate'))
def log(m): return open(m.LOG).read()


def test_success_swaps_and_shreds_backups(m):
    assert m.run({'CEREBRAS': 'newcerebras'}) == 0
    t = m.a.read_text()
    assert 'CEREBRAS_API_KEY=newcerebras' in t and 'OTHER=1' in t and 'oldgem' in t
    assert leftovers(m) == [] and not os.path.exists(m.JOURNAL)


def test_keep_backups_flag(m):
    assert m.run({'CEREBRAS': 'newcerebras'}, keep_backups=True) == 0
    assert len(leftovers(m)) == 2 and not os.path.exists(m.JOURNAL)


def test_verify_failure_rolls_back_and_cleans(m, monkeypatch):
    seq = iter([False, True])
    monkeypatch.setattr(m, 'verify', lambda provs: next(seq))
    assert m.run({'CEREBRAS': 'newcerebras'}) == 3
    assert 'oldcerebras' in m.a.read_text() and 'newcerebras' not in m.a.read_text()
    assert leftovers(m) == [] and not os.path.exists(m.JOURNAL) and m.calls['recreate'] == 2


def test_unverified_rollback_keeps_backups_then_recover(m, monkeypatch):
    monkeypatch.setattr(m, 'verify', lambda provs: False)
    assert m.run({'CEREBRAS': 'newcerebras'}) == 3
    assert len(leftovers(m)) == 2 and os.path.exists(m.JOURNAL)
    m.a.write_text('CEREBRAS_API_KEY=garbage\n')            # simulate a half-written state
    monkeypatch.setattr(m, 'verify', lambda provs: True)
    assert m.recover() == 0
    assert 'oldcerebras' in m.a.read_text()
    assert leftovers(m) == [] and not os.path.exists(m.JOURNAL)


def test_sigterm_mid_run_rolls_back(m):
    m.install_signal_handlers()
    def recreate():
        m.calls['recreate'] += 1
        if m.calls['recreate'] == 1:
            os.kill(os.getpid(), signal.SIGTERM)
            import time; time.sleep(1)                      # handler must fire before this returns
        return True
    m.recreate_litellm = recreate
    assert m.run({'CEREBRAS': 'newcerebras'}) == 3
    assert 'oldcerebras' in m.a.read_text() and 'newcerebras' not in m.a.read_text()
    assert 'INTERRUPTED by SIGTERM' in log(m) and leftovers(m) == [] and not os.path.exists(m.JOURNAL)


def test_selftest_changes_nothing_and_cleans(m):
    before = (m.a.read_text(), m.b.read_text())
    assert m.run({'CEREBRAS': 'oldcerebras', 'GEMINI': 'oldgem'}, selftest=True) == 0
    assert (m.a.read_text(), m.b.read_text()) == before and leftovers(m) == []


def test_drift_is_unfinished_not_rolled_back(m, monkeypatch):
    monkeypatch.setattr(m, 'stale_env_gate', lambda: False)
    assert m.run({'CEREBRAS': 'newcerebras'}) == 5
    assert 'newcerebras' in m.a.read_text() and leftovers(m) == [] and 'UNFINISHED' in log(m)


def test_missing_langfuse_trace_warns_but_does_not_fail(m, monkeypatch):
    monkeypatch.setattr(m, 'langfuse_trace_landed', lambda since, wait=45: False)
    assert m.run({'CEREBRAS': 'newcerebras'}) == 0
    assert 'WARN: no Langfuse trace' in log(m)


def test_journal_blocks_new_run(m, monkeypatch):
    open(m.JOURNAL, 'w').write('{}')
    monkeypatch.setattr(sys, 'argv', ['x', 'apply'])
    with pytest.raises(SystemExit) as e:
        m.main()
    assert 'recover' in str(e.value)


def test_selftest_requires_explicit_flag(m, monkeypatch):
    monkeypatch.setattr(sys, 'argv', ['x', 'selftest'])
    with pytest.raises(SystemExit) as e:
        m.main()
    assert e.value.code == 2 and m.calls['recreate'] == 0


def test_recover_with_no_journal_is_noop(m):
    assert m.recover() == 0 and m.calls['recreate'] == 0


def test_recover_refuses_when_backup_missing(m):
    import json
    open(m.JOURNAL, 'w').write(json.dumps({'ts': 't', 'provs': [], 'saved': [[str(m.a), str(m.tmp / 'nope')]]}))
    assert m.recover() == 4 and os.path.exists(m.JOURNAL)


def test_lint_allows_direct_mistral_id():
    assert '"mistral-small-latest"' in open(os.path.join(HERE, '..', 'model-ref-lint')).read()
