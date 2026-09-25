#!/usr/bin/env python3
"""fleet_spof_watch.py — the watchdogs FLEET_SERVICE_ATLAS §5.1 left uncovered (wq#1704).

Four subcommands, one per gap the atlas named as "dies silently, nobody notices":

  daemons    §5 #5 / §5.1 item 7 — alert-path daemons with no external check:
             lumen-gatus-ntfy-relay (:8784), ollama (:11434), lumen-guardian,
             and the two subscribers. Restart= is not a watchdog: a wedged
             daemon stays ActiveState=active forever.
  roundtrip  §5 #5 / §5.1 item 2 — proves publish -> subscribe -> persist for
             lumen-alert-queue, not just publish. The deadman beat (wq#1039)
             covers the publish half only, and on its own topic
             (alert-deadman-beat.sh:11 HB_TOPIC=lumen-heartbeat), which
             lumen-alert-queue does not subscribe to.
  restarts   §5 #7 / §5.1 item 3 — Restart=always with no StartLimitBurst turns
             a permanently broken dependency into an infinite quiet loop.
  timers     §5 #4 — wadjet-ha-monitor (Frank's password manager) and the
             backup chain have no dead-man of their own. Asserts ran-recently
             (timer LastTriggerUSec) and succeeded (service Result), which are
             different assertions (ISSUE-LOG #44).

Exit-code contract (wq#1657's preferred branch, the load-watch template): a
finding is NOT a unit failure — always exit 0 and report via ntfy + aq. A
nonzero exit means this guard itself crashed, so OnFailure= means what it says.

Findings edge-clear: when a target recovers, its open aq row is resolved, so
this guard cannot contribute to the §5 #2 triage saturation it was written for.

Env overrides (tests use them): SPOF_AQ_DIR, SPOF_STATE_DIR, SPOF_NTFY_BIN,
SPOF_RESTART_DELTA, SPOF_RESTART_WINDOW_S, SPOF_RT_TOPIC, SPOF_RT_DEADLINE_S.
"""
import argparse
import json
import os
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
import uuid

SOURCE = "spof-watch"
AQ_DIR = os.environ.get("SPOF_AQ_DIR", "/home/lumen/agents/alert-queue")
STATE_DIR = os.environ.get("SPOF_STATE_DIR", "/home/lumen/state/spof-watch")
NTFY_BIN = os.environ.get("SPOF_NTFY_BIN", "/home/lumen/bin/ntfy-send")
SYSTEMCTL = os.environ.get("SPOF_SYSTEMCTL", "/usr/bin/systemctl")

# --- daemons (atlas §5 #5). port=None means "no socket to probe, unit state
# and MainPID are the only signal". http= turns the probe into a real request.
DAEMONS = [
    {"unit": "lumen-gatus-ntfy-relay.service", "port": 8784,
     "silent": "Gatus alerts vanish: model rung outages become invisible"},
    {"unit": "ollama.service", "port": 11434, "http": "/api/version",
     "silent": "local model fallback gone; recall/rerank degrade quietly"},
    {"unit": "lumen-guardian.service", "port": None,
     "silent": "hardware thermal-threshold shutdown protection is off"},
    {"unit": "lumen-alert-queue.service", "port": None,
     "silent": "aq stops filling; every alert becomes a phone-only event"},
    {"unit": "lumen-command-receiver.service", "port": None,
     "silent": "phone commands ignored with no error anywhere"},
]

# --- timers (atlas §5 #4 / §4.12 / §4.13). max_age_s is generous-2x of the
# real cadence read off `systemctl list-timers` on 2026-09-11; a tighter bound
# would false-positive against this guard's own 10-minute cadence.
TIMERS = [
    {"timer": "wadjet-ha-monitor.timer", "max_age_s": 300,
     "why": "vault primary unwatched: vault.lumennode.uk down needs a human"},
    {"timer": "lumen-backup-critical.timer", "max_age_s": 7200,
     "why": "hourly critical backup stopped"},
    {"timer": "lumen-backup-important.timer", "max_age_s": 108000,
     "why": "daily important backup stopped"},
    {"timer": "lumen-backup-offsite.timer", "max_age_s": 108000,
     "why": "daily offsite copy stopped: no restore path off this box"},
    {"timer": "lumen-backup-prune.timer", "max_age_s": 864000,
     "why": "weekly prune stopped: backup volume grows unbounded"},
    {"timer": "vw-sobek-datasync.timer", "max_age_s": 7200,
     "why": "vault replica drifting from primary"},
    {"timer": "vw-bes-datasync.timer", "max_age_s": 7200,
     "why": "vault replica drifting from primary"},
]

RESTART_DELTA = int(os.environ.get("SPOF_RESTART_DELTA", "5"))
RESTART_WINDOW_S = int(os.environ.get("SPOF_RESTART_WINDOW_S", "86400"))
RT_TOPIC = os.environ.get("SPOF_RT_TOPIC", "lumen-updates")
RT_DEADLINE_S = int(os.environ.get("SPOF_RT_DEADLINE_S", "120"))
RT_POLL_S = int(os.environ.get("SPOF_RT_POLL_S", "5"))


# --------------------------------------------------------------- plumbing ---
def aq_mod():
    sys.path.insert(0, AQ_DIR)
    import aq
    return aq


def ntfy(title, msg, prio):
    try:
        subprocess.run([NTFY_BIN, "-p", prio, "-T", title, msg],
                       capture_output=True, text=True, timeout=15)
    except Exception:
        pass  # the alert path being down is the thing we are reporting


def raise_finding(key, title, msg, prio="high"):
    aq = aq_mod()
    aq.ingest(source=SOURCE, key=key, title=title, message=msg, priority=prio)
    ntfy(title, msg, prio)
    print(f"FINDING {key}: {title}")


def clear_finding(key, note):
    """Edge-clear: resolve any open row for this key. Returns rows resolved."""
    aq = aq_mod()
    c = aq.db()
    ids = [r[0] for r in c.execute(
        "SELECT id FROM alerts WHERE source=? AND key=? AND status='open'",
        (SOURCE, key)).fetchall()]
    for rid in ids:
        c.execute("UPDATE alerts SET status='resolved', note=?, resolved_ts=? "
                  "WHERE id=?", (note, aq.now(), rid))
    c.commit()
    c.close()
    if ids:
        print(f"CLEARED {key} ({len(ids)} row(s))")
    return len(ids)


def unit_props(units, props):
    """Batch `systemctl show`. Returns {unit_id: {prop: value}}."""
    if not units:
        return {}
    cmd = [SYSTEMCTL, "show", "--no-pager",
           "--property=" + ",".join(["Id"] + props),
           "--timestamp=unix"] + list(units)
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
    out = {}
    for block in r.stdout.split("\n\n"):
        rec = {}
        for line in block.strip().splitlines():
            if "=" in line:
                k, v = line.split("=", 1)
                rec[k] = v
        if rec.get("Id"):
            out[rec["Id"]] = rec
    return out


def epoch(value):
    """`--timestamp=unix` renders as `@<epoch>`; unset renders empty or @0."""
    v = (value or "").strip().lstrip("@")
    try:
        return int(v)
    except ValueError:
        return 0


def load_state(name):
    try:
        with open(os.path.join(STATE_DIR, name)) as f:
            return json.load(f)
    except Exception:
        return {}


def save_state(name, st):
    os.makedirs(STATE_DIR, exist_ok=True)
    path = os.path.join(STATE_DIR, name)
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(st, f)
    os.replace(tmp, path)


def probe_port(port, http=None, timeout=5):
    """Returns None if the socket answers, else why it did not."""
    if http:
        try:
            urllib.request.urlopen(
                f"http://127.0.0.1:{port}{http}", timeout=timeout).read(1)
            return None
        except urllib.error.HTTPError:
            return None  # it spoke HTTP; it is alive, just not happy
        except Exception as e:
            return f"http://127.0.0.1:{port}{http} -> {type(e).__name__}"
    try:
        socket.create_connection(("127.0.0.1", port), timeout=timeout).close()
        return None
    except Exception as e:
        return f"tcp 127.0.0.1:{port} -> {type(e).__name__}"


# ----------------------------------------------------------------- checks ---
def cmd_daemons(_a):
    props = unit_props([d["unit"] for d in DAEMONS],
                       ["ActiveState", "SubState", "MainPID"])
    bad = 0
    for d in DAEMONS:
        unit, key = d["unit"], f"daemon:{d['unit']}"
        rec = props.get(unit, {})
        state = rec.get("ActiveState", "not-found")
        if state != "active":
            bad += 1
            raise_finding(key, f"SPOF daemon down: {unit}",
                          f"ActiveState={state} SubState={rec.get('SubState')}. "
                          f"Silent consequence: {d['silent']}. (wq#1704, atlas §5 #5)",
                          prio="urgent")
            continue
        if rec.get("MainPID", "0") == "0":
            bad += 1
            raise_finding(key, f"SPOF daemon has no main PID: {unit}",
                          f"ActiveState=active but MainPID=0. "
                          f"Silent consequence: {d['silent']}. (wq#1704)",
                          prio="urgent")
            continue
        if d.get("port"):
            why = probe_port(d["port"], d.get("http"))
            if why:
                bad += 1
                raise_finding(key, f"SPOF daemon wedged: {unit}",
                              f"unit is active but does not answer: {why}. "
                              f"Silent consequence: {d['silent']}. "
                              f"Restart= will not fire for this. (wq#1704)",
                              prio="urgent")
                continue
        clear_finding(key, "spof-watch: daemon healthy again")
    print(f"spof-watch daemons: {len(DAEMONS)} checked, {bad} finding(s)")
    return 0


def cmd_roundtrip(_a):
    """Publish a marker and assert the subscriber persisted it to alerts.db."""
    key = "roundtrip:lumen-alert-queue"
    marker = "spof-rt-" + uuid.uuid4().hex[:12]
    r = subprocess.run(
        [NTFY_BIN, "-p", "min", "-t", RT_TOPIC, "-T", "spof-roundtrip",
         f"roundtrip probe {marker}"],
        capture_output=True, text=True, timeout=30)
    if r.returncode != 0:
        # Publish half is already covered off-fleet by the deadman beat, but if
        # we cannot publish there is nothing to assert about the subscribe half.
        raise_finding(key, "SPOF alert-path: publish failed",
                      f"ntfy-send exited {r.returncode}: "
                      f"{(r.stderr or r.stdout).strip()[:300]} (wq#1704)",
                      prio="urgent")
        print("spof-watch roundtrip: publish failed")
        return 0

    aq = aq_mod()
    like = f"%{marker}%"
    deadline = time.time() + RT_DEADLINE_S
    found = None
    while True:
        c = aq.db()
        row = c.execute(
            "SELECT id FROM alerts WHERE message LIKE ? OR title LIKE ?",
            (like, like)).fetchone()
        c.close()
        if row:
            found = row[0]
            break
        if time.time() >= deadline:
            break
        time.sleep(RT_POLL_S)

    if found is None:
        raise_finding(
            key, "SPOF alert-path: subscribe->persist BROKEN",
            f"published {marker} to topic {RT_TOPIC} but no alerts.db row "
            f"appeared within {RT_DEADLINE_S}s. lumen-alert-queue may be "
            f"active-but-wedged: every alert is now phone-only and aq triage "
            f"is blind. (wq#1704, atlas §5 #5)", prio="urgent")
        print("spof-watch roundtrip: BROKEN")
        return 0

    # Our own synthetic traffic must not add to the §5 #2 open-row pile.
    c = aq.db()
    c.execute("UPDATE alerts SET status='resolved', note=?, resolved_ts=? "
              "WHERE id=?", ("spof-watch roundtrip probe", aq.now(), found))
    c.commit()
    c.close()
    clear_finding(key, "spof-watch: roundtrip healthy again")
    print(f"spof-watch roundtrip: ok (row {found} persisted and resolved)")
    return 0


def fleet_daemon_units():
    r = subprocess.run(
        [SYSTEMCTL, "list-units", "--type=service", "--all",
         "--no-pager", "--no-legend"], capture_output=True, text=True,
        timeout=60)
    units = []
    for line in r.stdout.splitlines():
        for tok in line.split():
            if tok.endswith(".service"):
                units.append(tok.lstrip("● ").strip())
                break
    return units


def cmd_restarts(_a):
    """NRestarts delta per window. Restart=always with no StartLimitBurst
    ceiling climbs forever against a permanently dead dependency."""
    units = fleet_daemon_units()
    props = unit_props(units, ["Restart", "NRestarts"])
    watched = {u: int(p.get("NRestarts") or 0)
               for u, p in props.items()
               if p.get("Restart") in ("always", "on-failure")}
    st = load_state("restarts.json")
    now = int(time.time())
    base = st.get("baseline", {})
    since = st.get("since", 0)
    if now - since > RESTART_WINDOW_S:
        base, since = watched, now  # window rolled: rebase, report next pass

    bad = 0
    for unit, n in sorted(watched.items()):
        key = f"restarts:{unit}"
        prev = base.get(unit)
        delta = n - prev if prev is not None and n >= prev else 0
        if delta >= RESTART_DELTA:
            bad += 1
            raise_finding(
                key, f"Restart loop: {unit}",
                f"NRestarts +{delta} in {(now - since) // 60}min "
                f"(now {n}). Restart= has no StartLimitBurst ceiling here, so "
                f"a permanently broken dependency loops quietly forever. "
                f"(wq#1704, atlas §5 #7)", prio="high")
        else:
            clear_finding(key, "spof-watch: restart rate back to normal")
    save_state("restarts.json",
               {"baseline": base, "since": since, "last": watched})
    print(f"spof-watch restarts: {len(watched)} Restart= daemons, "
          f"{bad} finding(s)")
    return 0


def cmd_timers(_a):
    """Ran-recently (timer) and succeeded (service) are different assertions
    — ISSUE-LOG #44 burned wq#1252 on conflating them with a file mtime."""
    tprops = unit_props([t["timer"] for t in TIMERS], ["LastTriggerUSec"])
    services = [t["timer"][:-len(".timer")] + ".service" for t in TIMERS]
    sprops = unit_props(services, ["Result", "ExecMainExitTimestamp"])
    now = int(time.time())
    bad = 0
    for t in TIMERS:
        timer = t["timer"]
        svc = timer[:-len(".timer")] + ".service"
        key = f"timer:{timer}"
        rec = tprops.get(timer)
        if rec is None:
            bad += 1
            raise_finding(key, f"Dead-man: {timer} not loaded",
                          f"systemctl show returned no record — the timer is "
                          f"gone, masked, or lost to a unit-drift apply. "
                          f"{t['why']}. (wq#1704, atlas §5 #4)", prio="urgent")
            continue
        last = epoch(rec.get("LastTriggerUSec"))
        age = now - last
        if last == 0 or age > t["max_age_s"]:
            bad += 1
            age_txt = "never" if last == 0 else f"{age // 60}min ago"
            raise_finding(
                key, f"Dead-man: {timer} has not fired",
                f"LastTriggerUSec={age_txt}, budget {t['max_age_s'] // 60}min. "
                f"A stopped timer produces silence, not a failure. {t['why']}. "
                f"(wq#1704, atlas §5 #4)", prio="urgent")
            continue
        result = (sprops.get(svc) or {}).get("Result", "unknown")
        if result not in ("success", "unknown"):
            bad += 1
            raise_finding(
                key, f"Dead-man: {timer} fires but {svc} fails",
                f"last run Result={result}. The timer is alive, so a "
                f"trigger-recency check alone would call this healthy. "
                f"{t['why']}. (wq#1704, atlas §5 #4)", prio="urgent")
            continue
        clear_finding(key, "spof-watch: timer firing and succeeding again")
    print(f"spof-watch timers: {len(TIMERS)} checked, {bad} finding(s)")
    return 0


COMMANDS = {"daemons": cmd_daemons, "roundtrip": cmd_roundtrip,
            "restarts": cmd_restarts, "timers": cmd_timers}


def main(argv=None):
    p = argparse.ArgumentParser(
        prog="fleet-spof-watch", description=__doc__.splitlines()[0])
    sub = p.add_subparsers(dest="cmd", required=True)
    for name in COMMANDS:
        sub.add_parser(name)
    a = p.parse_args(argv)
    return COMMANDS[a.cmd](a)


if __name__ == "__main__":
    sys.exit(main())
