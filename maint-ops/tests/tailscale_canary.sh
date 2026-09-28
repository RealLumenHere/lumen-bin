#!/bin/bash
# Test-to-failure of tailscale-upgrade on the canary node wadjet (idle, LAN path 192.168.8.173). Ends on the target version.
export WQ_AGENT=deps-drain-98190c
S=${S:-$(cd "$(dirname "$0")/.." && pwd)/tailscale-upgrade}
ARGS="${CANARY_ARGS:-wadjet wadjet 192.168.8.173 100.116.217.15}"
export STATE_DIR=/tmp/ts_test_state_wq2987; rm -rf $STATE_DIR
PASS=0; FAIL=0
check() { if [ "$2" = "$3" ]; then echo "  PASS $1 (got $2)"; PASS=$((PASS+1)); else echo "  FAIL $1 (want $3 got $2)"; FAIL=$((FAIL+1)); fi; }
ver() { ssh -o BatchMode=yes -o ConnectTimeout=8 wadjet@192.168.8.173 'tailscale version | head -1' 2>/dev/null; }
echo "== start: wadjet version=$(ver)  time=$(date -u +%H:%M:%SZ)"
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
T1=$(ssh -o BatchMode=yes wadjet@192.168.8.173 'systemctl show tailscaled -p ActiveEnterTimestamp --value' 2>/dev/null)
bash $S --apply $ARGS 2>&1 | sed 's/^/    /'; check "idempotent rc" "${PIPESTATUS[0]}" 0
check "tailscaled not restarted" "$(ssh -o BatchMode=yes wadjet@192.168.8.173 'systemctl show tailscaled -p ActiveEnterTimestamp --value' 2>/dev/null)" "$T1"
echo "== final: wadjet version=$(ver) tailnet ping: $(tailscale ping -c 1 --timeout=5s 100.116.217.15 2>&1 | head -1)"
echo "SUMMARY pass=$PASS fail=$FAIL"
rm -rf $STATE_DIR
echo "=== DONE ==="
