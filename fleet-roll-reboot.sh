#!/bin/bash
LOG=/tmp/fleet_reboot.log
NODES="apep@100.82.2.72 anubis@100.121.17.5 sobek@100.86.156.91 isis@100.100.129.24 bes@100.110.156.91 thoth@100.75.1.108 seshat@100.121.243.124"
echo "[$(date -u)] START roll 7 peripherals (nephthys excluded; fail-safe)" >> "$LOG"
for nt in $NODES; do n="${nt%@*}"
  ssh -o ConnectTimeout=10 -o BatchMode=yes "$nt" 'sudo -n true' >>"$LOG" 2>&1 || { echo "[$(date -u)] STOP: $n sudo/ssh fail" >>"$LOG"; exit 1; }
  echo "[$(date -u)] $n rebooting" >> "$LOG"
  ssh -o ConnectTimeout=10 -o BatchMode=yes "$nt" 'sudo -n systemctl reboot' >>"$LOG" 2>&1
  sleep 25; back=0
  for i in $(seq 1 30); do ssh -o ConnectTimeout=8 -o BatchMode=yes "$nt" 'uptime' >/dev/null 2>&1 && { back=1; break; }; sleep 6; done
  [ "$back" = 1 ] || { echo "[$(date -u)] STOP: $n no return ~200s - MANUAL CHECK" >>"$LOG"; exit 2; }
  echo "[$(date -u)] $n BACK ($(ssh -o ConnectTimeout=8 -o BatchMode=yes "$nt" 'systemctl is-system-running' 2>/dev/null))" >> "$LOG"; sleep 5
done
echo "[$(date -u)] DONE all 7 back" >> "$LOG"
