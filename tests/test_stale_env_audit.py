"""stale-env-audit (wq#3211): a process that kept an old secret after its EnvironmentFile
was rotated must be reported by NAME, and no value may ever be printed."""
import importlib.machinery
import importlib.util
import sqlite3
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


# ---- wq#3215: --aq keeps the alert queue in step ---------------------------------------------

def _row(unit, differs=("LANGFUSE_PUBLIC_KEY",)):
    return {"unit": unit, "differs": list(differs), "missing": [], "file_newer_than_process": ["agent.env"],
            "also_set_inline": []}


def test_plan_adds_every_drifting_unit_and_resolves_only_clean_ones():
    add, res = sea.plan_aq([_row("a.service")], {"a.service": 1, "b.service": 2}, complete=True)
    assert [r["unit"] for r in add] == ["a.service"]
    assert res == [(2, "b.service")]


def test_plan_never_resolves_after_an_incomplete_scan():
    add, res = sea.plan_aq([], {"b.service": 2}, complete=False)
    assert res == []


def _stub_env(tmp_path, monkeypatch, rows):
    db = tmp_path / "alerts.db"
    c = sqlite3.connect(str(db))
    c.execute("create table alerts(id integer primary key, source text, key text, status text)")
    for i, (src, key, st) in enumerate(rows, 1):
        c.execute("insert into alerts values(?,?,?,?)", (i, src, key, st))
    c.commit()
    c.close()
    log = tmp_path / "aq.log"
    stub = tmp_path / "aq"
    stub.write_text('#!/bin/bash\nprintf "%s\\n" "$*" >> ' + str(log) + "\n")
    stub.chmod(0o755)
    monkeypatch.setattr(sea, "ALERTS_DB", str(db))
    monkeypatch.setattr(sea, "AQ_BIN", str(stub))
    monkeypatch.setattr(sea.os, "geteuid", lambda: 1000)
    return log


def test_sync_opens_a_row_for_drift_and_resolves_a_stale_one(tmp_path, monkeypatch):
    log = _stub_env(tmp_path, monkeypatch, [("stale-env", "old.service", "open"), ("other", "old.service", "open")])
    res = {"clean": 5, "unreadable": 0, "drift": [_row("lumen-messenger.service")]}
    assert sea.sync_aq(res) == (1, 1)
    calls = log.read_text().splitlines()
    assert calls[0].startswith("add stale-env lumen-messenger.service ")
    assert "LANGFUSE_PUBLIC_KEY" in calls[0] and "restart lumen-messenger.service" in calls[0]
    assert calls[1].startswith("resolve 1 ")          # only the stale-env row, not source=other


def test_sync_resolves_nothing_when_the_scan_was_partial(tmp_path, monkeypatch):
    log = _stub_env(tmp_path, monkeypatch, [("stale-env", "old.service", "open")])
    assert sea.sync_aq({"clean": 5, "unreadable": 3, "drift": []}) == (0, 0)
    assert not log.exists()


def test_sync_resolves_nothing_when_the_alert_db_is_unreadable(tmp_path, monkeypatch):
    log = _stub_env(tmp_path, monkeypatch, [])
    monkeypatch.setattr(sea, "ALERTS_DB", str(tmp_path / "missing.db"))
    assert sea.sync_aq({"clean": 5, "unreadable": 0, "drift": []}) == (0, 0)
    assert not log.exists()


def test_root_calls_aq_through_runuser(monkeypatch):
    seen = []
    monkeypatch.setattr(sea.os, "geteuid", lambda: 0)
    monkeypatch.setattr(sea.subprocess, "run", lambda cmd, **k: seen.append(cmd))
    sea._aq("add", "x")
    assert seen[0][:4] == ["runuser", "-u", "lumen", "--"]
