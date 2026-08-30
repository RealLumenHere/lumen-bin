#!/usr/bin/env bash
# wq#596 — ROLLBACK: return lumen-rerank to the 4-thread cap. Runnable, idempotent.
# Usage: rerank-threads-rollback.sh ["reason"]
# Restores the pre-596 state by disabling the zz-threads-16.conf drop-in; the
# 2026-07-26 resource-caps.conf (OMP=4, MemoryHigh/Max) then governs again.
set -u
REASON="${1:-manual}"
DROPIN=/etc/systemd/system/lumen-rerank.service.d/zz-threads-16.conf
LOG=/home/lumen/minerva/v4/reports/596_watch_events.log
NTFY=http://127.0.0.1:8080/lumen-fleet
ts() { date -u +%Y-%m-%dT%H:%M:%SZ; }
say() { echo "$(ts) $*" | tee -a "$LOG"; }

if [ ! -f "$DROPIN" ]; then
  say "ROLLBACK noop — $DROPIN absent (already rolled back). reason=$REASON"
else
  sudo -n mv "$DROPIN" "${DROPIN}.disabled.$(date -u +%Y%m%d_%H%M%SZ)" || { say "ROLLBACK FAILED — mv"; exit 1; }
  sudo -n systemctl daemon-reload || { say "ROLLBACK FAILED — daemon-reload"; exit 1; }
  say "ROLLBACK applied — drop-in disabled. reason=$REASON"
fi

sudo -n systemctl restart lumen-rerank || { say "ROLLBACK FAILED — restart"; exit 1; }
for i in $(seq 1 30); do
  hc=$(curl -s -m 5 -o /dev/null -w '%{http_code}' http://127.0.0.1:8140/health || echo 000)
  [ "$hc" = "200" ] && break
  sleep 2
done
P=$(systemctl show lumen-rerank -p MainPID --value)
THR=$(ls /proc/"$P"/task 2>/dev/null | wc -l)
OMP=$(tr '\0' '\n' < /proc/"$P"/environ 2>/dev/null | sed -n 's/^OMP_NUM_THREADS=//p')
say "ROLLBACK verify: health=$hc pid=$P threads=$THR OMP_NUM_THREADS=${OMP:-?} active=$(systemctl is-active lumen-rerank)"
curl -sf -m 5 -d "wq#596 rerank ROLLBACK to 4 threads ($REASON) health=$hc threads=$THR" "$NTFY" >/dev/null 2>&1 || true
[ "$hc" = "200" ] || exit 2
exit 0
