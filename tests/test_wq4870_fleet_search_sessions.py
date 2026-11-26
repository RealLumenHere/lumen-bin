"""wq#4870: `fleet-search sessions` wraps the local cass index of ~/.claude/projects.

Contract under test (offline, fake cass binary, no real index needed):
  (a) fresh index + hits -> ok:true, results mapped, exit 0
  (b) stale index        -> ok:false with a pointer to lumen-cass-index.service, exit 2
                            (never an empty "no findings" envelope)
  (c) cass missing       -> ok:false, exit 2
  (d) every cass call carries CASS_AUTO_REFRESH=0 + XDG_DATA_HOME (no detached reindex,
      never the root-fs default data dir)

Run: python3 -m pytest tests/test_wq4870_fleet_search_sessions.py -v
"""
import json
import os
import stat
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
SCRIPT = os.path.join(HERE, "..", "fleet-search")

FAKE = r'''#!/usr/bin/env python3
import json, os, sys
log = os.environ["FAKE_CASS_LOG"]
with open(log, "a") as fh:
    fh.write(json.dumps({"argv": sys.argv[1:], "auto": os.environ.get("CASS_AUTO_REFRESH"),
                         "xdg": os.environ.get("XDG_DATA_HOME")}) + "\n")
if sys.argv[1] == "status":
    stale = os.environ.get("FAKE_STALE") == "1"
    print(json.dumps({"index": {"exists": True, "fresh": not stale, "stale": stale,
                                "last_indexed_at": "2026-11-26T06:26:28Z"}}))
elif sys.argv[1] == "search":
    print(json.dumps({"total_matches": 1, "hits": [{
        "source_path": "/home/lumen/.claude/projects/x/s.jsonl", "line_number": 7,
        "workspace": "/home/lumen/foreman", "title": "t", "snippet": "litellm restart",
        "score": 3.5, "created_at": 1}]}))
'''


def _run(tmp_path, stale=False, bin_override=None):
    fake = tmp_path / "cass"
    fake.write_text(FAKE)
    fake.chmod(fake.stat().st_mode | stat.S_IEXEC)
    log = tmp_path / "calls.jsonl"
    env = dict(os.environ, FLEET_CASS_BIN=bin_override or str(fake),
               FLEET_CASS_XDG_DATA=str(tmp_path / "share"), FAKE_CASS_LOG=str(log),
               FAKE_STALE="1" if stale else "0", CASS_AUTO_REFRESH="1")
    r = subprocess.run([sys.executable, SCRIPT, "sessions", "litellm restart", "--n", "3"],
                       capture_output=True, text=True, env=env, timeout=60)
    calls = [json.loads(l) for l in log.read_text().splitlines()] if log.exists() else []
    return r, json.loads(r.stdout), calls


def test_fresh_index_returns_results(tmp_path):
    r, out, calls = _run(tmp_path)
    assert r.returncode == 0 and out["ok"] is True and out["source"] == "sessions"
    assert out["total_matches"] == 1
    assert out["results"][0]["source_path"].startswith("/home/lumen/.claude/projects/")
    assert out["results"][0]["line_number"] == 7
    search = [c for c in calls if c["argv"][0] == "search"][0]
    assert search["argv"][1] == "litellm restart" and "--limit" in search["argv"]


def test_stale_index_is_ok_false_not_empty(tmp_path):
    r, out, calls = _run(tmp_path, stale=True)
    assert r.returncode == 2 and out["ok"] is False
    assert "lumen-cass-index.service" in out["error"]
    assert "results" not in out
    assert not [c for c in calls if c["argv"][0] == "search"]


def test_missing_cass_is_ok_false(tmp_path):
    r, out, _ = _run(tmp_path, bin_override=str(tmp_path / "nope"))
    assert r.returncode == 2 and out["ok"] is False and "not installed" in out["error"]


def test_env_disables_auto_refresh_and_pins_data_dir(tmp_path):
    _, _, calls = _run(tmp_path)
    assert calls, "fake cass never called"
    for c in calls:
        assert c["auto"] == "0"
        assert c["xdg"] == str(tmp_path / "share")
