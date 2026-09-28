import datetime as dt
import json
import os
import stat
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import lumen_window as lw  # noqa: E402

CFG = """
[defaults]
tz = "America/Los_Angeles"
margin_sec = 600
verify_sec = 300
[windows.fleet]
day = "Mon"
start = "02:00"
end = "05:00"
[windows.commander-backup]
day = "Wed"
start = "02:00"
end = "05:00"
exclusive = true
nodes = ["commander-backup"]
lag_days = 14
primary_key = "commander-primary"
"""

MON_OPEN = "2026-09-28T09:05:00+00:00"   # 02:05 PDT
WED_OPEN = "2026-10-07T09:30:00+00:00"   # 02:30 PDT


@pytest.fixture
def env(tmp_path, monkeypatch):
    cfgp = tmp_path / "w.toml"
    cfgp.write_text(CFG)
    monkeypatch.setenv("LUMEN_WINDOW_CONFIG", str(cfgp))
    monkeypatch.setenv("LUMEN_WINDOW_STATE", str(tmp_path / "state"))
    monkeypatch.setenv("LUMEN_WINDOW_NTFY", "off")

    class E:
        tmp = tmp_path
        cfgp_ = cfgp

        @staticmethod
        def at(iso):
            monkeypatch.setenv("LUMEN_WINDOW_NOW", iso)

        @staticmethod
        def notes():
            p = tmp_path / "state" / "notifications.jsonl"
            return [json.loads(x) for x in p.read_text().splitlines()] if p.exists() else []

        @staticmethod
        def queue(op_id, cmd, **kw):
            args = ["queue", "add", "--id", op_id, "--window", kw.pop("window", "fleet"), "--cmd", cmd]
            for k, v in kw.items():
                args += [f"--{k.replace('_', '-')}"] + ([] if v is True else [str(v)])
            assert lw.main(args) == 0

    return E


def stub_gate(tmp, safe):
    p = tmp / "gate.sh"
    p.write_text("#!/bin/bash\n" + ('echo \'{"reboot_safe": true}\'; exit 0\n' if safe else 'echo \'{"reboot_safe": false}\'; exit 1\n'))
    p.chmod(p.stat().st_mode | stat.S_IEXEC)
    return str(p)


def test_dst_tracking(env):
    w = lw.load_config().windows["fleet"]
    st = lambda iso: lw.span(w, dt.datetime.fromisoformat(iso))[0]
    assert st("2026-09-28T08:59:59+00:00") == "closed" and st("2026-09-28T09:00:00+00:00") == "open"   # PDT
    assert st("2026-11-02T09:00:00+00:00") == "closed" and st("2026-11-02T10:00:00+00:00") == "open"   # PST
    assert st("2027-03-15T08:59:00+00:00") == "closed" and st("2027-03-15T09:00:00+00:00") == "open"   # after spring forward
    assert st("2026-09-28T12:00:00+00:00") == "closed"                                                # 05:00 PDT hard stop


def test_hard_stop_margin(env):
    env.at("2026-09-28T11:10:00+00:00")  # 3000s left
    assert lw.main(["check", "fleet", "--est-sec", "2500", "--quiet"]) == lw.TOO_LATE
    assert lw.main(["check", "fleet", "--est-sec", "2000", "--quiet"]) == lw.OK


def test_control_plane_never_authorised(env):
    env.at(MON_OPEN)
    assert lw.main(["check", "fleet", "--tags", "control-plane", "--quiet"]) == lw.CONTROL_PLANE
    assert lw.main(["enter", "fleet", "--op", "x", "--tags", "control-plane"]) == lw.CONTROL_PLANE


def test_exclusive_overlap_detected_and_enforced(env, monkeypatch):
    env.cfgp_.write_text(CFG.replace('day = "Wed"', 'day = "Mon"').replace('start = "02:00"\nend = "05:00"\nexclusive', 'start = "03:00"\nend = "04:00"\nexclusive'))
    assert any("overlap" in p for p in lw.validate(lw.load_config()))
    env.at("2026-09-28T10:30:00+00:00")  # 03:30 PDT, both open
    assert lw.main(["check", "fleet", "--quiet"]) == lw.EXCLUSIVE


def test_live_layout_has_no_overlap(env):
    assert lw.validate(lw.load_config()) == []


def test_exclusive_node_restriction(env):
    lw.main(["release", "record", "commander-primary", "1.2.3", "--at", "2026-09-01T00:00:00+00:00"])
    env.at(WED_OPEN)
    assert lw.main(["check", "commander-backup", "--release", "1.2.3", "--node", "other", "--quiet"]) == lw.NODE
    assert lw.main(["check", "commander-backup", "--release", "1.2.3", "--node", "commander-backup", "--quiet"]) == lw.OK


def test_lag_rule(env):
    lw.main(["release", "record", "commander-primary", "1.2.3", "--at", "2026-09-25T12:00:00+00:00"])
    env.at(WED_OPEN)  # 11.9 days later
    n = ["--node", "commander-backup", "--quiet"]
    assert lw.main(["check", "commander-backup", "--release", "1.2.3"] + n) == lw.LAG
    assert lw.main(["check", "commander-backup"] + n) == lw.LAG                       # release required
    assert lw.main(["check", "commander-backup", "--release", "9.9.9"] + n) == lw.LAG  # primary never ran it
    env.at("2026-10-14T09:30:00+00:00")  # 18.9 days
    assert lw.main(["check", "commander-backup", "--release", "1.2.3"] + n) == lw.OK


def test_backup_never_same_day_as_primary(env):
    lw.main(["release", "record", "commander-primary", "1.2.3", "--at", "2026-09-01T00:00:00+00:00"])
    lw.main(["release", "record", "commander-primary", "1.2.4", "--at", "2026-10-07T08:00:00+00:00"])  # 01:00 PDT same day
    env.at(WED_OPEN)
    assert lw.main(["check", "commander-backup", "--release", "1.2.3", "--node", "commander-backup", "--quiet"]) == lw.LAG


def test_release_record_refuses_same_day_pair(env):
    env.at(WED_OPEN)
    assert lw.main(["release", "record", "commander-backup", "1.2.3"]) == 0
    assert lw.main(["release", "record", "commander-primary", "1.2.4"]) == lw.LAG


def test_drain_halts_on_failure_and_skips_control_plane_and_reboot(env, monkeypatch):
    monkeypatch.setenv("LUMEN_REBOOT_GATE", stub_gate(env.tmp, safe=False))
    env.at(MON_OPEN)
    t = env.tmp
    env.queue("ok-op", f"touch {t}/a", est_sec=60, pri=1)
    env.queue("cp-op", f"touch {t}/cp", est_sec=60, pri=2, tags="control-plane")
    env.queue("rb-op", f"touch {t}/rb", est_sec=60, pri=3, node="n1", reboot=True)
    env.queue("fail-op", "false", est_sec=60, pri=4)
    env.queue("after-op", f"touch {t}/after", est_sec=60, pri=5)
    assert lw.main(["drain", "fleet"]) == 1
    assert (t / "a").exists()
    assert not (t / "cp").exists() and not (t / "rb").exists() and not (t / "after").exists()
    q = {x["id"]: x for x in lw.load_state()["queue"]}
    assert "ok-op" not in q and q["fail-op"]["attempts"] == 1 and "after-op" in q
    body = env.notes()[-1]["body"]
    assert "FAILED: fail-op" in body and "cp-op" in body and "reboot gate refused" in body
    assert lw.main(["check", "fleet", "--quiet"]) == lw.HALTED


def test_drain_reboot_runs_when_gate_passes(env, monkeypatch):
    monkeypatch.setenv("LUMEN_REBOOT_GATE", stub_gate(env.tmp, safe=True))
    env.at(MON_OPEN)
    env.queue("rb-op", f"touch {env.tmp}/rb", est_sec=60, node="n1", reboot=True)
    assert lw.main(["drain", "fleet"]) == 0 and (env.tmp / "rb").exists()


def test_drain_defers_op_that_cannot_finish(env):
    env.at("2026-09-28T11:50:00+00:00")  # 04:50 PDT, 600s left
    env.queue("late-op", f"touch {env.tmp}/late", est_sec=60)
    assert lw.main(["drain", "fleet"]) == 0
    assert not (env.tmp / "late").exists() and "deferred" in env.notes()[-1]["body"]


def test_failed_verify_halts(env):
    env.at(MON_OPEN)
    env.queue("v-op", "true", est_sec=60, verify="false")
    assert lw.main(["drain", "fleet"]) == 1
    assert [x["id"] for x in lw.load_state()["queue"]] == ["v-op"]


def test_close_screams_on_missed_drain_and_live_op(env):
    env.at(MON_OPEN)
    env.queue("q1", "true", est_sec=60)
    env.at("2026-09-28T12:00:30+00:00")
    assert lw.main(["close", "fleet"]) == 1 and "without a drain" in env.notes()[-1]["body"]
    dead = subprocess.Popen(["true"]); dead.wait()
    with lw.locked():
        s = lw.load_state()
        s["running"]["gone"] = {"window": "fleet", "pid": dead.pid}
        s["running"]["alive"] = {"window": "fleet", "pid": os.getpid()}
        s["queue"] = []
        lw.save_state(s)
    assert lw.main(["close", "fleet"]) == 1
    st = lw.load_state()["running"]
    assert "gone" not in st and "alive" in st and "alive still running" in env.notes()[-1]["body"]


def test_close_clean(env):
    env.at("2026-09-28T12:00:30+00:00")
    assert lw.main(["close", "fleet"]) == 0


def test_units_are_tz_anchored_and_non_persistent(env):
    u = lw.unit_files(lw.load_config())
    assert "OnCalendar=Mon *-*-* 02:00:00 America/Los_Angeles" in u["lumen-window-open-fleet.timer"]
    assert "OnCalendar=Mon *-*-* 05:00:00 America/Los_Angeles" in u["lumen-window-close-fleet.timer"]
    assert "OnCalendar=Wed *-*-* 02:00:00 America/Los_Angeles" in u["lumen-window-open-commander-backup.timer"]
    assert all("Persistent=false" in v for k, v in u.items() if k.endswith(".timer"))
    svc = u["lumen-window-open-fleet.service"]
    assert "ExecStart=/home/lumen/bin/lumen-window drain fleet" in svc and "RuntimeMaxSec=10800" in svc and "OnFailure=ntfy-onfailure@%n.service" in svc


def test_validate_flags_bad_windows(env):
    env.cfgp_.write_text(CFG.replace('end = "05:00"\n[windows.commander-backup]', 'end = "01:00"\n[windows.commander-backup]', 1).replace('lag_days = 14\nprimary_key = "commander-primary"', 'lag_days = 14'))
    p = lw.validate(lw.load_config())
    assert any("end must be after start" in x for x in p) and any("needs primary_key" in x for x in p)
