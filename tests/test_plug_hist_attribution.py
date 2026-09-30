"""wq#600: plug_hist attribution layer. The VM `node` label is an observation; the wq#599
ledger says who physically held the plug and when the two disagreed. Pure functions over
an in-memory events list: no VictoriaMetrics, no fleet state."""
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, ".."))
import plug_hist as ph  # noqa: E402

T = ph._epoch


def ev(plug, frm, to, cut, move=None):
    e = {"plug": plug, "from_node": frm, "to_node": to, "cutover_ts": cut}
    if move:
        e["physical_move_ts"] = move
    return e


P3 = [ev("P3", "hathor", "khepri", "2026-08-15T00:59:35Z", "2026-08-14T22:52:00Z"),
      ev("P3", "khepri", "hathor", "2026-08-15T01:28:32Z", "2026-08-15T01:22:00Z")]


def test_builtin_self_test_passes(capsys):
    assert ph._self_test() == 0
    assert "PASSED" in capsys.readouterr().out


def test_self_test_goes_red_when_the_lag_window_is_ignored(monkeypatch):
    """Positive control: if physical_move_ts is dropped, the tool must notice."""
    real = ph._plug_events

    def blind(plug, events):
        return [(cut, cut, f, t) for _m, cut, f, t in real(plug, events)]
    monkeypatch.setattr(ph, "_plug_events", blind)
    assert ph._self_test() == 1


def test_segments_are_contiguous_and_open_ended():
    segs = ph.attribution("P3", P3)
    assert [s["node"] for s in segs] == ["hathor", "khepri", "hathor"]
    assert segs[0]["from"] is None and segs[-1]["to"] is None
    assert segs[0]["to"] == segs[1]["from"] and segs[1]["to"] == segs[2]["from"]


def test_lookup_is_case_insensitive_and_unknown_plug_is_unattributed():
    assert ph.node_at("p3", T("2026-08-14T23:00:00Z"), P3) == "khepri"
    assert ph.attribution("P9", P3) == []
    assert ph.node_at("P9", 0, P3) is None


def test_label_changed_before_the_box_moved():
    """Reverse order: cutover 10:00, physical move 10:30 -> 10:00-10:30 the label says
    the NEW machine while the OLD one is still on the plug."""
    e = [ev("P4", "maat", "sobek", "2026-09-10T10:00:00Z", "2026-09-10T10:30:00Z")]
    (w,) = ph.mislabelled("P4", e)
    assert (w["physical"], w["labelled"]) == ("maat", "sobek")
    assert (w["from"], w["to"]) == (T("2026-09-10T10:00:00Z"), T("2026-09-10T10:30:00Z"))
    assert ph.node_at("P4", T("2026-09-10T10:15:00Z"), e) == "maat"
    assert ph.node_at("P4", T("2026-09-10T10:45:00Z"), e) == "sobek"


def test_unsorted_events_and_bad_timestamps_do_not_crash():
    shuffled = [P3[1], P3[0], ev("P3", "x", "y", "not-a-time")]
    assert [s["node"] for s in ph.attribution("P3", shuffled)] == ["hathor", "khepri", "hathor"]


def test_missing_ledger_is_empty_not_an_error(tmp_path):
    assert ph.label_events(str(tmp_path / "nope.json")) == []
    bad = tmp_path / "bad.json"
    bad.write_text("{not json")
    assert ph.label_events(str(bad)) == []


def test_real_ledger_reproduces_the_p3_episode():
    if not os.path.exists(ph.LABEL_EVENTS):
        return
    w = ph.mislabelled("P3")
    assert any(x["physical"] == "khepri" and x["labelled"] == "hathor" for x in w)
