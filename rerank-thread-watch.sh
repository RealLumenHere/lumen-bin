#!/usr/bin/env bash
# wq#596 — 48h auto-rollback watch for the pinned 16-thread rerank arm.
#
# Deliberately journald-INDEPENDENT: journald on this box retains only ~11h
# (supergateway logs ~774k lines/day into the user journal against
# SystemMaxUse=1G), so a 48h watch that read journald would be blind. State is
# kept in its own append-only CSV + a small state file.
#
# TRIP CONDITIONS (Frank-specified in wq#596):
#   A) gateway restart rate above baseline. Baseline = 0 restarts in 6.62d
#      (desktop-commander-mcp ActiveEnterTimestamp == boot, NRestarts=0), so ANY
#      restart trips. Detected by ActiveEnterTimestamp moving off the recorded
#      baseline stamp -- this catches BOTH policy restarts and explicit
#      `systemctl restart` (positive-control verified 2026-08-14).
#   B) memory-stall PSI full avg60 >= 25% (mcp-pressure-guard's own threshold)
#      on 2 CONSECUTIVE samples. Two samples, because avg60 can spike from an
#      unrelated batch job; the 2026-07-26 signature was SUSTAINED ~58%.
# Either trip -> rerank-threads-rollback.sh + ntfy. Rollback is one-shot
# (guarded by DONE marker) so a later unrelated restart cannot loop.
set -u
CSV=/home/lumen/minerva/v4/reports/596_watch.csv
LOG=/home/lumen/minerva/v4/reports/596_watch_events.log
STATE=/home/lumen/minerva/v4/reports/596_watch_state
DONE=/home/lumen/minerva/v4/reports/596_watch_ROLLED_BACK
BASE=/home/lumen/minerva/v4/reports/596_watch_baseline
NTFY=http://127.0.0.1:8080/lumen-fleet
PSI_THRESH=${PSI_THRESH:-25}
ts() { date -u +%Y-%m-%dT%H:%M:%SZ; }

[ -f "$BASE" ] || systemctl show desktop-commander-mcp -p ActiveEnterTimestamp --value > "$BASE"
BASE_STAMP=$(cat "$BASE")

psi=$(awk '/^full/{for(i=1;i<=NF;i++)if($i ~ /^avg60=/){sub("avg60=","",$i);print $i;exit}}' /proc/pressure/memory)
gw_stamp=$(systemctl show desktop-commander-mcp -p ActiveEnterTimestamp --value)
gw_nrestart=$(systemctl show desktop-commander-mcp -p NRestarts --value)
gw_mem=$(systemctl show desktop-commander-mcp -p MemoryCurrent --value)
P=$(systemctl show lumen-rerank -p MainPID --value)
case "$P" in ''|0|*[!0-9]*) P=0;; esac
if [ "$P" -gt 0 ] && [ -d "/proc/$P" ]; then
  thr=$(ls /proc/"$P"/task 2>/dev/null | wc -l)
  rss=$(awk '/^VmRSS:/{print $2}' /proc/"$P"/status 2>/dev/null)
else thr=0; rss=0; fi
hc=$(curl -s -m 5 -o /dev/null -w '%{http_code}' http://127.0.0.1:8140/health || echo 000)

prev=$(cat "$STATE" 2>/dev/null || echo 0)
case "$prev" in ''|*[!0-9]*) prev=0;; esac
psi_hot=0
awk -v p="${psi:-0}" -v t="$PSI_THRESH" 'BEGIN{exit !(p+0>=t+0)}' && psi_hot=1
echo "$psi_hot" > "$STATE"

gw_moved=0
[ "$gw_stamp" != "$BASE_STAMP" ] && gw_moved=1

[ -f "$CSV" ] || echo "ts_utc,psi_full_avg60,psi_hot,psi_prev_hot,gw_active_enter,gw_moved,gw_nrestarts,gw_mem_bytes,rerank_pid,rerank_threads,rerank_rss_kb,rerank_health" > "$CSV"
echo "$(ts),${psi:-NA},$psi_hot,$prev,${gw_stamp// /_},$gw_moved,${gw_nrestart:-NA},${gw_mem:-NA},$P,$thr,${rss:-0},$hc" >> "$CSV"

[ -f "$DONE" ] && exit 0

# 48h window expiry — stop watching, loudly, once.
BASE_AGE=$(( $(date +%s) - $(stat -c %Y "$BASE") ))
# Frank decision C 2026-08-14: 7d window (was 48h) — the 2026-07-26 incident took
# 5 days to manifest; 7d is the only window that would have caught it.
if [ "$BASE_AGE" -ge 604800 ]; then
  echo "$(ts) WATCH_WINDOW_COMPLETE (7d, no trip) — disabling timer" >> "$LOG"
  curl -sf -m 5 -d "wq#596 7d thread watch COMPLETE with no trip — 16 threads held clean" "$NTFY" >/dev/null 2>&1 || true
  systemctl disable --now lumen-rerank-thread-watch.timer >/dev/null 2>&1
  date -u +%Y-%m-%dT%H:%M:%SZ > "$DONE"
  exit 0
fi

trip=""
[ "$gw_moved" = "1" ] && trip="gateway_restarted(was:${BASE_STAMP} now:${gw_stamp})"
[ "$psi_hot" = "1" ] && [ "$prev" = "1" ] && trip="${trip:+$trip }psi_full_avg60=${psi}>=${PSI_THRESH}_x2"
[ -z "$trip" ] && exit 0

echo "$(ts) TRIP: $trip" >> "$LOG"
curl -sf -m 5 -d "wq#596 WATCH TRIP: $trip — auto-rolling back rerank to 4 threads" "$NTFY" >/dev/null 2>&1 || true
/home/lumen/bin/rerank-threads-rollback.sh "watch_trip:$trip"
rc=$?
date -u +%Y-%m-%dT%H:%M:%SZ > "$DONE"
echo "$(ts) rollback exit=$rc (one-shot marker written)" >> "$LOG"
exit 0
