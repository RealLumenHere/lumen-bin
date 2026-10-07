#!/usr/bin/env python3
"""plug-baseline - per-node idle and load wattage baselines from measured history.

Source: the existing corpus (VictoriaMetrics on Bes, 15s cadence) via plug_hist.
Nothing is sampled or stored by this tool beyond the derived baseline file.

Every number carries n and window. A baseline without n is an opinion.

  plug-baseline                 # 14d baselines, human table
  plug-baseline --window 7d --json
  plug-baseline --write         # refresh /home/lumen/fleet/power-ab/baselines.json
"""
import argparse, json, os, statistics as st, sys, time
from datetime import datetime, timezone

sys.path.insert(0, "/home/lumen/bin")
import plug_hist as ph

OUT = "/home/lumen/fleet/power-ab/baselines.json"
BUCKET_S = 300
POWERED_MIN_W = 0.5   # below this the plug is off or the node is unplugged (parked)


def _secs(w):
    u = {"m": 60, "h": 3600, "d": 86400}[w[-1]]
    return int(w[:-1]) * u


LABEL_EVENTS = "/home/lumen/fleet/power-ab/label_events.json"


def _label_events():
    """Plug LABEL cutovers (wq#599). Absent file = no known cutovers, not an error."""
    try:
        with open(LABEL_EVENTS) as f:
            return json.load(f).get("events", [])
    except Exception:
        return []


def _parse_ts(s):
    return datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp()


def _mislabel_overlaps(ev, start, end):
    """wq#3516: does this event's wq#600 mislabel window overlap [start, end]?

    A label-FIRST event (label changed, box moved later) with cutover < start < move has
    its cutover outside the window yet leaves mislabelled samples inside it; testing the
    cutover timestamp alone never sees it. plug_hist.mislabelled() is the single
    definition of that window, so ask it instead of re-deriving."""
    try:
        return any(w["from"] < end and w["to"] > start
                   for w in ph.mislabelled(ev.get("plug"), [ev]))
    except Exception:
        return False


def _apply_label_events(nodes, start, end):
    """Mark baselines whose window spans a label cutover.

    A window reaching back past a cutover mixes two machines under one name, and the
    numbers look perfectly healthy — that is the danger — so the contamination is
    stated on the node itself, where the next drift comparison will actually read it.
    BOTH ends of a cutover are contaminated: the old label picked up the new machine's
    watts before the relabel, and the new label inherits the plug's earlier history.
    Contamination clears on its own once the cutover ages out of the window.

    A plug can change hands more than once (P3 went hathor -> khepri -> hathor inside
    three hours), so events are applied in chronological order and the last one decides
    who currently owns the label. "Contaminated" and "retired" are NOT the same claim:
    a node can be the current, correct owner and still have an unusable baseline.
    """
    evs = []
    for ev in _label_events():
        try:
            evs.append((_parse_ts(ev["cutover_ts"]),
                        _parse_ts(ev.get("physical_move_ts") or ev["cutover_ts"]), ev))
        except Exception:
            continue
    owner = {}
    for cut, moved, ev in sorted(evs):
        if not (start <= cut <= end or _mislabel_overlaps(ev, start, end)):
            continue
        old, new = ev.get("from_node"), ev.get("to_node")
        owner[ev.get("plug")] = new
        for side, other, role in ((old, new, "lost"), (new, old, "gained")):
            if side not in nodes:
                continue
            d = nodes[side]
            d["baseline_frozen"] = True          # window spans a cutover: do not compare
            d.setdefault("cutover_history", []).append(
                {"at": ev["cutover_ts"], "plug": ev.get("plug"), "role": role,
                 "other_node": other})
            d["label_discontinuity"] = {
                "at": ev["cutover_ts"], "plug": ev.get("plug"), "other_node": other,
                # contamination opens at whichever came FIRST: the box moving (move-first,
                # the normal case) or the label changing (label-first, wq#3516).
                "contaminated_from": (ev["cutover_ts"] if cut < moved else
                                      (ev.get("physical_move_ts") or ev["cutover_ts"])),
                "warning": "baseline window spans a plug-label cutover on %s (%s %s at %s). "
                           "It mixes this node's watts with %s's. Do NOT use for drift "
                           "comparison until the cutover ages out of the window."
                           % (ev.get("plug"), side, role, ev["cutover_ts"], other)}
            if moved > start:
                d["contaminated_frac"] = round((end - moved) / (end - start), 3)
        if new in nodes:
            nodes[new]["baseline_starts_at"] = ev["cutover_ts"]
    # label_retired is a separate, weaker claim than contaminated: only true if the node
    # does not currently own the plug it lost.
    for plug, cur_owner in owner.items():
        for n, d in nodes.items():
            if d.get("cutover_history") and n != cur_owner and \
               any(h["plug"] == plug for h in d["cutover_history"]):
                d["label_retired_for"] = plug


def compute(window="14d", bucket_s=BUCKET_S):
    end = int(time.time())
    start = end - _secs(window)
    series = ph.query_range("avg_over_time(%s[%ds])" % (ph.FLEET, bucket_s),
                            start, end, bucket_s)
    raw_n = ph.n_samples(window, selector=ph.FLEET)
    meta = ph.meta()
    nodes = {}
    for r in series:
        node = r["metric"].get("node")
        if not node:
            continue
        pts = [(int(t), float(v)) for t, v in r["values"]]
        all_vals = sorted(v for _, v in pts)
        # PARKED-NODE CORRECTION (Frank 2026-08-14): unused boxes are deliberately
        # UNPLUGGED — the circuit cannot carry every PC plugged in with wake-on-AC-
        # recovery armed. A plug reading ~0W is "not connected", not "idle at 0W".
        # Percentiles over all buckets therefore report idle_w=0.0 for any node parked
        # >10% of the window (hathor and gameboy-pc read 0.0 idle on the 14d window
        # before this fix), which silences IDLE_BURN and fakes an IDLE_DRIFT win the
        # moment it is plugged back in. Idle/median/load are powered-bucket statistics.
        vals = sorted(v for v in all_vals if v > POWERED_MIN_W)
        unpowered = len(all_vals) - len(vals)
        if len(vals) < 12:                      # < 1h POWERED: refuse to baseline
            nodes[node] = {"insufficient_data": True, "n_buckets": len(all_vals),
                           "n_powered_buckets": len(vals),
                           "unpowered_frac": round(unpowered / max(len(all_vals), 1), 3),
                           "parked": True,
                           # last_w is carried even here: the liveness cross-check
                           # (wq#599) needs "what is this plug drawing NOW" for nodes
                           # that have too little powered history to baseline.
                           "last_w": round(pts[-1][1], 1) if pts else None,
                           "last_ts": datetime.fromtimestamp(pts[-1][0], timezone.utc)
                                      .isoformat(timespec="seconds") if pts else None}
            continue

        def q(pr):
            return round(vals[min(len(vals) - 1, int(pr * (len(vals) - 1)))], 1)

        idle, med, load = q(0.10), q(0.50), q(0.95)
        thr = idle + max(3.0, 0.15 * max(load - idle, 0))
        duty = sum(1 for v in vals if v > thr) / len(vals)
        mean_w = round(sum(vals) / len(vals), 1)
        nodes[node] = {
            "idle_w": idle, "p25_w": q(0.25), "median_w": med, "p75_w": q(0.75),
            "load_w": load, "max_w": round(vals[-1], 1), "mean_w": mean_w,
            "swing_w": round(load - idle, 1), "duty_frac": round(duty, 3),
            "kwh_day": round((sum(all_vals) / len(all_vals)) * 24 / 1000.0, 2),
            "n_buckets": len(all_vals),
            "n_powered_buckets": len(vals),
            "unpowered_frac": round(unpowered / len(all_vals), 3),
            "parked": unpowered / len(all_vals) > 0.10, "n_raw_samples": raw_n.get(node, 0),
            # CURRENT draw, not a window statistic (wq#599). The liveness cross-check
            # asks "is this plug drawing power right now while its labelled node is
            # unreachable" — a window mean cannot answer that: a node powered down two
            # hours ago still has a high mean, and a plug that only just started drawing
            # still has a low one.
            "last_w": round(pts[-1][1], 1),
            "last_ts": datetime.fromtimestamp(pts[-1][0], timezone.utc).isoformat(timespec="seconds"),
            "first": datetime.fromtimestamp(pts[0][0], timezone.utc).isoformat(timespec="seconds"),
            "last": datetime.fromtimestamp(pts[-1][0], timezone.utc).isoformat(timespec="seconds"),
            "plug": meta.get(node, {}).get("plug", "?"),
            "role": meta.get(node, {}).get("role", "-"),
        }
    ok, health = ph.corpus_health(span=window)
    _apply_label_events(nodes, start, end)
    return {"generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "window": window, "bucket_s": bucket_s, "source": ph.VM,
            "metric": ph.METRIC, "corpus_ok": ok, "corpus": health,
            "label_events": _label_events(),
            "definitions": {"idle_w": "p10 of 5m-mean wattage over POWERED buckets (>%.1fW)" % POWERED_MIN_W,
                            "load_w": "p95 of 5m-mean wattage over POWERED buckets",
                            "duty_frac": "fraction of POWERED buckets above idle + max(3W, 15% of swing)",
                            "unpowered_frac": "fraction of buckets at/below %.1fW — node unplugged or plug off; parked boxes are unplugged by policy (circuit budget)" % POWERED_MIN_W,
                            "kwh_day": "mean over ALL buckets incl. unpowered — real energy, not the powered-only floor"},
            "nodes": nodes}


def table(b):
    L = ["baselines  window=%s  bucket=%ds  source=%s  corpus_ok=%s" %
         (b["window"], b["bucket_s"], b["source"], b["corpus_ok"]),
         "%-12s %7s %7s %7s %7s %7s %6s %8s %9s" %
         ("node", "idle", "med", "load", "max", "swing", "duty", "kWh/day", "n(raw)")]
    for n, d in sorted(b["nodes"].items(), key=lambda kv: -kv[1].get("mean_w", 0)):
        if d.get("insufficient_data"):
            L.append("%-12s  INSUFFICIENT DATA (n_buckets=%d)" % (n, d["n_buckets"]))
            continue
        mark = "  PARKED %.0f%% unplugged" % (d["unpowered_frac"] * 100) if d.get("parked") else ""
        if d.get("baseline_frozen"):
            mark += "  BASELINE CONTAMINATED by label cutover - DO NOT COMPARE"
            if d.get("label_retired_for"):
                mark += " (label retired for %s)" % d["label_retired_for"]
        elif d.get("baseline_starts_at"):
            mark += "  NEW LABEL since %s" % d["baseline_starts_at"]
        L.append("%-12s %7.1f %7.1f %7.1f %7.1f %7.1f %5.0f%% %8.2f %9d%s" %
                 (n, d["idle_w"], d["median_w"], d["load_w"], d["max_w"], d["swing_w"],
                  d["duty_frac"] * 100, d["kwh_day"], d["n_raw_samples"], mark))
    # idle/med/load are POWERED-only stats, so summing mean_w would over-count parked
    # boxes as if they were plugged in all window. Real energy comes from kWh/day,
    # which is computed over every bucket including the unpowered ones.
    kwh = sum(d.get("kwh_day", 0) for d in b["nodes"].values())
    L.append("fleet energy over window: %.1f kWh/day  (%.0f W average, unpowered time included)"
             % (kwh, kwh * 1000.0 / 24))
    parked = [n for n, d in b["nodes"].items() if d.get("parked")]
    if parked:
        L.append("parked (unplugged part of the window, idle/load are powered-only): %s"
                 % ", ".join(sorted(parked)))
    return "\n".join(L)


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--window", default="14d")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--write", action="store_true")
    a = ap.parse_args()
    try:
        b = compute(a.window)
    except ph.CorpusError as e:
        print("CORPUS UNREACHABLE - no baseline computed: %s" % e, file=sys.stderr)
        return 3
    if a.write:
        if os.path.exists(OUT):
            os.replace(OUT, OUT + ".bak")
        tmp = OUT + ".tmp"
        with open(tmp, "w") as f:
            json.dump(b, f, indent=2)
        os.replace(tmp, OUT)
        print("wrote %s (prev -> %s.bak)" % (OUT, OUT))
    print(json.dumps(b, indent=2) if a.json else table(b))
    return 0


if __name__ == "__main__":
    sys.exit(main())
