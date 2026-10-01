"""wq#3282: fleet-update-check flags enabled services that are inactive (the BindsTo-stopped class
that `systemctl --failed` cannot see), and leaves out the legitimately idle ones."""
import importlib.machinery
import importlib.util
import os

HERE = os.path.dirname(os.path.abspath(__file__))
_l = importlib.machinery.SourceFileLoader("fuc", os.path.join(os.path.dirname(HERE), "fleet-update-check"))
_s = importlib.util.spec_from_loader("fuc", _l)
fuc = importlib.util.module_from_spec(_s)
_l.exec_module(fuc)


def blk(uid, active="inactive", typ="simple", rae="no", cond="yes", load="loaded"):
    return (f"Id={uid}\nLoadState={load}\nActiveState={active}\nType={typ}\n"
            f"RemainAfterExit={rae}\nConditionResult={cond}\n")


def raw(*blocks, trig=""):
    return f"#TRIGGERS {trig}\n" + "\n".join(blocks)


def test_the_2026_09_08_thoth_incident_is_caught():
    # sonarr/radarr/prowlarr cleanly stopped by BindsTo, bazarr failed, the rest running
    r = raw(blk("sonarr.service"), blk("radarr.service"), blk("prowlarr.service"),
            blk("bazarr.service", active="failed"), blk("qbittorrent-nox.service", active="active"))
    assert fuc.enabled_inactive(r) == ["prowlarr.service", "radarr.service", "sonarr.service"]


def test_running_failed_and_activating_are_not_this_checks_business():
    r = raw(blk("a.service", active="active"), blk("b.service", active="failed"),
            blk("c.service", active="activating"))
    assert fuc.enabled_inactive(r) == []


def test_plain_oneshots_are_idle_by_design_but_remain_after_exit_is_not():
    r = raw(blk("apt-daily.service", typ="oneshot"), blk("mount-thing.service", typ="oneshot", rae="yes"))
    assert fuc.enabled_inactive(r) == ["mount-thing.service"]


def test_timer_and_socket_started_services_are_excluded():
    r = raw(blk("backup.service"), blk("cups.service"), blk("real.service"), trig="backup cups")
    assert fuc.enabled_inactive(r) == ["real.service"]


def test_skipped_by_condition_and_unloaded_are_excluded():
    r = raw(blk("x.service", cond="no"), blk("y.service", load="not-found"))
    assert fuc.enabled_inactive(r) == []


def test_allowlist_is_honoured():
    r = raw(blk("thermald.service"), blk("sonarr.service"))
    assert fuc.enabled_inactive(r, idle={"thermald.service"}) == ["sonarr.service"]


def test_empty_and_garbage_input_do_not_raise():
    assert fuc.enabled_inactive("") == []
    assert fuc.enabled_inactive("#TRIGGERS \n\n\nnonsense") == []


def test_report_marks_new_units_urgent_and_known_ones_actionable():
    node = {"apt_security": 0, "apt_other": 0, "failed_units": [], "enabled_inactive": ["sonarr.service", "old.service"]}
    prev = {"nodes": {"thoth": {"enabled_inactive": ["old.service"]}}}
    _, urgent, action = fuc.build_report({"thoth": node}, {"latest": {}}, [], prev)
    assert any("NEW enabled-but-inactive" in u and "sonarr.service" in u for u in urgent)
    assert not any("old.service" in u for u in urgent)
    assert any("old.service enabled but inactive" in a for a in action)


def test_probe_failure_is_reported_not_silently_clean():
    node = {"apt_security": 0, "apt_other": 0, "failed_units": [], "enabled_inactive_err": "rc=255"}
    rep, _, action = fuc.build_report({"n": node}, {"latest": {}}, [], {})
    assert "PROBE-FAILED" in rep and any("probe failed" in a for a in action)
