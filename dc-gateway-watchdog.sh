#!/usr/bin/env bash
# dc-gateway-watchdog.sh — Restarts dc-gateway if MemoryCurrent > 2GB
# Deployed orch13-cont 2026-11-13. dc-gateway leaks (206MB->4.17GB in 10h)
set -euo pipefail
UNIT="dc-gateway.service"
THRESHOLD_BYTES=$((2 * 1024 * 1024 * 1024))
LOG_TAG="dc-gateway-watchdog"

if ! systemctl is-active --quiet "$UNIT" 2>/dev/null; then
    exit 0
fi

MEM_CURRENT=$(systemctl show "$UNIT" --property=MemoryCurrent --value 2>/dev/null || echo "0")
[[ "$MEM_CURRENT" == "[not set]" || -z "$MEM_CURRENT" ]] && exit 0

MEM_MB=$(( MEM_CURRENT / 1024 / 1024 ))

if (( MEM_CURRENT > THRESHOLD_BYTES )); then
    logger -t "$LOG_TAG" "ALERT: ${MEM_MB}MB (>2048MB) — restarting"
    systemctl restart "$UNIT"
    logger -t "$LOG_TAG" "OK: restarted (was ${MEM_MB}MB)"
    /home/lumen/bin/ntfy-send -t lumen-fleet -T "dc-gateway watchdog" "Restarted: was ${MEM_MB}MB" 2>/dev/null || true
else
    logger -t "$LOG_TAG" "OK: ${MEM_MB}MB"
fi
