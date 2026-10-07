"""wq#3516: label-cutover truth must reach the two consumers (plug_baseline, plug-findings).

wq#600 shipped plug_hist.mislabelled(); wq#599 shipped the baseline freeze. Two holes remained:

 1. detect() only honoured base[n]["baseline_frozen"]. baselines.json is written by a timer
    and is NOT re-frozen when a re-plug lands afterwards, while cur (24h, computed live) IS
    frozen. A stored pre-cutover baseline vs a frozen current window = one machine's idle
    floor compared with another's -> fabricated IDLE_DRIFT / TOP_MOVER.
 2. _apply_label_events froze only when the cutover timestamp sat inside the window. A
    label-FIRST event (label changed, box moved later) with cutover < window start < move
    leaves a mislabel window inside the data that was never flagged.

Pure functions only: no VictoriaMetrics, no fleet files."""
import importlib.machinery
import importlib.util
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
BIN = os.path.abspath(os.path.join(HERE, ".."))
sys.path.insert(0, BIN)
import plug_hist as ph  # noqa: E402  (must be imported from THIS tree before plug-findings
#                         inserts /home/lumen/bin on sys.path and caches the primary copy)
import plug_baseline as pb  # noqa: E402

_loader = importlib.machinery.SourceFileLoader("plug_findings_under_test",
                                               os.path.join(BIN, "plug-findings"))
_spec = importlib.util.spec_from_loader(_loader.name, _loader)
pf = importlib.util.module_from_spec(_spec)
_loader.exec_module(pf)

T = ph._epoch


def _node(idle, mean, frozen=False):
    d = {"idle_w": idle, "mean_w": mean, "duty_frac": 0.5, "kwh_day": 0.5, "plug": "P3"}
    if frozen:
        d["baseline_frozen"] = True
    return d


def _kinds(cur, base):
    return {f["kind"] for f in pf.detect(cur, base, live={}, now=0.0, expl={})}


# ---------- detect(): the current window's freeze must count ----------

def test_control_unfrozen_both_sides_does_report_drift_and_mover():
    """Sensitivity control: if this stops reporting, the freeze tests below prove nothing."""
    k = _kinds({"hathor": _node(18.0, 20.0)}, {"hathor": _node(10.0, 11.0)})
    assert {"IDLE_DRIFT", "TOP_MOVER"} <= k


def test_frozen_current_window_vs_stored_unfrozen_baseline_yields_no_comparison():
    k = _kinds({"hathor": _node(18.0, 20.0, frozen=True)}, {"hathor": _node(10.0, 11.0)})
    assert "IDLE_DRIFT" not in k and "TOP_MOVER" not in k


def test_frozen_baseline_still_suppresses_comparison_regression():
    k = _kinds({"hathor": _node(18.0, 20.0)}, {"hathor": _node(10.0, 11.0, frozen=True)})
    assert "IDLE_DRIFT" not in k and "TOP_MOVER" not in k


def test_frozen_current_does_not_hide_an_absolute_idle_burn():
    """IDLE_BURN is absolute (no yardstick), so a freeze must not mute it."""
    cur = {"hathor": dict(_node(40.0, 41.0, frozen=True), duty_frac=0.02, kwh_day=1.0)}
    assert "IDLE_BURN" in _kinds(cur, {"hathor": _node(10.0, 11.0)})


# ---------- _apply_label_events(): freeze on mislabel-window overlap ----------

def _apply(monkeypatch, events, start, end, names=("maat", "sobek")):
    monkeypatch.setattr(pb, "_label_events", lambda: events)
    nodes = {n: {} for n in names}
    pb._apply_label_events(nodes, T(start), T(end))
    return nodes


LABEL_FIRST = [{"plug": "P4", "from_node": "maat", "to_node": "sobek",
                "cutover_ts": "2026-09-10T10:00:00Z", "physical_move_ts": "2026-09-10T10:30:00Z"}]


def test_label_first_event_overlapping_window_start_freezes_both_nodes(monkeypatch):
    """cutover 10:00 < window start 10:15 < move 10:30: 10:15-10:30 is mislabelled data."""
    n = _apply(monkeypatch, LABEL_FIRST, "2026-09-10T10:15:00Z", "2026-09-10T12:00:00Z")
    assert n["maat"].get("baseline_frozen") and n["sobek"].get("baseline_frozen")


def test_label_first_contamination_starts_at_the_cutover_not_the_move(monkeypatch):
    n = _apply(monkeypatch, LABEL_FIRST, "2026-09-10T10:15:00Z", "2026-09-10T12:00:00Z")
    assert n["sobek"]["label_discontinuity"]["contaminated_from"] == "2026-09-10T10:00:00Z"


def test_event_wholly_before_window_does_not_freeze(monkeypatch):
    n = _apply(monkeypatch, LABEL_FIRST, "2026-09-10T11:00:00Z", "2026-09-10T12:00:00Z")
    assert not n["maat"].get("baseline_frozen") and not n["sobek"].get("baseline_frozen")


def test_move_first_event_inside_window_still_freezes_regression(monkeypatch):
    ev = [{"plug": "P3", "from_node": "hathor", "to_node": "khepri",
           "cutover_ts": "2026-08-15T00:59:35Z", "physical_move_ts": "2026-08-14T22:52:00Z"}]
    n = _apply(monkeypatch, ev, "2026-08-14T00:00:00Z", "2026-08-16T00:00:00Z",
               names=("hathor", "khepri"))
    assert n["hathor"].get("baseline_frozen") and n["khepri"].get("baseline_frozen")
    assert n["khepri"]["label_discontinuity"]["contaminated_from"] == "2026-08-14T22:52:00Z"


def test_cutover_only_event_without_physical_move_still_freezes_regression(monkeypatch):
    ev = [{"plug": "P5", "from_node": "isis", "to_node": "sia", "cutover_ts": "2026-09-01T00:00:00Z"}]
    n = _apply(monkeypatch, ev, "2026-08-25T00:00:00Z", "2026-09-05T00:00:00Z", names=("isis", "sia"))
    assert n["isis"].get("baseline_frozen") and n["sia"].get("baseline_frozen")
