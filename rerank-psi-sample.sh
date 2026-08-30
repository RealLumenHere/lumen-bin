#!/usr/bin/env bash
# wq#596 — PSI + rerank/gateway sampler. Writes CSV, one row per interval.
# Usage: rerank-psi-sample.sh <out.csv> <minutes> [interval_sec] [label]
set -u
OUT="${1:?out csv}"; MINS="${2:?minutes}"; IVL="${3:-30}"; LABEL="${4:-unlabeled}"
END=$(( $(date +%s) + MINS*60 ))
[ -f "$OUT" ] || echo "ts_utc,label,psi_full_avg10,psi_full_avg60,psi_full_avg300,psi_some_avg60,rerank_pid,rerank_threads,rerank_rss_kb,gw_mem_bytes,gw_active_enter,gw_nrestarts,load1,mem_avail_kb,swap_used_kb" > "$OUT"
while [ "$(date +%s)" -lt "$END" ]; do
  f=$(awk '/^full/{print}' /proc/pressure/memory)
  s=$(awk '/^some/{print}' /proc/pressure/memory)
  fa10=$(echo "$f" | sed -n 's/.*avg10=\([0-9.]*\).*/\1/p')
  fa60=$(echo "$f" | sed -n 's/.*avg60=\([0-9.]*\).*/\1/p')
  fa300=$(echo "$f" | sed -n 's/.*avg300=\([0-9.]*\).*/\1/p')
  sa60=$(echo "$s" | sed -n 's/.*avg60=\([0-9.]*\).*/\1/p')
  rp=$(systemctl show lumen-rerank -p MainPID --value 2>/dev/null)
  case "$rp" in ''|0|*[!0-9]*) rp=0;; esac
  if [ "$rp" -gt 0 ] && [ -d "/proc/$rp" ]; then
    rt=$(ls /proc/"$rp"/task 2>/dev/null | wc -l)
    rr=$(awk '/^VmRSS:/{print $2}' /proc/"$rp"/status 2>/dev/null)
  else rt=0; rr=0; fi
  gm=$(systemctl show desktop-commander-mcp -p MemoryCurrent --value 2>/dev/null)
  ge=$(systemctl show desktop-commander-mcp -p ActiveEnterTimestamp --value 2>/dev/null | tr ' ' '_')
  gn=$(systemctl show desktop-commander-mcp -p NRestarts --value 2>/dev/null)
  l1=$(awk '{print $1}' /proc/loadavg)
  ma=$(awk '/^MemAvailable:/{print $2}' /proc/meminfo)
  st=$(awk '/^SwapTotal:/{t=$2} /^SwapFree:/{f=$2} END{print t-f}' /proc/meminfo)
  echo "$(date -u +%Y-%m-%dT%H:%M:%SZ),$LABEL,${fa10:-NA},${fa60:-NA},${fa300:-NA},${sa60:-NA},$rp,$rt,${rr:-0},${gm:-NA},${ge:-NA},${gn:-NA},$l1,$ma,$st" >> "$OUT"
  sleep "$IVL"
done
echo "SAMPLER_DONE $LABEL $(date -u +%Y-%m-%dT%H:%M:%SZ)" >> "$OUT"
