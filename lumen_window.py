#!/usr/bin/env python3
"""lumen_window: maintenance-window scheduler and gate (wq#1401).

Windows are weekly, anchored to a tz name (zoneinfo), so they follow DST.
Anything disruptive checks the gate first. The drain runs a queued batch
inside the window, stops at the first failure, and never starts an op that
cannot finish before the hard stop.

Control-plane rule: ops tagged control-plane are never run or authorised
here. The drain lists them and pings Frank; he drives them by hand.

Exit codes for `check`/`enter`:
  0 ok  10 closed  11 too late for est  12 exclusive conflict  13 disabled
  14 release-lag rule  15 control-plane (needs Frank)  16 window halted
  17 node not allowed  18 reboot gate said no  2 usage
"""
from __future__ import annotations

import argparse
import datetime as dt
import fcntl
import json
import os
import re
import signal
import subprocess
import sys
import time
import tomllib
import urllib.request
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from zoneinfo import ZoneInfo

DAYS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
OK, USAGE, CLOSED, TOO_LATE, EXCLUSIVE, DISABLED, LAG, CONTROL_PLANE, HALTED, NODE, REBOOT_GATE = (
    0, 2, 10, 11, 12, 13, 14, 15, 16, 17, 18)
UTC = dt.timezone.utc


def config_path() -> Path:
    return Path(os.environ.get("LUMEN_WINDOW_CONFIG", "/home/lumen/instructions/playbooks/maintenance-window.toml"))


def state_dir() -> Path:
    return Path(os.environ.get("LUMEN_WINDOW_STATE", "/home/lumen/.local/state/lumen-window"))


def reboot_gate_bin() -> str:
    return os.environ.get("LUMEN_REBOOT_GATE", "/home/lumen/bin/lumen-reboot-gate")


def now_utc() -> dt.datetime:
    v = os.environ.get("LUMEN_WINDOW_NOW")
    return dt.datetime.fromisoformat(v).astimezone(UTC) if v else dt.datetime.now(UTC)


@dataclass
class Window:
    name: str
    enabled: bool
    day: int
    start: dt.time
    end: dt.time
    tz: ZoneInfo
    exclusive: bool = False
    nodes: list = field(default_factory=list)
    lag_days: int = 0
    primary_key: str = ""
    description: str = ""

    @property
    def length_sec(self) -> int:
        d = dt.date(2001, 1, 1)
        return int((dt.datetime.combine(d, self.end) - dt.datetime.combine(d, self.start)).total_seconds())


@dataclass
class Config:
    windows: dict
    margin_sec: int = 600
    verify_sec: int = 300
    ntfy_url: str = "http://localhost:8080/lumen-alerts"


def _t(s: str) -> dt.time:
    h, m = s.split(":")
    return dt.time(int(h), int(m))


def load_config(path=None) -> Config:
    with open(path or config_path(), "rb") as fh:
        raw = tomllib.load(fh)
    d = raw.get("defaults", {})
    cfg = Config(windows={}, margin_sec=int(d.get("margin_sec", 600)), verify_sec=int(d.get("verify_sec", 300)),
                 ntfy_url=d.get("ntfy_url", "http://localhost:8080/lumen-alerts"))
    tz_default = d.get("tz", "America/Los_Angeles")
    for name, w in raw.get("windows", {}).items():
        cfg.windows[name] = Window(
            name=name, enabled=bool(w.get("enabled", True)), day=DAYS.index(w["day"]),
            start=_t(w["start"]), end=_t(w["end"]), tz=ZoneInfo(w.get("tz", tz_default)),
            exclusive=bool(w.get("exclusive", False)), nodes=list(w.get("nodes", [])),
            lag_days=int(w.get("lag_days", 0)), primary_key=w.get("primary_key", ""),
            description=w.get("description", ""))
    return cfg


def validate(cfg: Config) -> list:
    problems = []
    for w in cfg.windows.values():
        if not re.fullmatch(r"[a-z0-9][a-z0-9-]*", w.name):
            problems.append(f"{w.name}: name must be a lowercase slug")
        if w.end <= w.start:
            problems.append(f"{w.name}: end must be after start on the same day")
        if w.lag_days and not w.primary_key:
            problems.append(f"{w.name}: lag_days needs primary_key")
    now = now_utc()
    spans = {n: _weeks(w, now, 10) for n, w in cfg.windows.items() if w.enabled}
    for a in spans:
        for b in spans:
            if a >= b or not (cfg.windows[a].exclusive or cfg.windows[b].exclusive):
                continue
            if any(s1 < e2 and s2 < e1 for s1, e1 in spans[a] for s2, e2 in spans[b]):
                problems.append(f"exclusive window overlap: {a} and {b}")
    return problems


def _occurrence(w: Window, d: dt.date):
    s = dt.datetime.combine(d, w.start, tzinfo=w.tz).astimezone(UTC)
    e = dt.datetime.combine(d, w.end, tzinfo=w.tz).astimezone(UTC)
    return s, e


def _weeks(w: Window, now: dt.datetime, n: int) -> list:
    out, base = [], now.astimezone(w.tz).date()
    for off in range(-1, 7 * n):
        d = base + dt.timedelta(days=off)
        if d.weekday() == w.day:
            out.append(_occurrence(w, d))
    return out


def span(w: Window, now: dt.datetime):
    """(state, start, end): the occurrence containing now, else the next one."""
    for s, e in _weeks(w, now, 2):
        if e > now:
            return ("open" if s <= now else "closed"), s, e
    raise RuntimeError("no occurrence found")


def just_ended(w: Window, now: dt.datetime, tol_sec: int = 900):
    for s, e in _weeks(w, now, 2):
        if e <= now and (now - e).total_seconds() <= tol_sec:
            return s, e
    return None


def fmt(t: dt.datetime, tz) -> str:
    return t.astimezone(tz).strftime("%a %Y-%m-%d %H:%M %Z")


# ---------------------------------------------------------------- state
@contextmanager
def locked():
    d = state_dir()
    d.mkdir(parents=True, exist_ok=True)
    with open(d / "lock", "w") as lk:
        fcntl.flock(lk, fcntl.LOCK_EX)
        yield


def _alive(pid) -> bool:
    try:
        os.kill(int(pid), 0)
        return True
    except (OSError, ValueError, TypeError):
        return False


def load_state() -> dict:
    try:
        s = json.load(open(state_dir() / "state.json"))
    except FileNotFoundError:
        s = {}
    for k, v in (("running", {}), ("queue", []), ("halted", {}), ("releases", {}), ("drained", {})):
        s.setdefault(k, v)
    return s


def save_state(s: dict) -> None:
    p = state_dir() / "state.json"
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(s, indent=1, sort_keys=True))
    os.replace(tmp, p)


def log_event(kind: str, **kw) -> None:
    state_dir().mkdir(parents=True, exist_ok=True)
    rec = {"ts": now_utc().isoformat(), "kind": kind, **kw}
    with open(state_dir() / "history.jsonl", "a") as fh:
        fh.write(json.dumps(rec) + "\n")


def notify(cfg: Config, title: str, body: str, priority: str = "default") -> None:
    state_dir().mkdir(parents=True, exist_ok=True)
    with open(state_dir() / "notifications.jsonl", "a") as fh:
        fh.write(json.dumps({"ts": now_utc().isoformat(), "title": title, "body": body, "priority": priority}) + "\n")
    if os.environ.get("LUMEN_WINDOW_NTFY", "on") == "off":
        return
    try:
        req = urllib.request.Request(cfg.ntfy_url, data=body.encode()[:3500], method="POST",
                                     headers={"Title": title, "Priority": priority, "Tags": "wrench"})
        urllib.request.urlopen(req, timeout=8).read()
    except Exception as exc:  # alerting must never break the run
        log_event("notify_failed", err=str(exc))


# ----------------------------------------------------------------- gate
def lag_check(w: Window, state: dict, now: dt.datetime, release):
    if not w.lag_days:
        return None
    if not release:
        return LAG, f"{w.name}: pass --release <version>; primary must have run it {w.lag_days}d first"
    recs = state["releases"].get(w.primary_key, [])
    today = now.astimezone(w.tz).date()
    if any(dt.datetime.fromisoformat(r["applied"]).astimezone(w.tz).date() == today for r in recs):
        return LAG, "primary was updated today; the backup never updates the same day"
    hit = [dt.datetime.fromisoformat(r["applied"]) for r in recs if r["version"] == release]
    if not hit:
        return LAG, f"release {release} not recorded on {w.primary_key}; primary must run it first"
    applied = min(hit)
    age = (now - applied).total_seconds() / 86400
    if age < w.lag_days:
        return LAG, f"release {release} ran on primary {age:.1f}d; need {w.lag_days}d (eligible {fmt(applied + dt.timedelta(days=w.lag_days), w.tz)})"
    return None


def gate(cfg: Config, state: dict, name: str, now: dt.datetime, est_sec: int = 0, node=None, tags=(), release=None):
    w = cfg.windows.get(name)
    if w is None:
        return USAGE, f"unknown window {name}"
    if not w.enabled:
        return DISABLED, f"{name} is disabled"
    st, s, e = span(w, now)
    if st != "open":
        return CLOSED, f"{name} closed; next opens {fmt(s, w.tz)}"
    if "control-plane" in tags:
        return CONTROL_PLANE, "control-plane op: not run unattended, Frank drives it"
    h = state["halted"].get(name)
    if h and h.get("start") == s.isoformat():
        return HALTED, f"{name} halted this window by {h.get('op')}: {h.get('note', '')}"
    for o in cfg.windows.values():
        if o.name != name and o.enabled and o.exclusive and span(o, now)[0] == "open":
            return EXCLUSIVE, f"exclusive window {o.name} is open"
    if w.exclusive:
        others = [i for i, r in state["running"].items() if r["window"] != name and _alive(r.get("pid"))]
        if others:
            return EXCLUSIVE, f"other ops running: {', '.join(others)}"
        if w.nodes and node not in w.nodes:
            return NODE, f"{name} only allows nodes {w.nodes}"
    lag = lag_check(w, state, now, release)
    if lag:
        return lag
    left = (e - now).total_seconds()
    if left < est_sec + cfg.margin_sec:
        return TOO_LATE, f"{int(left)}s left, need est {est_sec}s + margin {cfg.margin_sec}s"
    return OK, f"open, {int(left)}s left"


def is_open(name: str) -> bool:
    """Library helper for other tools (dispatcher, promoter)."""
    cfg = load_config()
    return gate(cfg, load_state(), name, now_utc())[0] == OK


# ------------------------------------------------------------- commands
def _split(s):
    return [x for x in (s or "").split(",") if x]


def cmd_status(cfg, a):
    now, state, rows = now_utc(), load_state(), []
    for w in cfg.windows.values():
        st, s, e = span(w, now)
        rows.append({"window": w.name, "enabled": w.enabled, "exclusive": w.exclusive, "state": st,
                     "opens": fmt(s, w.tz), "closes": fmt(e, w.tz),
                     "seconds_left": int((e - now).total_seconds()) if st == "open" else 0,
                     "queued": sum(1 for q in state["queue"] if q["window"] == w.name)})
    if a.json:
        print(json.dumps({"windows": rows, "running": state["running"], "halted": state["halted"]}, indent=1))
        return OK
    for r in rows:
        flag = "OPEN" if r["state"] == "open" else "closed"
        print(f"{r['window']:<18} {flag:<6} {'on ' if r['enabled'] else 'OFF'} opens {r['opens']}  closes {r['closes']}  queued={r['queued']}"
              + ("  exclusive" if r["exclusive"] else ""))
    for i, r in state["running"].items():
        print(f"running: {i} window={r['window']} node={r.get('node')}")
    return OK


def cmd_check(cfg, a):
    code, msg = gate(cfg, load_state(), a.window, now_utc(), a.est_sec, a.node, _split(a.tags), a.release)
    if not a.quiet:
        print(("OK " if code == OK else f"NO({code}) ") + msg)
    return code


def _register(state, op_id, window, node, est, tags):
    state["running"][op_id] = {"window": window, "node": node, "est_sec": est, "tags": tags,
                               "started": now_utc().isoformat(), "pid": os.getpid()}


def cmd_enter(cfg, a):
    with locked():
        state = load_state()
        code, msg = gate(cfg, state, a.window, now_utc(), a.est_sec, a.node, _split(a.tags), a.release)
        if code == OK:
            _register(state, a.op, a.window, a.node, a.est_sec, _split(a.tags))
            state["running"][a.op]["pid"] = a.pid or os.getppid()
            save_state(state)
            log_event("enter", op=a.op, window=a.window, node=a.node)
    print(("OK " if code == OK else f"NO({code}) ") + msg)
    return code


def _halt(cfg, state, window, op_id, note):
    _, s, _ = span(cfg.windows[window], now_utc())
    state["halted"][window] = {"start": s.isoformat(), "op": op_id, "note": note}


def cmd_leave(cfg, a):
    with locked():
        state = load_state()
        r = state["running"].pop(a.op, None)
        if r and a.result == "fail":
            _halt(cfg, state, r["window"], a.op, a.note or "")
        save_state(state)
    log_event("leave", op=a.op, result=a.result, note=a.note)
    if a.result == "fail":
        notify(cfg, f"MAINT WINDOW FAILURE: {a.op}", a.note or "op reported failure; window halted", "urgent")
    print("left", a.op)
    return OK


def cmd_queue(cfg, a):
    with locked():
        state = load_state()
        if a.qcmd == "add":
            if a.window not in cfg.windows:
                print(f"unknown window {a.window}")
                return USAGE
            if any(q["id"] == a.id for q in state["queue"]):
                print(f"duplicate id {a.id}")
                return USAGE
            if a.reboot and not a.node:
                print("--reboot needs --node")
                return USAGE
            state["queue"].append({"id": a.id, "window": a.window, "cmd": a.cmd, "verify": a.verify, "precheck": a.precheck,
                                   "est_sec": a.est_sec, "node": a.node, "tags": _split(a.tags), "reboot": a.reboot,
                                   "ticket": a.ticket, "pri": a.pri, "added": now_utc().isoformat(), "attempts": 0})
            save_state(state)
            print("queued", a.id)
        elif a.qcmd == "rm":
            state["queue"] = [q for q in state["queue"] if q["id"] != a.id]
            save_state(state)
            print("removed", a.id)
        else:
            if a.json:
                print(json.dumps(state["queue"], indent=1))
            for q in state["queue"]:
                print(f"{q['id']:<24} {q['window']:<16} est={q['est_sec']}s node={q.get('node')} tags={','.join(q['tags'])}"
                      f"{' reboot' if q['reboot'] else ''} attempts={q['attempts']}")
    return OK


def _tail(out, n=1500):
    return (out or "")[-n:]


def run_cmd(cmd: str, timeout: float):
    p = subprocess.Popen(["bash", "-c", cmd], stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                         start_new_session=True, text=True)
    try:
        out, _ = p.communicate(timeout=max(1, timeout))
        return p.returncode, _tail(out)
    except subprocess.TimeoutExpired:
        os.killpg(p.pid, signal.SIGTERM)
        try:
            out, _ = p.communicate(timeout=10)
        except subprocess.TimeoutExpired:
            os.killpg(p.pid, signal.SIGKILL)
            out, _ = p.communicate()
        return 124, _tail(out) + "\n[timeout]"


def reboot_ok(node: str) -> bool:
    try:
        r = subprocess.run([reboot_gate_bin(), node, "--json"], capture_output=True, text=True, timeout=180)
        return r.returncode == 0 and json.loads(r.stdout).get("reboot_safe") is True
    except Exception:
        return False


def cmd_drain(cfg, a):
    w = cfg.windows.get(a.window)
    if w is None or not w.enabled:
        print("unknown or disabled window")
        return USAGE
    now = now_utc()
    st, s, e = span(w, now)
    if st != "open":
        if 0 < (s - now).total_seconds() <= 30 and not os.environ.get("LUMEN_WINDOW_NOW"):
            time.sleep((s - now).total_seconds() + 1)
        else:
            print(f"{a.window} not open; next {fmt(s, w.tz)}")
            return OK
    os.makedirs(state_dir(), exist_ok=True)
    fl = open(state_dir() / f"drain-{a.window}.lock", "w")
    try:
        fcntl.flock(fl, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        print("another drain is running")
        return OK
    with locked():
        state = load_state()
        state["drained"][a.window] = s.isoformat()
        save_state(state)
    queue = sorted([q for q in load_state()["queue"] if q["window"] == a.window], key=lambda q: (q.get("pri", 5), q["added"]))
    ran, needs_frank, deferred, skipped, failed = [], [], [], [], None
    for op in queue:
        tags = op.get("tags", [])
        if "control-plane" in tags:
            needs_frank.append(op["id"])
            continue
        est = int(op["est_sec"]) + cfg.verify_sec
        code, msg = gate(cfg, load_state(), a.window, now_utc(), est, op.get("node"), tags)
        if code == TOO_LATE:
            deferred.append(op["id"])
            continue
        if code == NODE:
            skipped.append(f"{op['id']} ({msg})")
            continue
        if code != OK:
            skipped.append(f"{op['id']} ({msg})")
            break
        if a.dry_run:
            ran.append(op["id"] + " (dry-run)")
            continue
        if op.get("reboot") and not reboot_ok(op["node"]):
            skipped.append(f"{op['id']} (reboot gate refused {op['node']})")
            continue
        if op.get("precheck"):
            rc, out = run_cmd(op["precheck"], 120)
            if rc != 0:
                skipped.append(f"{op['id']} (precheck failed: {_tail(out, 200)})")
                continue
        with locked():
            state = load_state()
            _register(state, op["id"], a.window, op.get("node"), est, tags)
            save_state(state)
        left = (e - now_utc()).total_seconds()
        rc, out = run_cmd(op["cmd"], min(int(op["est_sec"]) * 2, left - 60))
        if rc == 0 and op.get("verify"):
            left = (e - now_utc()).total_seconds()
            rc, out2 = run_cmd(op["verify"], min(cfg.verify_sec, max(left - 30, 1)))
            out += "\n[verify] " + out2
        with locked():
            state = load_state()
            state["running"].pop(op["id"], None)
            if rc == 0:
                state["queue"] = [q for q in state["queue"] if q["id"] != op["id"]]
            else:
                for q in state["queue"]:
                    if q["id"] == op["id"]:
                        q["attempts"] += 1
                        q["last_error"] = _tail(out, 400)
                _halt(cfg, state, a.window, op["id"], f"rc={rc}")
            save_state(state)
        log_event("op", op=op["id"], rc=rc, window=a.window)
        if rc == 0:
            ran.append(op["id"])
        else:
            failed = (op["id"], rc, out)
            break
    lines = []
    if ran:
        lines.append("ran: " + ", ".join(ran))
    if failed:
        lines.append(f"FAILED: {failed[0]} rc={failed[1]}; batch halted, nothing else started\n{_tail(failed[2], 600)}")
    if needs_frank:
        lines.append("needs Frank (control-plane, not run): " + ", ".join(needs_frank))
    if deferred:
        lines.append("deferred (would not finish before hard stop): " + ", ".join(deferred))
    if skipped:
        lines.append("skipped: " + "; ".join(skipped))
    if lines:
        notify(cfg, f"maint window {a.window}: {'FAILED' if failed else 'done'}", "\n".join(lines), "urgent" if failed else "default")
        print("\n".join(lines))
    else:
        print("queue empty, nothing to do")
    return 1 if failed else OK


def cmd_close(cfg, a):
    w = cfg.windows.get(a.window)
    if w is None:
        return USAGE
    now = now_utc()
    ended = just_ended(w, now)
    problems = []
    with locked():
        state = load_state()
        for i, r in list(state["running"].items()):
            if r["window"] != a.window:
                continue
            if _alive(r.get("pid")):
                problems.append(f"{i} still running at hard stop")
            else:
                state["running"].pop(i)
        left = [q["id"] for q in state["queue"] if q["window"] == a.window and "control-plane" not in q.get("tags", [])]
        if ended and left and state["drained"].get(a.window) != ended[0].isoformat():
            problems.append(f"window passed without a drain; queued: {', '.join(left)}")
        save_state(state)
    if problems:
        notify(cfg, f"MAINT WINDOW {a.window} PROBLEM at hard stop", "\n".join(problems), "urgent")
        print("\n".join(problems))
        return 1
    print("closed clean")
    return OK


def cmd_release(cfg, a):
    with locked():
        state = load_state()
        if a.rcmd == "record":
            today = now_utc().astimezone(UTC).date()
            for w in cfg.windows.values():
                pair = {w.primary_key: w.name, w.name: w.primary_key} if w.lag_days else {}
                other = pair.get(a.key)
                if other and any(dt.datetime.fromisoformat(r["applied"]).date() == today
                                 for r in state["releases"].get(other, [])):
                    print(f"refused: {other} was updated today; primary and backup never update the same day")
                    return LAG
            when = dt.datetime.fromisoformat(a.at).astimezone(UTC) if a.at else now_utc()
            state["releases"].setdefault(a.key, []).append({"version": a.version, "applied": when.isoformat()})
            save_state(state)
            print("recorded", a.key, a.version)
        else:
            w = cfg.windows.get(a.key)
            r = lag_check(w, state, now_utc(), a.version) if w else (USAGE, "unknown window")
            print("eligible" if r is None else r[1])
            return OK if r is None else r[0]
    return OK


def unit_files(cfg: Config) -> dict:
    out = {}
    for w in cfg.windows.values():
        if not w.enabled:
            continue
        cal = lambda t: f"{DAYS[w.day]} *-*-* {t.strftime('%H:%M')}:00 {w.tz.key}"
        L = w.length_sec
        for kind, t, verb in (("open", w.start, "drain"), ("close", w.end, "close")):
            base = f"lumen-window-{kind}-{w.name}"
            out[f"{base}.service"] = (
                f"# generated by lumen-window install-units, edit the toml instead\n[Unit]\n"
                f"Description=Maintenance window {w.name}: {verb} (wq#1401)\nOnFailure=ntfy-onfailure@%n.service\n\n"
                f"[Service]\nType=oneshot\nUser=lumen\nExecStart=/home/lumen/bin/lumen-window {verb} {w.name}\n"
                f"ExecStartPost=-/home/lumen/bin/lumen-cron-stamp %n\n"
                + (f"TimeoutStartSec={L}\nRuntimeMaxSec={L}\n" if kind == "open" else "TimeoutStartSec=120\n"))
            out[f"{base}.timer"] = (
                f"# generated by lumen-window install-units, edit the toml instead\n[Unit]\n"
                f"Description=Maintenance window {w.name} {kind}s {DAYS[w.day]} {t.strftime('%H:%M')} {w.tz.key}\n\n"
                f"[Timer]\nOnCalendar={cal(t)}\nPersistent=false\nAccuracySec=1s\n\n[Install]\nWantedBy=timers.target\n")
    return out


def cmd_install_units(cfg, a):
    units = unit_files(cfg)
    stage = Path(a.stage)
    stage.mkdir(parents=True, exist_ok=True)
    for n, body in units.items():
        (stage / n).write_text(body)
    print(f"staged {len(units)} units in {stage}")
    if a.apply:
        for n in units:
            subprocess.run(["sudo", "-n", "install", "-m", "644", str(stage / n), f"/etc/systemd/system/{n}"], check=True)
        subprocess.run(["sudo", "-n", "systemctl", "daemon-reload"], check=True)
        subprocess.run(["sudo", "-n", "systemctl", "enable", "--now"] + [n for n in units if n.endswith(".timer")], check=True)
        print("installed and enabled timers")
    return OK


def cmd_validate(cfg, a):
    p = validate(cfg)
    print("config OK" if not p else "\n".join(p))
    return 1 if p else OK


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="lumen-window", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="action", required=True)
    p = sub.add_parser("status"); p.add_argument("--json", action="store_true")
    for n in ("check", "enter"):
        p = sub.add_parser(n); p.add_argument("window"); p.add_argument("--est-sec", type=int, default=0)
        p.add_argument("--node"); p.add_argument("--tags", default=""); p.add_argument("--release")
        if n == "check":
            p.add_argument("--quiet", action="store_true")
        else:
            p.add_argument("--op", required=True); p.add_argument("--pid", type=int)
    p = sub.add_parser("leave"); p.add_argument("--op", required=True); p.add_argument("--result", choices=["ok", "fail"], required=True); p.add_argument("--note")
    q = sub.add_parser("queue"); qs = q.add_subparsers(dest="qcmd", required=True)
    p = qs.add_parser("add"); p.add_argument("--id", required=True); p.add_argument("--window", required=True); p.add_argument("--cmd", required=True)
    p.add_argument("--verify"); p.add_argument("--precheck"); p.add_argument("--est-sec", type=int, default=300); p.add_argument("--node")
    p.add_argument("--tags", default=""); p.add_argument("--reboot", action="store_true"); p.add_argument("--ticket"); p.add_argument("--pri", type=int, default=5)
    p = qs.add_parser("rm"); p.add_argument("id")
    p = qs.add_parser("list"); p.add_argument("--json", action="store_true")
    p = sub.add_parser("drain"); p.add_argument("window"); p.add_argument("--dry-run", action="store_true")
    p = sub.add_parser("close"); p.add_argument("window")
    r = sub.add_parser("release"); rs = r.add_subparsers(dest="rcmd", required=True)
    p = rs.add_parser("record"); p.add_argument("key"); p.add_argument("version"); p.add_argument("--at")
    p = rs.add_parser("eligible"); p.add_argument("key"); p.add_argument("version")
    sub.add_parser("validate")
    p = sub.add_parser("install-units"); p.add_argument("--stage", default="/tmp/lumen-window-units"); p.add_argument("--apply", action="store_true")
    a = ap.parse_args(argv)
    cfg = load_config()
    return {"status": cmd_status, "check": cmd_check, "enter": cmd_enter, "leave": cmd_leave, "queue": cmd_queue,
            "drain": cmd_drain, "close": cmd_close, "release": cmd_release, "validate": cmd_validate,
            "install-units": cmd_install_units}[a.action](cfg, a)


if __name__ == "__main__":
    sys.exit(main())
