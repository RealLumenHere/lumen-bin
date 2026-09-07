#!/bin/bash
# fleet-roll-reboot.sh — serial, fail-safe rolling restart of the peripheral fleet.
#
# wq#1456: the roster comes from the fleet inventory (`fleet-inv ssh-list`, DESIGN_SOT.md) —
# no NODES list lives here. Selection: MONITORED + status active + not governance.never_restart
# (thor: Frank's workstation) + not governance.crown_jewel (maat, lumen) + not this host. A box
# that is parked/standby/bench/decommissioned never appears, so the fail-safe STOP below only
# ever fires on a box the inventory says should be up.   `--list` prints the roster and exits.
LOG=/tmp/fleet_reboot.log
FLEET_INV=${FLEET_INV:-/home/lumen/bin/fleet-inv}
ROSTER=$("$FLEET_INV" ssh-list --status active --maintenance --no-crown-jewel --exclude-self) \
  || { echo "fleet-roll-reboot: fleet-inv ssh-list failed — refusing to roll without a roster" >&2; exit 1; }
[ -n "$ROSTER" ] || { echo "fleet-roll-reboot: empty roster from inventory — refusing to run" >&2; exit 1; }
if [ "${1:-}" = "--list" ]; then printf '%s\n' "$ROSTER"; exit 0; fi
NODES=$(printf '%s\n' "$ROSTER" | cut -f2 | tr '\n' ' ')
COUNT=$(printf '%s\n' "$ROSTER" | grep -c .)
echo "[$(date -u)] START roll $COUNT peripherals from inventory: $NODES(fail-safe)" >> "$LOG"
for nt in $NODES; do n="${nt%@*}"
  ssh -o ConnectTimeout=10 -o BatchMode=yes "$nt" 'sudo -n true' >>"$LOG" 2>&1 || { echo "[$(date -u)] STOP: $n sudo/ssh fail" >>"$LOG"; exit 1; }
  echo "[$(date -u)] $n rebooting" >> "$LOG"
  ssh -o ConnectTimeout=10 -o BatchMode=yes "$nt" 'sudo -n systemctl reboot' >>"$LOG" 2>&1
  sleep 25; back=0
  for i in $(seq 1 30); do ssh -o ConnectTimeout=8 -o BatchMode=yes "$nt" 'uptime' >/dev/null 2>&1 && { back=1; break; }; sleep 6; done
  [ "$back" = 1 ] || { echo "[$(date -u)] STOP: $n no return ~200s - MANUAL CHECK" >>"$LOG"; exit 2; }
  echo "[$(date -u)] $n BACK ($(ssh -o ConnectTimeout=8 -o BatchMode=yes "$nt" 'systemctl is-system-running' 2>/dev/null))" >> "$LOG"; sleep 5
done
echo "[$(date -u)] DONE all $COUNT back" >> "$LOG"
