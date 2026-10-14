#!/usr/bin/env bash
# tests/test_require_vault_backup.sh — wq#3747: lib/require-vault-backup.sh refuses a rotation
# unless a recent backup exists, and (when the live file is given) unless that backup IS the
# live value. Dummy values only; every path is a scratch sandbox via RVB_* overrides.
set -uo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
LIB="$HERE/../lib/require-vault-backup.sh"
FAIL=0
ok(){ echo "   ok: $1"; }
bad(){ echo "   FAIL: $1"; FAIL=1; }
SBOX=$(mktemp -d); trap 'rm -rf "$SBOX"' EXIT
export RVB_BACKUP_DIRS="$SBOX/backups" RVB_LOG="$SBOX/rotation.log"
mkdir -p "$SBOX/backups"
LIVE="$SBOX/live.token"; printf 'dummy-current-value-0001' > "$LIVE"

echo "-- no backup anywhere: refused (exit 2) --"
"$LIB" k1 "$LIVE" >/dev/null 2>&1; rc=$?
[ "$rc" = 2 ] && ok "exit 2" || bad "expected 2 got $rc"

echo "-- backup holding an OLDER value: refused --"
printf 'dummy-older-value-0000' > "$SBOX/backups/k1.bak.20260101_000000"
"$LIB" k1 "$LIVE" >/dev/null 2>&1; rc=$?
[ "$rc" = 2 ] && ok "stale-value backup does not open the gate" || bad "expected 2 got $rc"

echo "-- sibling .bak with older value: refused --"
printf 'dummy-older-value-0000' > "$LIVE.bak.20260101_000000"
"$LIB" k2 "$LIVE" >/dev/null 2>&1; rc=$?
[ "$rc" = 2 ] && ok "sibling stale .bak refused" || bad "expected 2 got $rc"

echo "-- backup identical to live: allowed --"
cp "$LIVE" "$SBOX/backups/k1.bak.20260102_000000"
"$LIB" k1 "$LIVE" >/dev/null 2>&1; rc=$?
[ "$rc" = 0 ] && ok "exit 0" || bad "expected 0 got $rc"

echo "-- sibling .bak identical to live: allowed --"
cp "$LIVE" "$LIVE.bak.20260102_000000"
"$LIB" k2 "$LIVE" >/dev/null 2>&1; rc=$?
[ "$rc" = 0 ] && ok "sibling matching .bak allowed" || bad "expected 0 got $rc"

echo "-- no LIVE_FILE: name + age only --"
"$LIB" k3 >/dev/null 2>&1; rc=$?
[ "$rc" = 2 ] && ok "k3 refused" || bad "k3 expected 2 got $rc"
touch "$SBOX/backups/k3.bak.1"
"$LIB" k3 >/dev/null 2>&1; rc=$?
[ "$rc" = 0 ] && ok "k3 allowed by name" || bad "k3 expected 0 got $rc"

echo "-- usage error --"
"$LIB" >/dev/null 2>&1; rc=$?
[ "$rc" = 1 ] && ok "exit 1" || bad "expected 1 got $rc"

echo "-- audit log never holds the plaintext --"
if grep -q "dummy-current-value-0001" "$RVB_LOG"; then bad "plaintext in log"; else ok "log holds hashes only"; fi
grep -q "REFUSED" "$RVB_LOG" && grep -q "ALLOWED" "$RVB_LOG" && ok "REFUSED and ALLOWED both logged" || bad "log missing entries"

[ "$FAIL" = 0 ] && { echo "ALL TESTS PASSED"; exit 0; } || { echo "SOME TESTS FAILED"; exit 1; }
