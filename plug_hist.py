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
import json, os, re, sys, time, urllib.parse, urllib.request

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
    if cmd == "raw":
        print(json.dumps(query(argv[2]), indent=2))
        return 0
    print(__doc__ + "\nusage: plug_hist.py [health|nodes|meta|raw <promql>]", file=sys.stderr)
    return 2


if __name__ == "__main__":
    try:
        sys.exit(_main(sys.argv))
    except CorpusError as e:
        print("CORPUS UNREACHABLE: %s" % e, file=sys.stderr)
        sys.exit(3)
