#!/bin/bash
# Test-to-failure of tailscale-upgrade on the canary node anubis (idle-enough, LAN 192.168.8.232).
# Ends on the target version. anubis replaced wadjet as the canary node wq#4695 (2026-10-26):
# wadjet was retired, isis/hathor/khepri were already gone (wq#4695), and the canary target must
# never be a node listed in RETIRED_NODES_JSON below -- see check_canary_not_retired().
export WQ_AGENT=deps-drain-98190c
S=${S:-$(cd "$(dirname "$0")/.." && pwd)/tailscale-upgrade}
RETIRED_NODES_JSON=/home/lumen/bin/fleet-retired-nodes.json
CANARY_NODE=anubis
ARGS="${CANARY_ARGS:-anubis anubis 192.168.8.232 100.121.17.5}"
export STATE_DIR=/tmp/ts_test_state_wq2987; rm -rf $STATE_DIR

# wq#4695: fail LOUD and immediately if the configured canary is already known-retired, instead
# of discovering it three ssh calls later. Same list lumen_watchdog.py reads, so a retirement
# recorded once catches both consumers.
check_canary_not_retired() {
  python3 -c "
import json, sys
try:
    retired = json.load(open('$RETIRED_NODES_JSON'))
except Exception as e:
    print(f'REFUSED: cannot read {\"$RETIRED_NODES_JSON\"!r}: {e!r}')
    sys.exit(9)
sys.exit(1 if '$CANARY_NODE' in retired else 0)
" || { echo "REFUSED: canary node '$CANARY_NODE' is listed retired in $RETIRED_NODES_JSON — pick a live replacement before running this harness"; exit 9; }
}
check_canary_not_retired

# wq#4695: an unreachable canary must fail the whole run loudly up front, not limp through case 0
# and either time out opaquely or have a caller mistake a later soft failure for "skip".
if ! ssh -n -o BatchMode=yes -o ConnectTimeout=8 anubis@192.168.8.232 true 2>/dev/null; then
  echo "REFUSED: canary node anubis@192.168.8.232 is unreachable — this harness FAILS, it does not skip. Fix reachability or pick a new canary (see $RETIRED_NODES_JSON) before re-running."
  exit 9
fi

PASS=0; FAIL=0
check() { if [ "$2" = "$3" ]; then echo "  PASS $1 (got $2)"; PASS=$((PASS+1)); else echo "  FAIL $1 (want $3 got $2)"; FAIL=$((FAIL+1)); fi; }
ver() { ssh -o BatchMode=yes -o ConnectTimeout=8 anubis@192.168.8.232 'tailscale version | head -1' 2>/dev/null; }
echo "== start: anubis version=$(ver)  time=$(date -u +%H:%M:%SZ)"
echo "== 0. Thor must be refused (want rc 3)"; bash $S --precheck thor thor 192.168.8.132 100.112.151.100 2>&1 | sed 's/^/    /'; check "thor refused rc" "${PIPESTATUS[0]}" 3
echo "== 1. rollback with no recorded state must refuse loudly (want rc 2)"; bash $S --rollback $ARGS 2>&1 | sed 's/^/    /'; check "no-state rollback rc" "${PIPESTATUS[0]}" 2
echo "== 2. precheck (want rc 0)"; bash $S --precheck $ARGS 2>&1 | sed 's/^/    /'; check "precheck rc" "${PIPESTATUS[0]}" 0
echo "== 3. apply with INJECTED verify failure (want rc 1 and version back to old)"
OLDV=$(ver)
FORCE_VERIFY_FAIL=1 bash $S --apply $ARGS 2>&1 | sed 's/^/    /'; RC=${PIPESTATUS[0]}
check "inject rc" "$RC" 1; check "version restored" "$(ver)" "$OLDV"
echo "== 4. real apply (want rc 0, 1.102.4)"
bash $S --apply $ARGS 2>&1 | sed 's/^/    /'; RC=${PIPESTATUS[0]}
check "apply rc" "$RC" 0; check "version now" "$(ver)" "1.102.4"
echo "== 5. re-apply is idempotent (want rc 0, no change)"
T1=$(ssh -o BatchMode=yes anubis@192.168.8.232 'systemctl show tailscaled -p ActiveEnterTimestamp --value' 2>/dev/null)
bash $S --apply $ARGS 2>&1 | sed 's/^/    /'; check "idempotent rc" "${PIPESTATUS[0]}" 0
check "tailscaled not restarted" "$(ssh -o BatchMode=yes anubis@192.168.8.232 'systemctl show tailscaled -p ActiveEnterTimestamp --value' 2>/dev/null)" "$T1"
echo "== final: anubis version=$(ver) tailnet ping: $(tailscale ping -c 1 --timeout=5s 100.121.17.5 2>&1 | head -1)"
echo "SUMMARY pass=$PASS fail=$FAIL"
rm -rf $STATE_DIR
echo "=== DONE ==="
