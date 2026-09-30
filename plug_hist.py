#!/usr/bin/env python3
"""plug_hist - shared read access to the fleet smart-plug wattage corpus.

CORPUS (verified live 2026-08-14, wq#591): VictoriaMetrics on Bes
(100.110.156.91), -retentionPeriod=3y, metric homeassistant_sensor_power_w,
13 live series at 15s cadence, label node=<fleet node>. Reached from Lumen
through lumen-powerdash-vmtunnel (127.0.0.1:18428).

There is deliberately NO second store. /home/lumen/fleet/power-ab/power.db is
legacy A/B scratch (6 rows, 2026-07-16) and stays that way; plug-snapshot's
overwrite of glance/assets/plugs.json is a publish path, not a corpus.
The wq#591 failure was never missing data - it was never querying it.
"""
import calendar, json, os, re, sys, time, urllib.parse, urllib.request

VM      = os.environ.get("PLUG_VM_URL", "http://127.0.0.1:18428")
METRIC  = "homeassistant_sensor_power_w"
# Canonical fleet selector. Verified 2026-08-14: exactly 13 live series at 15s.
# node="" and node="p7-unknown" are DEAD relabel ghosts from the 2026-07-20 vmagent
# relabel (45 and 683 samples in 30d, zero in the last hour) - excluding them is not
# hiding data, and corpus_health still alarms if a real plug stops reporting.
GHOSTS  = ('', 'p7-unknown')
FLEET   = METRIC + '{node!="",node!="p7-unknown"}'
TIMEOUT = int(os.environ.get("PLUG_VM_TIMEOUT", "25"))


class CorpusError(RuntimeError):
    """Raised when the corpus cannot be consulted. Never swallow this silently:
    going blind is itself a finding (see plug-findings CORPUS_BLIND)."""


def _get(path, params):
    url = VM + path + "?" + urllib.parse.urlencode(params, doseq=True)
    try:
        with urllib.request.urlopen(url, timeout=TIMEOUT) as r:
            d = json.load(r)
    except Exception as e:
        raise CorpusError("VM unreachable at %s: %s" % (VM, e)) from e
    if d.get("status") != "success":
        raise CorpusError("VM error: %s" % json.dumps(d)[:300])
    return d["data"]


def query(promql, ts=None):
    p = {"query": promql}
    if ts:
        p["time"] = ts
    return _get("/api/v1/query", p)["result"]


def query_range(promql, start, end, step):
    return _get("/api/v1/query_range", {"query": promql, "start": start,
                                        "end": end, "step": step})["result"]


def by_node(promql):
    """-> {node: float}. Series with no node label are keyed by their entity id."""
    out = {}
    for r in query(promql):
        m = r["metric"]
        key = m.get("node") or m.get("entity", "?")
        try:
            out[key] = float(r["value"][1])
        except (TypeError, ValueError, IndexError):
            continue
    return out


def sel(node=None):
    return METRIC + ('{node="%s"}' % node if node else "")


def nodes(window="10m"):
    """Nodes with a live plug series. Unlabeled series (p13/p14 personal PCs) included by entity."""
    return sorted(by_node("count_over_time(%s[%s])" % (METRIC, window)))


def meta():
    out = {}
    for r in query("node_power_meta"):
        m = r["metric"]
        out[m.get("node", "?")] = {k: v for k, v in m.items() if k != "__name__"}
    return out


def pct(q, window, node=None, bucket="5m"):
    """Percentile q of bucket-mean wattage across window, per node."""
    return by_node("quantile_over_time(%s, avg_over_time(%s[%s])[%s:%s])"
                   % (q, sel(node), bucket, window, bucket))


def mean(window, node=None):
    return by_node("avg_over_time(%s[%s])" % (sel(node), window))


def maxw(window, node=None):
    return by_node("max_over_time(%s[%s])" % (sel(node), window))


def n_samples(window, node=None, selector=None):
    expr = selector or sel(node)
    return {k: int(v) for k, v in by_node("count_over_time(%s[%s])" % (expr, window)).items()}


def fleet_nodes(window="10m"):
    """Fleet nodes reporting a plug series right now (ghost series excluded)."""
    return sorted(n_samples(window, selector=FLEET))


def mapped_nodes():
    """Nodes that node_power_meta says own a real plug (P<n>/E<n>). khepri is mapped to
    P1 but has been POWERED DOWN since 2026-07-16, so it has no series and is not a gap."""
    out = {}
    for n, m in meta().items():
        plug = (m.get("plug") or "-").strip()
        if re.match(r"^[PE]\d+", plug):
            out[n] = plug
    return out


def corpus_health(stale_min=10, span="30d"):
    """(ok, dict). Anything built on this corpus publishes size + freshness with its
    result: a clean report from an empty corpus is the audit failure this fleet keeps
    repeating (FAILURE_ARCHAEOLOGY fq F1). Positive control lives in plug-findings."""
    fresh  = n_samples("%dm" % stale_min, selector=FLEET)
    span_n = n_samples(span, selector=FLEET)
    mapped = mapped_nodes()
    dropped = sorted(n for n in span_n if n not in fresh)                 # had history, went quiet
    unseen  = sorted(n for n in mapped if n not in span_n and n not in fresh)  # mapped, never seen
    rep = {"vm": VM, "metric": METRIC, "window": span,
           "series_live": len(fresh), "samples_%s" % span: sum(span_n.values()),
           "nodes_live": sorted(fresh), "dropped": dropped,
           "mapped_no_data": unseen, "checked_at": int(time.time())}
    return (len(fresh) > 0 and not dropped), rep


def _iso(ts):
    return None if ts is None else time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(ts))


# ---------------------------------------------------------------------------
# Attribution (wq#600). The series are already keyed by PLUG: every sample carries the
# stable `entity` label (sensor.pN_current_consumption). `node` is a vmagent relabel that
# is static while boxes are re-plugged (fleet.power_policy), so it is only ever an
# observation. Who PHYSICALLY held a plug, and when the VM label disagreed with that, is
# carried by the wq#599 ledger; query it here rather than trusting the label.
LABEL_EVENTS = os.environ.get("PLUG_LABEL_EVENTS", "/home/lumen/fleet/power-ab/label_events.json")


def _epoch(s):
    """'2026-08-15T00:59:35Z' -> UTC epoch seconds; None if missing or unparseable."""
    if not s:
        return None
    try:
        return calendar.timegm(time.strptime(str(s)[:19], "%Y-%m-%dT%H:%M:%S"))
    except ValueError:
        return None


def label_events(path=None):
    """Ledger events, [] when the file is absent or unreadable (no known cutovers is not
    an error; a plug with no events is simply unattributed)."""
    try:
        with open(path or LABEL_EVENTS) as f:
            return list(json.load(f).get("events") or [])
    except (OSError, ValueError, AttributeError):
        return []


def _plug_events(plug, events):
    """[(physical_move_epoch, label_cutover_epoch, from_node, to_node)] for one plug, in
    time order. physical_move_ts is when the box really moved; cutover_ts is when the VM
    label was changed, and they differ (wq#599: 22:52Z vs 00:59:35Z). A missing
    physical_move_ts falls back to the cutover, i.e. no known mislabel window."""
    rows = []
    for ev in events:
        if str(ev.get("plug") or "").upper() != str(plug).upper():
            continue
        cut = _epoch(ev.get("cutover_ts"))
        if cut is None:
            continue
        rows.append((_epoch(ev.get("physical_move_ts")) or cut, cut,
                     ev.get("from_node"), ev.get("to_node")))
    rows.sort()
    return rows


def attribution(plug, events=None):
    """Time-bounded PHYSICAL occupant of `plug` from the ledger:
    [{"from": epoch|None, "to": epoch|None, "node": name}], None = open end.
    [] when the ledger has no history for the plug (unattributed, not 'no owner')."""
    rows = _plug_events(plug, label_events() if events is None else events)
    if not rows:
        return []
    segs, start, node = [], None, rows[0][2]
    for move, _cut, _frm, to in rows:
        segs.append({"from": start, "to": move, "node": node})
        start, node = move, to
    segs.append({"from": start, "to": None, "node": node})
    return segs


def node_at(plug, ts, events=None):
    """Physical occupant of `plug` at epoch `ts`, or None when unattributed."""
    for s in attribution(plug, events):
        if (s["from"] is None or ts >= s["from"]) and (s["to"] is None or ts < s["to"]):
            return s["node"]
    return None


def mislabelled(plug, events=None):
    """Windows where the VM `node` label named a different machine than the one on the
    plug: [{"from", "to", "physical", "labelled"}]. Samples in these windows must not be
    read under the label. Covers both orders (box moved first, or label changed first)."""
    out = []
    for move, cut, frm, to in _plug_events(plug, label_events() if events is None else events):
        if move < cut:
            out.append({"from": move, "to": cut, "physical": to, "labelled": frm})
        elif cut < move:
            out.append({"from": cut, "to": move, "physical": frm, "labelled": to})
    return out


def _self_test():
    """Planted known-positive: the real wq#599 P3 episode must be reproduced, and a plug
    with an aligned cutover must report no mislabel window, or the tool is lying."""
    t = _epoch
    ev = [{"plug": "P3", "from_node": "hathor", "to_node": "khepri",
           "cutover_ts": "2026-08-15T00:59:35Z", "physical_move_ts": "2026-08-14T22:52:00Z"},
          {"plug": "P3", "from_node": "khepri", "to_node": "hathor",
           "cutover_ts": "2026-08-15T01:28:32Z", "physical_move_ts": "2026-08-15T01:22:00Z"},
          {"plug": "P5", "from_node": "isis", "to_node": "sia",
           "cutover_ts": "2026-09-01T00:00:00Z"}]
    m = mislabelled("P3", ev)
    checks = {
        "before first move -> hathor": node_at("P3", t("2026-08-14T12:00:00Z"), ev) == "hathor",
        "inside label lag -> khepri (label still said hathor)":
            node_at("P3", t("2026-08-14T23:30:00Z"), ev) == "khepri",
        "after return -> hathor": node_at("P3", t("2026-08-15T02:00:00Z"), ev) == "hathor",
        "plug with no history -> None": node_at("P9", t("2026-08-15T02:00:00Z"), ev) is None,
        "P3 has two mislabel windows": len(m) == 2,
        "window 1 = khepri physically, labelled hathor":
            len(m) > 0
            and (m[0]["physical"], m[0]["labelled"]) == ("khepri", "hathor")
            and (m[0]["from"], m[0]["to"]) == (t("2026-08-14T22:52:00Z"), t("2026-08-15T00:59:35Z")),
        "window 2 = hathor physically, labelled khepri":
            len(m) > 1 and (m[1]["physical"], m[1]["labelled"]) == ("hathor", "khepri"),
        "aligned cutover has no mislabel window": mislabelled("P5", ev) == [],
        "absent ledger is not an error": label_events("/nonexistent/wq600") == [],
    }
    bad = [k for k, ok in checks.items() if not ok]
    for k, ok in checks.items():
        print("%-58s %s" % (k, "ok" if ok else "FAIL"))
    print("SELF-TEST %s" % ("FAILED: %s" % bad if bad else "PASSED (%d checks)" % len(checks)))
    return 1 if bad else 0


def _main(argv):
    cmd = argv[1] if len(argv) > 1 else "health"
    if cmd == "health":
        ok, rep = corpus_health()
        print(json.dumps(rep, indent=2))
        return 0 if ok else 1
    if cmd == "nodes":
        for n in fleet_nodes():
            print(n)
        return 0
    if cmd == "meta":
        print(json.dumps(meta(), indent=2))
        return 0
    if cmd == "attribution" and len(argv) > 2:
        segs = attribution(argv[2])
        if not segs:
            print("%s: no ledger history (unattributed)" % argv[2])
        for s in segs:
            print("%s  %s -> %s" % (s["node"], _iso(s["from"]) or "(start)", _iso(s["to"]) or "(now)"))
        for w in mislabelled(argv[2]):
            print("MISLABELLED %s -> %s: plug held %s, VM label said %s"
                  % (_iso(w["from"]), _iso(w["to"]), w["physical"], w["labelled"]))
        return 0
    if cmd == "self-test":
        return _self_test()
    if cmd == "raw":
        print(json.dumps(query(argv[2]), indent=2))
        return 0
    print(__doc__ + "\nusage: plug_hist.py [health|nodes|meta|raw <promql>|attribution <plug>|self-test]",
          file=sys.stderr)
    return 2


if __name__ == "__main__":
    try:
        sys.exit(_main(sys.argv))
    except CorpusError as e:
        print("CORPUS UNREACHABLE: %s" % e, file=sys.stderr)
        sys.exit(3)
