"""stale-env-audit (wq#3211): a process that kept an old secret after its EnvironmentFile
was rotated must be reported by NAME, and no value may ever be printed."""
import importlib.machinery
import importlib.util
import os

HERE = os.path.dirname(os.path.abspath(__file__))
_path = os.path.join(HERE, "..", "stale-env-audit")
_loader = importlib.machinery.SourceFileLoader("stale_env_audit", _path)
_spec = importlib.util.spec_from_loader("stale_env_audit", _loader)
sea = importlib.util.module_from_spec(_spec)
_loader.exec_module(sea)

OLD = "pk-lf-" + "OLDVALUE-should-never-print"
NEW = "pk-lf-" + "NEWVALUE-should-never-print"


def test_parse_envfile_handles_comments_quotes_and_blank(tmp_path):
    p = tmp_path / "e.env"
    p.write_text('# c\n; c2\nA=1\nB="two words"\nC=\'x\'\n\nEMPTY=\nnoequals\n')
    assert sea.parse_envfile(str(p)) == {"A": "1", "B": "two words", "C": "x", "EMPTY": ""}


def test_parse_envfile_unreadable_is_none(tmp_path):
    assert sea.parse_envfile(str(tmp_path / "nope.env")) is None


def test_rotated_secret_the_process_never_reloaded_is_reported():
    """The wq#3211 shape: file has the new key, the running process still has the old."""
    differs, missing = sea.find_drift({"LANGFUSE_PUBLIC_KEY": OLD, "OTHER": "same"},
                                      {"LANGFUSE_PUBLIC_KEY": NEW, "OTHER": "same"})
    assert differs == ["LANGFUSE_PUBLIC_KEY"] and missing == []


def test_clean_process_reports_nothing():
    assert sea.find_drift({"A": "1", "B": "2"}, {"A": "1", "B": "2"}) == ([], [])


def test_new_var_added_to_file_after_start_is_missing():
    assert sea.find_drift({"A": "1"}, {"A": "1", "NEWVAR": "x"}) == ([], ["NEWVAR"])


def test_empty_file_values_are_ignored_like_systemd_does():
    """ssh.service ships SSHD_OPTS= ; systemd never exports empty assignments."""
    assert sea.find_drift({}, {"SSHD_OPTS": ""}) == ([], [])


def test_report_never_prints_a_value(monkeypatch, capsys):
    monkeypatch.setattr(sea, "audit", lambda: {
        "clean": 3, "unreadable": 0,
        "drift": [{"unit": "lumen-messenger.service", "differs": ["LANGFUSE_PUBLIC_KEY"], "missing": [],
                   "file_newer_than_process": ["agent.env"], "also_set_inline": []}]})
    rc = sea.main([])
    out = capsys.readouterr().out
    assert rc == 1
    assert "lumen-messenger.service" in out and "LANGFUSE_PUBLIC_KEY" in out
    assert OLD not in out and NEW not in out


def test_exit_codes(monkeypatch):
    monkeypatch.setattr(sea, "audit", lambda: {"clean": 4, "drift": [], "unreadable": 0})
    assert sea.main([]) == 0
    monkeypatch.setattr(sea, "audit", lambda: {"clean": 0, "drift": [], "unreadable": 9})
    assert sea.main([]) == 2
