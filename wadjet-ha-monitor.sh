#!/bin/bash
# wadjet-ha-monitor.sh — Vault HA auto-promote monitor (runs on Lumen, root, via systemd timer ~30s)
# Probes Ma'at (live vault primary). On sustained failure, fences Ma'at, promotes Wadjet,
# flips the Cloudflare tunnel origin vault.lumennode.uk -> Wadjet, starts Wadjet's Vaultwarden.
# Single-writer fence = the CF tunnel origin (Lumen owns the one public ingress).
# FAILBACK IS MANUAL (see wadjet-ha-failback.sh). Auto-failback is intentionally NOT implemented.
#
# Modes:
#   (no args)   normal probe/decide/act cycle (called by the timer)
#   --status    print current HA state and live probe results, take NO action
#   --dry-run   run the decision logic but only LOG what it would do; never act
set -uo pipefail

HA_DIR=/home/lumen/ha
ENV_FILE="$HA_DIR/ha.env"
[ -r "$ENV_FILE" ] || { echo "missing $ENV_FILE"; exit 2; }
# shellcheck disable=SC1090
source "$ENV_FILE"

PRIMARY_FILE="$HA_DIR/primary"
COUNT_FILE="$HA_DIR/maat_fail_count"
ENABLE_FLAG="$HA_DIR/AUTO_PROMOTE_ENABLED"
LOG="$HA_DIR/monitor.log"
LOCK="$HA_DIR/.failover.lock"

MODE="${1:-run}"
SSH_OPTS=(-o IdentitiesOnly=yes -o ConnectTimeout=6 -o StrictHostKeyChecking=accept-new -i "$SSH_KEY")

log(){ echo "$(date '+%Y-%m-%d %H:%M:%S %Z') $*" >> "$LOG"; }
notify(){ # title priority message
  curl -s -m 5 -H "Title: $1" -H "Priority: $2" -d "$3" "$NTFY_URL" >/dev/null 2>&1 || true
}

probe_maat_ok(){
  timeout "$PROBE_TIMEOUT" bash -c "exec 3<>/dev/tcp/$MAAT_LAN/5432" 2>/dev/null || return 1  # quiet TCP-open; was pg_isready (root@root pg_hba FATAL spam)
  curl -sf -m "$PROBE_TIMEOUT" -o /dev/null "$VAULT_ORIGIN_MAAT/alive" || return 1
  return 0
}
probe_wadjet_ok(){
  timeout "$PROBE_TIMEOUT" bash -c "exec 3<>/dev/tcp/$WADJET_LAN/5432" 2>/dev/null || return 1  # quiet TCP-open; was pg_isready
  curl -sf -m "$PROBE_TIMEOUT" -o /dev/null "$VAULT_ORIGIN_WADJET/alive" || return 1
  return 0
}

fence_maat(){ # best-effort: stop Ma'at VW + PG so a half-alive Ma'at cannot serve. If Ma'at is down, SSH fails = already fenced.
  local pw; pw=$(cat "$HA_DIR/.fleet_pw" 2>/dev/null)
  if timeout 20 ssh "${SSH_OPTS[@]}" "$MAAT_SSH" \
      "echo '$pw' | sudo -S -p '' bash -c 'docker stop vaultwarden 2>/dev/null; systemctl stop postgresql@18-main 2>/dev/null; echo FENCED'" >>"$LOG" 2>&1; then
    log "fence_maat: Ma'at reachable, VW+PG stopped"
  else
    log "fence_maat: Ma'at unreachable/failed (treating as already down)"
  fi
}

promote_wadjet(){ # returns 0 if Wadjet promoted to primary
  local out
  out=$(timeout 70 ssh "${SSH_OPTS[@]}" "$WADJET_SSH" \
        "sudo -n runuser -u postgres -- psql -tAc 'SELECT pg_promote(true,60)'" 2>>"$LOG")
  log "promote_wadjet: pg_promote returned '$(echo "$out" | tr -d '[:space:]')'"
  echo "$out" | grep -q t || return 1
  # Belt-and-suspenders: a standby built via pg_basebackup can inherit the SOURCE's
  # listen_addresses in auto.conf (e.g. Ma'at's IP) -> the promoted primary would not listen
  # on its own LAN IP. Pin it to Wadjet's own IP. NON-DISRUPTIVE: ALTER SYSTEM + reload only
  # (listen_addresses rebinds only on restart; we never restart a just-promoted primary
  # mid-outage -- the live sockets are kept and the corrected value persists for next start).
  if timeout 30 ssh "${SSH_OPTS[@]}" "$WADJET_SSH" \
       "sudo -n runuser -u postgres -- psql -tAc \"ALTER SYSTEM SET listen_addresses='localhost,$WADJET_LAN'\" -tAc 'SELECT pg_reload_conf()'" >>"$LOG" 2>&1; then
    log "promote_wadjet: listen_addresses pinned to localhost,$WADJET_LAN (persisted; rebinds on next restart)"
  else
    log "promote_wadjet: WARN could not pin listen_addresses"
  fi
  return 0
}

flip_origin_to_wadjet(){
  cp "$CF_CONFIG" "${CF_CONFIG}.bak.failover.$(date +%Y%m%d_%H%M%S)"
  sed -i "s#$VAULT_ORIGIN_MAAT#$VAULT_ORIGIN_WADJET#g" "$CF_CONFIG"
  if grep -q "$VAULT_ORIGIN_WADJET" "$CF_CONFIG"; then log "flip_origin: vault origin now -> Wadjet"; else log "flip_origin: ERROR origin not updated"; fi
}
reload_cloudflared(){
  if systemctl kill -s HUP cloudflared.service 2>>"$LOG"; then
    log "reload_cloudflared: SIGHUP sent"
  else
    systemctl restart cloudflared.service && log "reload_cloudflared: restarted (HUP failed)" || log "reload_cloudflared: ERROR"
  fi
}
start_wadjet_vw(){
  if timeout 25 ssh "${SSH_OPTS[@]}" "$WADJET_SSH" "sudo -n docker start vaultwarden" >>"$LOG" 2>&1; then
    log "start_wadjet_vw: started"
  else
    log "start_wadjet_vw: ERROR"
  fi
}

do_failover(){
  exec 9>"$LOCK"
  flock -n 9 || { log "failover: lock held, another run in progress"; return 0; }
  log "=== INITIATING FAILOVER Ma'at -> Wadjet ==="
  notify "VAULT FAILOVER STARTING" "urgent" "Ma'at vault unreachable. Promoting Wadjet to primary."
  fence_maat
  if ! promote_wadjet; then
    log "FAILOVER ABORTED: pg_promote failed"
    notify "VAULT FAILOVER FAILED" "urgent" "pg_promote on Wadjet FAILED. Ma'at fenced. MANUAL intervention required."
    return 1
  fi
  flip_origin_to_wadjet
  reload_cloudflared
  start_wadjet_vw
  echo wadjet > "$PRIMARY_FILE"
  echo 0 > "$COUNT_FILE"
  log "=== FAILOVER COMPLETE: primary=wadjet ==="
  notify "VAULT FAILOVER COMPLETE" "urgent" "Wadjet is now vault primary. vault.lumennode.uk -> Wadjet (192.168.8.173). FAILBACK IS MANUAL: /home/lumen/bin/wadjet-ha-failback.sh"
}

CUR_PRIMARY=$(cat "$PRIMARY_FILE" 2>/dev/null || echo maat)

# ---- status mode ----
if [ "$MODE" = "--status" ]; then
  echo "primary       : $CUR_PRIMARY"
  echo "fail_count    : $(cat "$COUNT_FILE" 2>/dev/null || echo 0) / $FAIL_THRESHOLD"
  echo "auto_promote  : $( [ -e "$ENABLE_FLAG" ] && echo ENABLED || echo DISABLED )"
  if probe_maat_ok; then echo "probe Ma'at   : OK (pg_isready + VW /alive)"; else echo "probe Ma'at   : FAIL"; fi
  if probe_wadjet_ok; then echo "probe Wadjet  : OK (standby pg + VW stopped? note: /alive only if VW running)"; else echo "probe Wadjet  : pg or VW-alive not both up (expected: VW stopped on standby)"; fi
  echo "--- last 8 log lines ---"; tail -8 "$LOG" 2>/dev/null
  exit 0
fi

# ---- post-failover: primary is wadjet -> monitor only, NO auto-failback ----
if [ "$CUR_PRIMARY" = "wadjet" ]; then
  if probe_wadjet_ok; then exit 0; fi
  log "WARN: Wadjet (current primary) probe FAILED"
  notify "VAULT WARNING" "high" "Wadjet (active vault primary) is failing its probe. Manual attention needed."
  exit 0
fi

# ---- normal: primary is maat ----
if probe_maat_ok; then
  echo 0 > "$COUNT_FILE"
  exit 0
fi

N=$(( $(cat "$COUNT_FILE" 2>/dev/null || echo 0) + 1 ))
echo "$N" > "$COUNT_FILE"
log "Ma'at probe FAIL ($N/$FAIL_THRESHOLD)"

if [ "$N" -lt "$FAIL_THRESHOLD" ]; then exit 0; fi

if [ ! -e "$ENABLE_FLAG" ]; then
  log "Ma'at down ${N}x but AUTO_PROMOTE disabled — alert only"
  notify "VAULT DOWN (no auto-promote)" "urgent" "Ma'at vault unreachable ${N}x; auto-promote is DISABLED. Manual failover needed."
  exit 0
fi

if [ "$MODE" = "--dry-run" ]; then
  log "DRY-RUN: threshold reached; WOULD fence Ma'at, promote Wadjet, flip CF origin, start Wadjet VW"
  exit 0
fi

do_failover
