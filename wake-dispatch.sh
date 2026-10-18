#!/usr/bin/env bash
# /home/lumen/bin/wake-dispatch.sh  (wq#3685, mac-ops 2026-10-08)
# Wake-on-demand for the Mac Pro inference nodes. Does NOT modify dispatch-batch; callable
# from it (ExecStartPre / direct call, preferably backgrounded - a cold wake takes 2-3 min) or by
# hand. NEVER blocks a ticket: every failure is logged and `check`/`done` still exit 0 so LiteLLM
# fallback handles the ticket.
#
#   wake-dispatch.sh check [--dry-run]         scan open/claimed wq tickets whose `model` maps to a Mac;
#                                              wake each target that is offline
#   wake-dispatch.sh wake <node> [--dry-run]   force-wake one node (rc 1 on failure)
#   wake-dispatch.sh done <node> [--dry-run]   after a ticket completes: if no other pending ticket maps
#                                              to <node> and <node> is an auto-sleep node, start
#                                              auto-sleep.service there (it re-checks idle; powers off only
#                                              after IDLE_MINUTES idle)
#   wake-dispatch.sh status                    per-node online state + pending tickets
#
# WAKE METHOD (in order):
#   1. Wake-on-LAN magic packet (Lumen LAN ens1 -> 192.168.8.255). wq#3683 test 2026-10-08: S5 WoL did
#      NOT wake hathor (4 packet variants; plug showed 0.0-0.5W) - so step 1 is attempted but is
#      detected as ineffective after WOL_PROBE_SECS (no power surge on the smart plug) and skipped fast.
#   2. Smart-plug power cycle (fleet-standard remote recovery, `plug-ctl`), ONLY when the node does not
#      answer ping/ssh AND its plug reads < PLUG_OFF_W watts (so we can never cut power to a live box).
#      Boots in ~2-3 min (Restore-on-AC-power).
# Config: /home/lumen/.secrets/mac-wol.env  (<NODE>_MAC, <NODE>_IP, optional <NODE>_PLUG e.g. p3)
# Disable everything: touch /home/lumen/.wake-dispatch.disable
set -u
WOL_ENV=${WOL_ENV:-/home/lumen/.secrets/mac-wol.env}      # overridable for tests
source "$WOL_ENV" 2>/dev/null || { echo "$WOL_ENV missing" >&2; exit 1; }
QDB=${QDB:-/home/lumen/agents/work-queue/queue.db}
LOG=${LOG:-/home/lumen/logs/wake-dispatch.log}
BCAST=${BCAST:-192.168.8.255}
WAKE_POLL_SECS=${WAKE_POLL_SECS:-180}   # max wait for ssh per wake method
WOL_PROBE_SECS=${WOL_PROBE_SECS:-25}    # after WoL: if the plug still reads < PLUG_OFF_W the packet did nothing
PLUG_OFF_W=${PLUG_OFF_W:-15}
TS_REMEDIATE_SECS=${TS_REMEDIATE_SECS:-45}  # after tailscaled restart: max wait for tailnet port
PORT=${PORT:-11434}
# Nodes that auto-sleep.timer manages. isis is WARM STANDBY by Frank directive (2026-09-18):
# it is woken if found offline but is NEVER put to sleep by this tool.
AUTOSLEEP_NODES=${AUTOSLEEP_NODES:-"hathor"}
# model (ticket `model` field / LiteLLM name) -> node. Deliberately excludes qwen-local
# (least-busy across khepri/isis/hathor - no single target) and cloud models.
model_node(){ case "$1" in
  qwen3:32b|qwen3.6:35b-a3b|qwen36-local|qwen36-live|isis-qwen3-32b|isis-qwen36) echo isis ;;
  llama3.1:8b|hathor-llama8b|hathor-lfm-1b|hathor-embed) echo hathor ;;
  *) echo "" ;; esac; }

mkdir -p "$(dirname "$LOG")"
log(){ echo "$(date '+%F %T %Z') [wake-dispatch] $*" | tee -a "$LOG" >&2; }
DRY=0; args=()
for a in "$@"; do [ "$a" = "--dry-run" ] && DRY=1 || args+=("$a"); done
set -- "${args[@]:-}"
cmd=${1:-status}; node=${2:-}

upper(){ echo "$1" | tr 'a-z' 'A-Z'; }
nodevar(){ local v="$(upper "$1")_$2"; echo "${!v:-}"; }
nodes(){ grep -oE '^[A-Z0-9]+_MAC=' "$WOL_ENV" | sed 's/_MAC=//' | tr 'A-Z' 'a-z'; }
# "online" = what LiteLLM sees: ollama on the TAILNET address (LAN :11434 is firewalled by the wq3108 INPUT-DROP policy)
online(){ local ip; ip=$(nodevar "$1" TS_IP); [ -n "$ip" ] || ip=$(nodevar "$1" IP); [ -n "$ip" ] && nc -z -w3 "$ip" "$PORT" 2>/dev/null; }
ssh_up(){ ssh -o BatchMode=yes -o ConnectTimeout=4 "$1@$(nodevar "$1" IP)" true 2>/dev/null; }   # LAN path (does not depend on tailscaled)
plug_watts(){ /home/lumen/bin/plug-ctl "$1" watts 2>/dev/null | tail -1 | awk '{print int($3)}'; }

# tickets whose model maps to a node -> "id:model" lines
pending_for(){ local want="$1" id m
  [ -r "$QDB" ] || return 0
  sqlite3 -separator ' ' "file:$QDB?mode=ro" "select id, model from tasks where status in ('open','claimed') and model is not null and model != '';" 2>/dev/null |
  while read -r id m; do [ "$(model_node "$m")" = "$want" ] && echo "$id:$m"; done; }

poll_ssh(){ local n="$1" cap="$2" t0; t0=$(date +%s)
  until ssh_up "$n"; do sleep 5; [ $(( $(date +%s)-t0 )) -ge "$cap" ] && return 1; done; return 0; }

wake_node(){ local n="$1" mac ip plug t0 w up
  mac=$(nodevar "$n" MAC); ip=$(nodevar "$n" IP); plug=$(nodevar "$n" PLUG)
  [ -n "$mac" ] && [ -n "$ip" ] || { log "ERROR $n: no ${n^^}_MAC/${n^^}_IP in mac-wol.env"; return 1; }
  if online "$n"; then log "$n already online (:$PORT answering)"; return 0; fi
  exec 9>"/tmp/wake-dispatch.$n.lock"; flock -w 240 9 || { log "ERROR $n: lock timeout"; return 1; }   # one wake at a time per node
  if online "$n"; then log "$n came online while waiting for lock"; return 0; fi
  if ssh_up "$n"; then log "$n: ssh up but :$PORT down (service issue, not power) - not waking"; return 1; fi
  if [ "$DRY" = 1 ]; then log "DRY-RUN: would WoL $n ($mac via $BCAST), then plug-cycle ${plug:-<no plug configured>} if WoL ineffective"; return 0; fi
  t0=$(date +%s)
  log "$n offline - sending WoL magic packet ($mac -> $BCAST)"
  wakeonlan -i "$BCAST" "$mac" >/dev/null 2>&1 || log "WARN $n: wakeonlan command failed"
  if [ -n "$plug" ]; then
    sleep "$WOL_PROBE_SECS"; w=$(plug_watts "$plug"); w=${w:-0}
    if [ "$w" -ge "$PLUG_OFF_W" ]; then log "$n: plug $plug reads ${w}W after WoL - box is powering up"
    else
      log "$n: WoL ineffective (plug $plug still ${w}W < ${PLUG_OFF_W}W after ${WOL_PROBE_SECS}s) - falling back to plug power-cycle"
      if ping -c2 -W2 "$ip" >/dev/null 2>&1; then log "ERROR $n: answers ping - refusing to cut power"; return 1; fi
      if [ "$w" -ge "$PLUG_OFF_W" ]; then log "ERROR $n: plug reading changed - refusing"; return 1; fi
      /home/lumen/bin/plug-ctl "$plug" off >/dev/null 2>&1; sleep 6; /home/lumen/bin/plug-ctl "$plug" on >/dev/null 2>&1
      log "$n: plug $plug cycled (off 6s -> on)"
    fi
  else log "WARN $n: no ${n^^}_PLUG configured - WoL only"; fi
  poll_ssh "$n" "$WAKE_POLL_SECS" || { log "ERROR $n: no ssh after ${WAKE_POLL_SECS}s - NOT blocking tickets (LiteLLM fallback handles it)"; return 1; }
  up=$(( $(date +%s)-t0 ))
  local t1; t1=$(date +%s)
  until online "$n"; do sleep 3; [ $(( $(date +%s)-t1 )) -ge 60 ] && { log "WARN $n: ssh up (${up}s) but :$PORT not answering after +60s"; return 0; }; done
  # LiteLLM reaches Macs over the tailnet; a cold boot can leave tailscaled 'network down' (seen on hathor 2026-10-08).
  # Auto-remediation (wq#3710): restart tailscaled on the node, re-probe, WARN only if still down.
  local tsip; tsip=$(nodevar "$n" TS_IP)
  if [ -n "$tsip" ] && ! nc -z -w5 "$tsip" "$PORT" 2>/dev/null; then
    log "$n: awake on LAN but tailnet $tsip:$PORT unreachable — restarting tailscaled (boot race remediation)"
    if ssh -o BatchMode=yes -o ConnectTimeout=6 "$n" 'sudo -n systemctl restart tailscaled' 2>/dev/null; then
      local ts_t0; ts_t0=$(date +%s)
      local ts_ok=0
      while [ $(( $(date +%s)-ts_t0 )) -lt "$TS_REMEDIATE_SECS" ]; do
        sleep 3
        if nc -z -w3 "$tsip" "$PORT" 2>/dev/null; then ts_ok=1; break; fi
      done
      if [ "$ts_ok" = 1 ]; then
        log "OK $n: tailscaled restart fixed boot race — $tsip:$PORT up after $(( $(date +%s)-ts_t0 ))s"
      else
        log "WARN $n: tailscaled restart did not fix tailnet — $tsip:$PORT still unreachable after ${TS_REMEDIATE_SECS}s"
      fi
    else
      log "WARN $n: cannot restart tailscaled via ssh (sudo -n systemctl restart tailscaled failed)"
    fi
  fi
  log "OK $n awake: ssh up ${up}s, :$PORT up at $(( $(date +%s)-t0 ))s"; return 0; }

case "$cmd" in
  check)
    [ -e /home/lumen/.wake-dispatch.disable ] && { log "disabled by flag file"; exit 0; }
    for n in $(nodes); do
      p=$(pending_for "$n" | tr '\n' ' ')
      if [ -n "$p" ]; then
        log "$n: pending heavy-model ticket(s): $p"
        wake_node "$n" || log "$n: wake failed - continuing, tickets NOT blocked"
      fi
    done
    exit 0 ;;
  wake)
    [ -n "$node" ] || { echo "usage: wake <node>" >&2; exit 2; }
    wake_node "$node"; exit $? ;;
  done)
    [ -n "$node" ] || { echo "usage: done <node>" >&2; exit 2; }
    p=$(pending_for "$node" | tr '\n' ' ')
    if [ -n "$p" ]; then log "$node: still has pending ticket(s): $p - leaving it up"; exit 0; fi
    case " $AUTOSLEEP_NODES " in *" $node "*) ;; *) log "$node: not an auto-sleep node (warm standby) - not triggering"; exit 0 ;; esac
    if [ "$DRY" = 1 ]; then log "DRY-RUN: would start auto-sleep.service on $node"; exit 0; fi
    if ssh -o BatchMode=yes -o ConnectTimeout=6 "$node" 'sudo -n systemctl start --no-block auto-sleep.service' 2>/dev/null; then
      log "$node: no pending tickets - auto-sleep evaluation triggered (powers off after idle window)"
    else log "WARN $node: could not trigger auto-sleep (unreachable?)"; fi
    exit 0 ;;
  status)
    for n in $(nodes); do
      st=OFFLINE; online "$n" && st=online
      p=$(pending_for "$n" | tr '\n' ' ')
      echo "$n  $st  mac=$(nodevar "$n" MAC)  ip=$(nodevar "$n" IP)  plug=$(nodevar "$n" PLUG)  autosleep=$(case " $AUTOSLEEP_NODES " in *" $n "*) echo yes;; *) echo no;; esac)  pending=[${p}]"
    done ;;
  *) echo "usage: $0 {check|wake <node>|done <node>|status} [--dry-run]" >&2; exit 2 ;;
esac
