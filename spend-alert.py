#!/usr/bin/env python3
"""spend-alert.py — threshold alerts for Lumen spend pools.

wq#720 (SPEND-2): alert BEFORE spend hits the floor.

Reads the deepseek_spend.db snapshot history, detects the most recent top-up,
and fires ntfy alerts at configurable % of budget remaining. Also detects
runaway daily burn (2x the 7-day rolling average).

Runs as ExecStartPost on lumen-deepseek-spend.service (every 15 min).
Alert dedup: once a threshold fires, it won't fire again until a top-up resets it.

Config env vars (all optional):
  SPEND_ALERT_THRESHOLDS  comma-sep pct remaining, default "50,25,10"
  SPEND_ALERT_RUNAWAY_X   multiplier for daily burn, default "2.0"
  SPEND_ALERT_STATE       state file path
  SPEND_ALERT_DB          deepseek_spend.db path
"""
import json, os, sqlite3, subprocess, sys, time
from datetime import datetime, timezone
from pathlib import Path

DB_PATH = os.environ.get("SPEND_ALERT_DB",
                         "/home/lumen/messenger/data/deepseek_spend.db")
STATE_PATH = os.environ.get("SPEND_ALERT_STATE",
                            "/home/lumen/agent/spend_alert_state.json")
NTFY_BIN = "/home/lumen/bin/ntfy-send"
THRESHOLDS = sorted(
    [int(x) for x in os.environ.get("SPEND_ALERT_THRESHOLDS", "50,25,10").split(",")],
    reverse=True,
)  # descending: 50, 25, 10
RUNAWAY_X = float(os.environ.get("SPEND_ALERT_RUNAWAY_X", "2.0"))
DAY_SECS = 86400


def _db():
    if not os.path.exists(DB_PATH):
        print(f"spend-alert: db not found at {DB_PATH}", file=sys.stderr)
        sys.exit(0)  # not an error — spend history may not have started yet
    con = sqlite3.connect(DB_PATH, timeout=10)
    con.execute("PRAGMA journal_mode=WAL")
    return con


def _load_state():
    try:
        return json.loads(Path(STATE_PATH).read_text())
    except Exception:
        return {}


def _save_state(state):
    Path(STATE_PATH).parent.mkdir(parents=True, exist_ok=True)
    Path(STATE_PATH).write_text(json.dumps(state, indent=2) + "\n")


def _ntfy(msg, title, priority="high", tags="warning"):
    try:
        subprocess.run(
            [NTFY_BIN, "-t", "lumen-alerts", "-p", priority, "-T", title, msg],
            capture_output=True, timeout=15,
        )
    except Exception as e:
        print(f"spend-alert: ntfy failed: {e}", file=sys.stderr)


def find_last_topup(con):
    """Find the most recent top-up event. A top-up is a row where topup > 0.
    Returns (ts, balance_after_topup, topup_amount) or None."""
    row = con.execute(
        "SELECT ts, balance, topup FROM snapshots WHERE topup > 0.01 "
        "ORDER BY ts DESC LIMIT 1"
    ).fetchone()
    if row:
        return {"ts": row[0], "balance": row[1], "topup": row[2]}
    # No top-up ever recorded — use the earliest snapshot as baseline
    row = con.execute(
        "SELECT ts, balance FROM snapshots ORDER BY ts ASC LIMIT 1"
    ).fetchone()
    if row:
        return {"ts": row[0], "balance": row[1], "topup": 0.0}
    return None


def current_balance(con):
    row = con.execute(
        "SELECT ts, balance FROM snapshots ORDER BY ts DESC LIMIT 1"
    ).fetchone()
    if row:
        return {"ts": row[0], "balance": row[1]}
    return None


def daily_burn_rates(con, days=8):
    """Compute per-day spend for the last N days (Pacific time).
    Returns list of (date_str, spend_usd) newest first."""
    from zoneinfo import ZoneInfo
    pt = ZoneInfo("America/Los_Angeles")
    now_pt = datetime.now(pt)
    rates = []
    for d in range(days):
        from datetime import timedelta
        day = (now_pt - timedelta(days=d)).replace(
            hour=0, minute=0, second=0, microsecond=0
        )
        day_start = int(day.timestamp())
        day_end = day_start + DAY_SECS
        row = con.execute(
            "SELECT COALESCE(SUM(spend), 0) FROM snapshots WHERE ts >= ? AND ts < ?",
            (day_start, day_end),
        ).fetchone()
        rates.append((day.strftime("%Y-%m-%d"), round(row[0], 4)))
    return rates


def check_thresholds(con, state):
    """Check balance-remaining thresholds and runaway burn rate.
    Returns list of alerts to fire."""
    alerts = []
    topup_info = find_last_topup(con)
    if not topup_info:
        return alerts
    cur = current_balance(con)
    if not cur:
        return alerts

    # The "budget" is the balance right after the last top-up.
    # For a $49.99 top-up with $0 prior balance, budget = $49.99.
    # For a $49.99 top-up with $22.xx prior, budget = $72.xx (the post-topup balance).
    budget = topup_info["balance"]
    balance = cur["balance"]
    if budget <= 0:
        return alerts

    pct_remaining = round(100.0 * balance / budget, 1)
    topup_ts = topup_info["ts"]

    # Detect top-up reset: if topup ts changed since last state, clear fired thresholds
    last_topup_ts = state.get("last_topup_ts", 0)
    if topup_ts != last_topup_ts:
        state["fired_thresholds"] = []
        state["last_topup_ts"] = topup_ts
        state["budget"] = budget

    fired = set(state.get("fired_thresholds", []))

    for thr in THRESHOLDS:
        if thr in fired:
            continue
        if pct_remaining <= thr:
            severity = "info" if thr >= 50 else ("high" if thr >= 25 else "urgent")
            tags = "chart_with_downwards_trend" if thr >= 25 else "rotating_light"
            alerts.append({
                "threshold": thr,
                "pct_remaining": pct_remaining,
                "balance": balance,
                "budget": budget,
                "priority": severity,
                "tags": tags,
            })
            fired.add(thr)

    state["fired_thresholds"] = sorted(fired)
    return alerts


def check_runaway(con, state):
    """Fire if today's burn > RUNAWAY_X * 7d rolling average."""
    rates = daily_burn_rates(con, days=8)
    if len(rates) < 2:
        return None
    today_spend = rates[0][1]
    # 7d average excludes today
    past_7 = [r[1] for r in rates[1:8]]
    if not past_7:
        return None
    avg_7d = sum(past_7) / len(past_7)
    if avg_7d < 0.01:
        # Baseline too low to be meaningful — skip
        return None
    ratio = today_spend / avg_7d
    if ratio >= RUNAWAY_X:
        # Dedup: only fire once per day
        today_str = rates[0][0]
        if state.get("runaway_fired_date") == today_str:
            return None
        state["runaway_fired_date"] = today_str
        return {
            "today_spend": today_spend,
            "avg_7d": round(avg_7d, 4),
            "ratio": round(ratio, 1),
            "date": today_str,
        }
    return None


def main():
    con = _db()
    state = _load_state()

    # --- threshold alerts ---
    alerts = check_thresholds(con, state)
    for a in alerts:
        title = f"DeepSeek spend: {a['pct_remaining']}% remaining"
        msg = (
            f"Balance ${a['balance']:.2f} / ${a['budget']:.2f} budget "
            f"({a['pct_remaining']}% left).\n"
            f"Threshold crossed: {a['threshold']}% remaining."
        )
        _ntfy(msg, title, priority=a["priority"], tags=a["tags"])
        print(f"ALERT [{a['priority']}]: {title}")

    # --- runaway burn ---
    runaway = check_runaway(con, state)
    if runaway:
        title = "DeepSeek RUNAWAY spend detected"
        msg = (
            f"Today's burn ${runaway['today_spend']:.2f} is "
            f"{runaway['ratio']}x the 7d average ${runaway['avg_7d']:.2f}.\n"
            f"Date: {runaway['date']}"
        )
        _ntfy(msg, title, priority="urgent", tags="rotating_light")
        print(f"ALERT [urgent]: {title}")

    _save_state(state)
    con.close()

    cur = current_balance(_db())
    bal_str = f"${cur['balance']:.2f}" if cur else "n/a"
    n_fired = len(state.get("fired_thresholds", []))
    print(f"spend-alert: balance={bal_str}, "
          f"thresholds_fired={n_fired}/{len(THRESHOLDS)}, "
          f"alerts_this_run={len(alerts) + (1 if runaway else 0)}")


def test():
    """Test mode: inject mock balance to verify each threshold fires."""
    import tempfile, shutil
    print("=== spend-alert test mode ===")
    # Copy real DB to temp
    tmp = tempfile.mktemp(suffix=".db")
    shutil.copy2(DB_PATH, tmp)
    con = sqlite3.connect(tmp, timeout=10)
    con.execute("PRAGMA journal_mode=WAL")

    # Find current topup baseline
    topup_info = find_last_topup(con)
    if not topup_info:
        print("FAIL: no topup/baseline found in DB")
        os.unlink(tmp)
        return False
    budget = topup_info["balance"]
    print(f"Budget baseline: ${budget:.2f} (from topup at {topup_info['ts']})")

    results = []
    for pct in [45, 20, 8]:
        # Inject a mock balance snapshot
        mock_bal = round(budget * pct / 100.0, 2)
        now_ts = int(time.time()) + len(results)
        con.execute(
            "INSERT INTO snapshots (ts, balance, spend, topup) VALUES (?,?,?,?)",
            (now_ts, mock_bal, 0.0, 0.0),
        )
        con.commit()

        test_state_path = tmp + ".state.json"
        # Carry state across iterations to test dedup
        if results:
            Path(test_state_path).write_text(
                json.dumps(test_state, indent=2) + "\n"
            )
        else:
            try:
                os.unlink(test_state_path)
            except FileNotFoundError:
                pass

        # Re-read state fresh
        try:
            test_state = json.loads(Path(test_state_path).read_text())
        except Exception:
            test_state = {}

        alerts = check_thresholds(con, test_state)
        fired_thrs = [a["threshold"] for a in alerts]
        Path(test_state_path).write_text(
            json.dumps(test_state, indent=2) + "\n"
        )
        print(
            f"  mock balance ${mock_bal:.2f} ({pct}% of ${budget:.2f}): "
            f"fired thresholds {fired_thrs}"
        )
        results.append((pct, fired_thrs))

    # Verify expected fires
    ok = True
    # At 45% remaining, 50% threshold should fire
    if 50 not in results[0][1]:
        print("FAIL: 50% threshold did not fire at 45% remaining")
        ok = False
    # At 20% remaining, 25% threshold should fire (50% already fired)
    if 25 not in results[1][1]:
        print("FAIL: 25% threshold did not fire at 20% remaining")
        ok = False
    # 50% should NOT re-fire (dedup)
    if 50 in results[1][1]:
        print("FAIL: 50% threshold re-fired (dedup broken)")
        ok = False
    # At 8% remaining, 10% threshold should fire
    if 10 not in results[2][1]:
        print("FAIL: 10% threshold did not fire at 8% remaining")
        ok = False

    # Cleanup
    os.unlink(tmp)
    for ext in [".state.json"]:
        try:
            os.unlink(tmp + ext)
        except FileNotFoundError:
            pass

    if ok:
        print("PASS: all threshold alerts fired correctly with dedup")
    return ok


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "test":
        sys.exit(0 if test() else 1)
    main()
