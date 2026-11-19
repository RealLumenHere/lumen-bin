#!/usr/bin/env bash
# fleet-auth-ssh-probe.sh — synthetic AUTH-layer SSH probe across the fleet.
# Closes fleet.monitoring_gap: liveness monitors (gatus ICMP/HTTP, Nephthys awareness TCP:22)
# read green even when SSH AUTH is broken (expired key, tailscale-SSH check-mode, sshd misconfig).
# This actually logs in via each node's REAL mgmt path and runs `true`, alerting fail-loud to the
# INDEPENDENT ntfy.sh sink (same topic the Nephthys watchdog uses). Edge-triggered (state file),
# so it pings on transition only (OK->FAIL and FAIL->OK), not every run.
#
# Read-only on every target (runs `true`). No state changed on nodes. Safe to run any time.
set -u
KEY=/home/lumen/.ssh/id_ed25519
SSHO="-i $KEY -o BatchMode=yes -o ConnectTimeout=8 -o StrictHostKeyChecking=accept-new -o PreferredAuthentications=publickey"
NTFY="https://ntfy.sh/lumen-reciprocal-watch-x7q2"
STATE_DIR=/home/lumen/ha/authssh
mkdir -p "$STATE_DIR"
FAIL_THRESHOLD=2   # consecutive failures before alerting (ride out a transient blip)

# node:mgmt-path map (REAL management path per fleet doc; Thoth LAN-only by design)
NODES="
nephthys nephthys@192.168.8.176
isis isis@100.100.129.24
hathor hathor@100.80.71.13
seshat seshat@192.168.8.219
anubis anubis@192.168.8.232
apep apep@100.82.2.72
maat maam@100.105.219.34
thoth thoth@192.168.8.218
thor thor@100.112.151.100
"

ping_ntfy(){ curl -s -m 8 -d "$1" -H "Title: fleet-auth-ssh-probe" "$NTFY" >/dev/null 2>&1; }

now=$(date -u +%H:%M:%SZ)
overall_fail=0
report=""

while read -r name path; do
  [ -z "${name:-}" ] && continue
  cnt_file="$STATE_DIR/${name}.failcount"
  st_file="$STATE_DIR/${name}.state"
  prev_state=$(cat "$st_file" 2>/dev/null || echo OK)
  prev_cnt=$(cat "$cnt_file" 2>/dev/null || echo 0)

  if timeout 14 ssh $SSHO "$path" 'true' </dev/null 2>/dev/null; then
    # success -> reset; alert only if recovering from FAIL
    echo 0 > "$cnt_file"; echo OK > "$st_file"
    if [ "$prev_state" = "FAIL" ]; then
      ping_ntfy "RESOLVED auth-ssh OK again: $name ($path) @ $now"
    fi
    report="${report}${name}=OK "
  else
    cnt=$((prev_cnt+1)); echo "$cnt" > "$cnt_file"
    report="${report}${name}=FAIL(${cnt}) "
    overall_fail=1
    if [ "$cnt" -ge "$FAIL_THRESHOLD" ] && [ "$prev_state" != "FAIL" ]; then
      echo FAIL > "$st_file"
      ping_ntfy "CRITICAL auth-ssh FAILED: $name ($path) — node may be up+pingable but SSH AUTH is broken (key/tailscale-ssh/sshd). @ $now"
    fi
  fi
done <<< "$NODES"

echo "[$now] $report"
exit 0
