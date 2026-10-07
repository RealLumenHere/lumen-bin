#!/usr/bin/env python3
"""guard-meta-watchdog (wq#1705) — remediation for guards that stay dead.

WHY THIS EXISTS (2026-09-10/11): seven guard/watchdog units were sitting in
`failed` — pat-scan, the drift guards, budget-alert and friends. Nothing was
broken about DETECTION: `lumen-failed-units` saw every one of them. It is
edge-triggered and reporting-only by design ("a unit that stays failed does NOT
re-alert every cycle ... It NEVER restarts, resets, or mutates any unit"), so a
guard that died once got exactly one notification and then went quiet forever.
The firefighter was dead and the smoke alarm had already stopped ringing.

This closes the remediation half, and only that half:

    crashed guard  ->  reset-failed + restart, with exponential backoff
    still crashed after MAX_ATTEMPTS  ->  ONE aq row (source='guard-selfheal',
                                          key=unit) + one urgent page
    recovered      ->  clear backoff state, post a low-priority CLEARED notice

Three things it deliberately does NOT do:

  * It does not touch non-guard units. `lumen-failed-units` owns visibility for
    everything; this owns remediation for the units whose whole job is to watch
    other things. Restarting arbitrary system services from a cron-ish timer is
    how you get the two-agents-restarting-lumen-agent race in REGISTRY.yaml.
  * It does not restart a guard that "failed" because it FOUND something.
    wq#1657 established the exit-code contract: a unit tagged
    `X-Lumen-Guard=finding-exit` (or named in guard-finding-exit.list) that
    exits 1 is reporting a finding, not crashing. Restarting it would just
    re-run the scan, re-find the same thing, and fail again forever. Those are
    classified `finding` here and left strictly alone.
  * It does not restart forever. A guard that keeps dying after repeated heals
    (flapping) escalates instead of churning — see FLAP_LIMIT.

Fail-open: any internal error exits 0 with state untouched. A broken
meta-watchdog must not itself become an eighth dead guard.

State: /home/lumen/agents/guard-meta-watchdog/state.json
"""

import json
import os
import subprocess
import sys
import time

STATE_PATH = os.environ.get(
    "GUARD_META_STATE", "/home/lumen/agents/guard-meta-watchdog/state.json")

# wq#1657's bridge list. Its target path, so that when wq#1657 deploys there is
# exactly one list; the copy shipped next to this script is the fallback until
# then (wq#1657 is staged-not-deployed as of 2026-09-11).
FINDING_LIST_PATHS = (
    os.environ.get("GUARD_FINDING_LIST", "/home/lumen/bin/guard-finding-exit.list"),
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "guard-finding-exit.list"),
)

AQ = os.environ.get("GUARD_META_AQ", "/home/lumen/bin/aq")
NTFY = os.environ.get("GUARD_META_NTFY", "/home/lumen/bin/ntfy-send")

MAX_ATTEMPTS = int(os.environ.get("GUARD_META_MAX_ATTEMPTS", "3") or 3)
BACKOFF_BASE_S = int(os.environ.get("GUARD_META_BACKOFF_BASE_S", "300") or 300)
BACKOFF_CAP_S = int(os.environ.get("GUARD_META_BACKOFF_CAP_S", "3600") or 3600)
# How many successful heals of the same unit within FLAP_WINDOW_S before we stop
# healing and escalate: a guard we have resurrected 3 times today is not healthy,
# it is flapping, and silently restarting it is how the outage stays invisible.
FLAP_LIMIT = int(os.environ.get("GUARD_META_FLAP_LIMIT", "3") or 3)
FLAP_WINDOW_S = int(os.environ.get("GUARD_META_FLAP_WINDOW_S", "86400") or 86400)

# A guard is identified by role, not by an enumerated allowlist — an allowlist is
# exactly what let logrotate.service rot for six days (see lumen-failed-units).
GUARD_NAME_TOKENS = ("guard", "watch", "sentinel", "canary", "drift", "scan",
                     "alert", "audit", "probe", "lint", "sweep", "monitor",
                     "healer", "reconcile", "deadman", "smoke", "check",
                     "verify")

# Units that must never be auto-restarted even though they match a guard token.
# Nothing on this host restarts Thor (instinct 2) and nothing here restarts the
# agent runtime that may be mid-turn.
PROTECTED = frozenset(x for x in (
    os.environ.get("GUARD_META_PROTECTED", "lumen-agent.service").split(","))
    if x.strip())


# ---------------------------------------------------------------------------
# systemd surface — one indirection so tests never touch the real system
# ---------------------------------------------------------------------------

def _run(argv, timeout=30):
    try:
        return subprocess.run(argv, capture_output=True, text=True, timeout=timeout)
    except (OSError, subprocess.SubprocessError):
        return None


class Systemd:
    """The only object that shells out to systemctl. Tests substitute a fake."""

    def failed_units(self):
        p = _run(["systemctl", "list-units", "--type=service", "--type=timer",
                  "--type=socket", "--type=mount", "--state=failed",
                  "--no-legend", "--plain"])
        if p is None or p.returncode != 0:
            return []
        out = []
        for line in (p.stdout or "").splitlines():
            parts = line.split()
            if parts and parts[0].endswith((".service", ".timer", ".socket", ".mount")):
                out.append(parts[0])
        return sorted(set(out))

    def show(self, unit, props):
        p = _run(["systemctl", "show", unit, "--no-pager"]
                 + [f"-p{k}" for k in props])
        vals = {}
        if p is None:
            return vals
        for line in (p.stdout or "").splitlines():
            if "=" in line:
                k, v = line.split("=", 1)
                vals[k] = v
        return vals

    def cat(self, unit):
        p = _run(["systemctl", "cat", unit])
        return (p.stdout or "") if p and p.returncode == 0 else ""

    def reset_failed(self, unit):
        p = _run(["systemctl", "reset-failed", unit])
        return bool(p and p.returncode == 0)

    def restart(self, unit):
        p = _run(["systemctl", "restart", unit], timeout=120)
        return bool(p and p.returncode == 0)

    def is_failed(self, unit):
        p = _run(["systemctl", "is-failed", unit])
        return bool(p and (p.stdout or "").strip() == "failed")


# ---------------------------------------------------------------------------
# classification
# ---------------------------------------------------------------------------

def _finding_list():
    for path in FINDING_LIST_PATHS:
        if not path:
            continue
        try:
            with open(path) as f:
                names = {ln.strip() for ln in f
                         if ln.strip() and not ln.lstrip().startswith("#")}
            if names:
                return names
        except OSError:
            continue
    return set()


def is_guard(unit, description=""):
    """Role test. Name tokens first, then the unit's own Description — a unit
    called `unit-drift.service` and one described as "... sentinel ..." are both
    guards, and neither is in any hand-maintained list."""
    hay = f"{unit} {description}".lower()
    return any(tok in hay for tok in GUARD_NAME_TOKENS)


def classify(unit, systemd, finding_names=None):
    """-> ('crash'|'finding'|'not-a-guard', detail dict)

    wq#1657's contract: under Mode B, exit code 1 from a declared finding-exit
    guard is a FINDING. Anything else nonzero — and any signal, timeout or
    start-limit result — is a crash, tag or no tag.
    """
    names = _finding_list() if finding_names is None else finding_names
    props = systemd.show(unit, ("Result", "ExecMainStatus", "Description",
                                "ActiveState"))
    detail = {
        "result": props.get("Result", ""),
        "exit": props.get("ExecMainStatus", ""),
        "description": props.get("Description", ""),
    }
    if not is_guard(unit, detail["description"]):
        return "not-a-guard", detail

    tagged = unit in names or "X-Lumen-Guard=finding-exit" in systemd.cat(unit)
    detail["finding_exit_declared"] = tagged
    if tagged and detail["result"] == "exit-code" and detail["exit"] == "1":
        return "finding", detail
    return "crash", detail


# ---------------------------------------------------------------------------
# state
# ---------------------------------------------------------------------------

def load_state(path=None):
    try:
        with open(path or STATE_PATH) as f:
            doc = json.load(f)
        if isinstance(doc, dict) and isinstance(doc.get("units"), dict):
            return doc
    except (OSError, ValueError):
        pass
    return {"units": {}}


def save_state(state, path=None):
    path = path or STATE_PATH
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        tmp = path + ".tmp"
        with open(tmp, "w") as f:
            json.dump(state, f, indent=1, sort_keys=True)
        os.replace(tmp, path)
        return True
    except OSError:
        return False


def backoff_for(attempts):
    """Delay before attempt number `attempts` (0-based): 300s, 600s, 1200s..."""
    return min(BACKOFF_BASE_S * (2 ** max(0, attempts)), BACKOFF_CAP_S)


# ---------------------------------------------------------------------------
# notification
# ---------------------------------------------------------------------------

class Notifier:
    """aq row + ntfy page. `aq add <source> <key> <title> <msg>` dedups on
    (source, key), so re-running this timer cannot spam duplicate rows for the
    same stuck unit."""

    SOURCE = "guard-selfheal"

    def __init__(self, aq=AQ, ntfy=NTFY):
        self.aq, self.ntfy = aq, ntfy
        self.sent = []

    def __call__(self, priority, key, title, message):
        self.sent.append({"priority": priority, "key": key, "title": title,
                          "message": message})
        if self.ntfy and os.path.exists(self.ntfy):
            _run([self.ntfy, "-t", "lumen-alerts", "-p", priority, "-T", title,
                  "--quiet", message])
        if self.aq and os.path.exists(self.aq):
            _run([self.aq, "add", self.SOURCE, key, title, message])
        return True


# ---------------------------------------------------------------------------
# the sweep
# ---------------------------------------------------------------------------

def sweep(systemd=None, notify=None, state=None, now=None, state_path=None,
          persist=True):
    """One pass. Returns a report dict; never raises for an operational fault."""
    systemd = systemd or Systemd()
    notify = notify or Notifier()
    now = int(time.time()) if now is None else int(now)
    owned_state = state is None
    state = load_state(state_path) if owned_state else state
    units_state = state.setdefault("units", {})

    report = {"attempted": [], "healed": [], "backing_off": [], "escalated": [],
              "findings": [], "skipped": [], "recovered": []}

    failed = systemd.failed_units()
    finding_names = _finding_list()

    for unit in failed:
        kind, detail = classify(unit, systemd, finding_names)
        if kind == "not-a-guard":
            report["skipped"].append({"unit": unit, "why": "not-a-guard"})
            continue
        if kind == "finding":
            # Visibility is lumen-failed-units' job and OnFailure= keeps it loud
            # (wq#1657). Restarting would re-find the same thing forever.
            report["findings"].append({"unit": unit, **detail})
            continue
        if unit in PROTECTED:
            report["skipped"].append({"unit": unit, "why": "protected"})
            continue

        st = units_state.setdefault(unit, {"attempts": 0, "heals": [], "next_at": 0})
        st.setdefault("heals", [])
        st["heals"] = [t for t in st["heals"] if now - t < FLAP_WINDOW_S]

        if len(st["heals"]) >= FLAP_LIMIT:
            if not st.get("escalated_at"):
                st["escalated_at"] = now
                notify("high", unit, f"Guard flapping: {unit}",
                       f"healed {len(st['heals'])}x in the last "
                       f"{FLAP_WINDOW_S // 3600}h and is failed again "
                       f"(result={detail['result']} exit={detail['exit']}). "
                       f"Restarting it again would only hide the cause.")
            report["escalated"].append({"unit": unit, "why": "flapping",
                                        "heals": len(st["heals"])})
            continue

        if st["attempts"] >= MAX_ATTEMPTS:
            if not st.get("escalated_at"):
                st["escalated_at"] = now
                notify("high", unit, f"Guard will not restart: {unit}",
                       f"{st['attempts']} restart attempts failed "
                       f"(result={detail['result']} exit={detail['exit']}). "
                       f"{detail['description']} — this guard is down and is NOT "
                       f"watching whatever it was meant to watch.")
            report["escalated"].append({"unit": unit, "why": "max-attempts",
                                        "attempts": st["attempts"]})
            continue

        if now < int(st.get("next_at") or 0):
            report["backing_off"].append({"unit": unit,
                                          "retry_in_s": int(st["next_at"]) - now,
                                          "attempts": st["attempts"]})
            continue

        # A unit parked by StartLimitBurst refuses `restart` until its failure
        # state is cleared, so reset-failed is not optional here.
        systemd.reset_failed(unit)
        ok = systemd.restart(unit) and not systemd.is_failed(unit)
        st["attempts"] = int(st["attempts"]) + 1
        st["last_attempt_at"] = now
        if ok:
            st["heals"].append(now)
            st["attempts"] = 0
            st["next_at"] = 0
            report["healed"].append({"unit": unit, "heals": len(st["heals"])})
            notify("low", unit, f"Guard self-healed: {unit}",
                   f"was failed (result={detail['result']} exit={detail['exit']}), "
                   f"restarted and is running again.")
        else:
            st["next_at"] = now + backoff_for(st["attempts"])
            report["attempted"].append({"unit": unit, "attempts": st["attempts"],
                                        "healed": False,
                                        "next_at": st["next_at"]})

    # Anything we were tracking that is no longer failed has recovered on its
    # own; drop its backoff so a future failure starts from a clean slate.
    for unit in [u for u in units_state if u not in failed]:
        st = units_state[unit]
        if st.get("attempts") or st.get("escalated_at"):
            notify("low", unit, f"Recovered: {unit}",
                   "no longer in failed state; self-heal state cleared.")
        report["recovered"].append(unit)
        units_state.pop(unit, None)

    state["checked_at"] = now
    if persist and owned_state:
        save_state(state, state_path)
    return report


class DryRun(Systemd):
    """Reads the live system, refuses every mutation. `--dry-run` answers
    "what would this do right now" without being a second thing that restarts
    units behind an operator's back."""

    def reset_failed(self, unit):
        return True

    def restart(self, unit):
        return False


def main(argv=None):
    argv = list(argv if argv is not None else [])
    dry = "--dry-run" in argv
    try:
        report = sweep(systemd=DryRun() if dry else None,
                       notify=(lambda *a: True) if dry else None,
                       persist=not dry)
    except Exception as e:  # fail-open: never become the eighth dead guard
        print(f"guard-meta-watchdog: internal error, exiting 0: {e}")
        return 0
    if dry or "--print" in argv or os.isatty(1):
        print(json.dumps(report, indent=1, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
