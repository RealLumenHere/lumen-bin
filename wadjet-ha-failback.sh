#!/bin/bash
# wadjet-ha-failback.sh — MANUAL, supervised failback after an auto-promote to Wadjet.
# Never run automatically. Each destructive action requires --confirm.
# Run on Lumen. Reuses proven streaming mechanics from the original bring-up.
#
# Usage:
#   wadjet-ha-failback.sh status
#   wadjet-ha-failback.sh rebuild-maat --confirm     # make repaired Ma'at a STANDBY of Wadjet (non-disruptive; Wadjet keeps serving)
#   wadjet-ha-failback.sh switchover  --confirm      # cut public vault back to Ma'at, demote Wadjet to standby (maintenance window)
#
# Order: repair Ma'at hardware/OS -> rebuild-maat -> wait until caught up (status) -> switchover.
set -uo pipefail
HA_DIR=/home/lumen/ha
source "$HA_DIR/ha.env"
REPL_PW=$(cat /home/lumen/.wadjet_replicator_pw)
FLEET_PW=$(cat "$HA_DIR/.fleet_pw")
SSH_OPTS=(-o IdentitiesOnly=yes -o ConnectTimeout=6 -o StrictHostKeyChecking=accept-new -i "$SSH_KEY")
CMD="${1:-status}"; CONFIRM="${2:-}"
DETACH=0; for a in "$@"; do [ "$a" = "--detach" ] && DETACH=1; [ "$a" = "--confirm" ] && CONFIRM="--confirm"; done

maat_root(){ ssh "${SSH_OPTS[@]}" "$MAAT_SSH" "echo '$FLEET_PW' | sudo -S -p '' bash -c \"$1\""; }
wadjet_root(){ ssh "${SSH_OPTS[@]}" "$WADJET_SSH" "sudo -n bash -c \"$1\""; }
maat_psql(){ ssh "${SSH_OPTS[@]}" "$MAAT_SSH" "echo '$FLEET_PW' | sudo -S -p '' runuser -u postgres -- psql -tAc \"$1\""; }
wadjet_psql(){ ssh "${SSH_OPTS[@]}" "$WADJET_SSH" "sudo -n runuser -u postgres -- psql -tAc \"$1\""; }
need_confirm(){ [ "$CONFIRM" = "--confirm" ] || { echo "Refusing: re-run with --confirm to perform '$CMD'."; exit 1; }; }

# ===== HA hardening helpers (added 2026-06-26) =====
ENABLE_FLAG="$HA_DIR/AUTO_PROMOTE_ENABLED"
DISARMED_FLAG="${ENABLE_FLAG}.failback_disarmed"
_DISARMED=0
# Self-disarm the auto-promote monitor so it cannot fence Ma'at / promote Wadjet mid-procedure.
disarm_monitor(){
  if [ -e "$ENABLE_FLAG" ]; then
    mv "$ENABLE_FLAG" "$DISARMED_FLAG" && _DISARMED=1
    echo "    monitor DISARMED for this op (auto-promote paused)"
  else
    echo "    monitor already disarmed (no AUTO_PROMOTE_ENABLED present)"
  fi
}
# Re-arm only if THIS run disarmed it. Wired to an EXIT trap so it always restores.
rearm_monitor(){
  if [ "$_DISARMED" = 1 ] && [ -e "$DISARMED_FLAG" ]; then
    mv "$DISARMED_FLAG" "$ENABLE_FLAG" && _DISARMED=0
    echo "    monitor RE-ARMED (auto-promote restored)"
  fi
}

# Pre-flight: pg_basebackup -X stream reads SOURCE PGDATA via the postgres backend; a file the
# backend cannot read aborts the copy -- but only AFTER the target data dir has been archived/
# emptied -> data-loss window. Abort BEFORE touching the target if the SOURCE PGDATA holds any
# non-postgres-owned file (e.g. a stray root-owned *.bak -- the documented 2026-06-26 root cause).
prescan_source(){ # $1 = maat|wadjet (the SOURCE/primary basebackup'd FROM)
  local who="$1" rootfn out
  if [ "$who" = wadjet ]; then rootfn=wadjet_root; else rootfn=maat_root; fi
  # Run as ROOT for complete traversal (postgres can't descend root-owned mode-700 dirs).
  # Flag anything NOT owned by postgres -- the documented 2026-06-26 root cause was a stray
  # root-owned file that aborted pg_basebackup AFTER the target PGDATA was already wiped.
  out=$($rootfn "find /var/lib/postgresql/18/main -xdev ! -user postgres -printf '%u:%g %m %p\n' 2>/dev/null")
  if [ -n "$out" ]; then
    echo "ABORT (pre-scan): $who PGDATA contains non-postgres-owned files; pg_basebackup could"
    echo "       fail mid-copy AFTER the target was wiped. TARGET NOT TOUCHED. Offending files:"
    echo "$out" | sed 's/^/         /'
    exit 1
  fi
  echo "    pre-scan OK: all $who PGDATA files owned by postgres"
}

# After a basebackup the target's postgresql.auto.conf carries the SOURCE's listen_addresses
# (base postgresql.conf pins none on these nodes) -> the target PG would bind the wrong IP /
# loopback-only on start. Pin auto.conf to the target's OWN IP while PG is stopped, pre-start.
# Ships a tiny script and runs it as root (avoids nested-quote + sudo-stdin hazards).
fix_listen_addresses(){ # $1 = maat|wadjet (the TARGET just basebackup'd)
  local who="$1" sshtarget ip
  if [ "$who" = wadjet ]; then sshtarget="$WADJET_SSH"; ip="$WADJET_LAN"; else sshtarget="$MAAT_SSH"; ip="$MAAT_LAN"; fi
  ssh "${SSH_OPTS[@]}" "$sshtarget" "cat > /tmp/_fix_la.sh" <<'RFLA'
#!/bin/bash
AC=/var/lib/postgresql/18/main/postgresql.auto.conf
IP="$1"
touch "$AC"
grep -vE '^[[:space:]]*listen_addresses' "$AC" > "$AC.tmp" 2>/dev/null && mv "$AC.tmp" "$AC"
printf "listen_addresses = 'localhost,%s'\n" "$IP" >> "$AC"
chown postgres:postgres "$AC"
RFLA
  if [ "$who" = wadjet ]; then
    ssh "${SSH_OPTS[@]}" "$sshtarget" "sudo -n bash /tmp/_fix_la.sh '$ip'; rm -f /tmp/_fix_la.sh"
  else
    ssh "${SSH_OPTS[@]}" "$sshtarget" "echo '$FLEET_PW' | sudo -S -p '' bash /tmp/_fix_la.sh '$ip'; rm -f /tmp/_fix_la.sh"
  fi
  echo "    listen_addresses pinned to localhost,$ip in $who auto.conf (pre-start)"
}

# Re-exec a destructive command detached as a transient systemd unit so a dropped Commander/SSH
# session (e.g. cloudflared reload killing the foreground process) cannot SIGKILL it mid-run.
# Opt-in via a trailing --detach arg. Internal guard _DETACHED=1 prevents re-loop.
maybe_detach(){
  [ "${_DETACHED:-0}" = 1 ] && return 0
  [ "$DETACH" = 1 ] || return 0
  local unit="vault-failback-${CMD}-$(date +%Y%m%d_%H%M%S)"
  echo "Detaching as systemd unit: $unit  (survives SSH/cloudflared session drops)"
  echo "  follow:  journalctl -u $unit -f      status: systemctl status $unit"
  echo "$FLEET_PW" | sudo -S -p '' systemd-run --unit="$unit" --collect --property=Type=oneshot \
       --setenv=_DETACHED=1 /home/lumen/bin/wadjet-ha-failback.sh "$CMD" --confirm
  exit $?
}

show_status(){
  # The WADJET_* vars point at the CURRENT promote target (sobek since 2026-08-10,
  # aq#429); the names are kept for script compat. Label the output with the REAL
  # host so nobody reads a healthy Sobek as a healthy Wadjet — the actual Wadjet
  # box has been powered off since 2026-07-16 and off the tailnet for 25+ days.
  local SB="${WADJET_SSH##*@}"
  local SBN="${WADJET_SSH%%@*}"
  echo "primary lock      : $(cat "$HA_DIR/primary" 2>/dev/null)"
  echo "standby target    : $SBN@$SB  (WADJET_* vars = compat alias, NOT the wadjet host)"
  echo "Ma'at  in_recovery: $(maat_psql 'SELECT pg_is_in_recovery()' 2>/dev/null)"
  echo "$SBN in_recovery: $(wadjet_psql 'SELECT pg_is_in_recovery()' 2>/dev/null)"
  echo "Ma'at  wal_recv   : $(maat_psql "SELECT status||' from '||sender_host FROM pg_stat_wal_receiver" 2>/dev/null)"
  echo "$SBN wal_recv   : $(wadjet_psql "SELECT status||' from '||sender_host FROM pg_stat_wal_receiver" 2>/dev/null)"
  echo "Ma'at  repl_out   : $(maat_psql "SELECT application_name||' '||state FROM pg_stat_replication" 2>/dev/null)"
  echo "$SBN repl_out   : $(wadjet_psql "SELECT application_name||' '||state FROM pg_stat_replication" 2>/dev/null)"
  echo "CF vault origin   : $(grep -A1 'vault.lumennode.uk' "$CF_CONFIG" | grep service | tr -d ' ')"
}

# Make Ma'at a fresh streaming standby of Wadjet (Wadjet must currently be primary).
rebuild_maat(){
  need_confirm
  maybe_detach
  trap rearm_monitor EXIT
  [ "$(wadjet_psql 'SELECT pg_is_in_recovery()')" = "f" ] || { echo "ABORT: Wadjet is not a read-write primary."; exit 1; }
  disarm_monitor
  echo "[pre] pre-scan SOURCE (Wadjet) PGDATA before touching Ma'at"
  prescan_source wadjet
  echo "[1/6] Ensure Wadjet accepts replication from Ma'at (.202) + listens on LAN"
  wadjet_root "grep -qE 'replicator[[:space:]]+192\.168\.8\.202/32' /etc/postgresql/18/main/pg_hba.conf || echo 'host    replication     replicator      192.168.8.202/32        scram-sha-256' >> /etc/postgresql/18/main/pg_hba.conf; systemctl reload postgresql@18-main"
  CURLISTEN=$(wadjet_psql 'SHOW listen_addresses')
  if ! echo "$CURLISTEN" | grep -q '192.168.8.173'; then
     echo "    Wadjet listen_addresses=$CURLISTEN -> adding LAN (requires Wadjet PG restart)"
     wadjet_root "runuser -u postgres -- psql -c \\\"ALTER SYSTEM SET listen_addresses = 'localhost,192.168.8.173'\\\"; systemctl restart postgresql@18-main"
     sleep 3
  fi
  echo "[2/6] Ensure replication slot for Ma'at on Wadjet"
  wadjet_psql "SELECT pg_create_physical_replication_slot('maat_slot') WHERE NOT EXISTS (SELECT 1 FROM pg_replication_slots WHERE slot_name='maat_slot')" >/dev/null 2>&1 || true
  echo "[3/6] Stop Ma'at PG + VW, archive old data dir"
  maat_root "docker stop vaultwarden 2>/dev/null; systemctl stop postgresql@18-main; TS=\$(date +%Y%m%d_%H%M%S); mv /var/lib/postgresql/18/main /var/lib/postgresql/18/main.old.\$TS; install -d -o postgres -g postgres -m 700 /var/lib/postgresql/18/main"
  echo "[4/6] Seed Ma'at .pgpass for Wadjet primary"
  maat_root "grep -q '192.168.8.173:5432:replication:replicator:' /var/lib/postgresql/.pgpass 2>/dev/null || printf '192.168.8.173:5432:replication:replicator:%s\n192.168.8.173:5432:*:replicator:%s\n' '$REPL_PW' '$REPL_PW' >> /var/lib/postgresql/.pgpass; chown postgres:postgres /var/lib/postgresql/.pgpass; chmod 600 /var/lib/postgresql/.pgpass"
  echo "[5/6] pg_basebackup Ma'at <- Wadjet (slot maat_slot)"
  maat_root "runuser -u postgres -- env PGPASSWORD='$REPL_PW' pg_basebackup -h 192.168.8.173 -p 5432 -U replicator -D /var/lib/postgresql/18/main -R -S maat_slot -X stream -P"
  echo "[5b/6] Pin Ma'at listen_addresses to its own IP (strip inherited Wadjet auto.conf value) BEFORE start"
  fix_listen_addresses maat
  echo "[6/6] Start Ma'at PG (as standby of Wadjet) + verify"
  maat_root "systemctl start postgresql@18-main"; sleep 5
  echo "Ma'at in_recovery=$(maat_psql 'SELECT pg_is_in_recovery()')  wal_recv=$(maat_psql "SELECT status||' from '||sender_host FROM pg_stat_wal_receiver")"
  echo "DONE. Ma'at is now a standby of Wadjet. Wait until caught up (status), then: switchover --confirm"
}

# Deliberate cutover: public vault Wadjet -> Ma'at, demote Wadjet to standby of Ma'at.
switchover(){
  need_confirm
  maybe_detach
  trap rearm_monitor EXIT
  echo "[pre] verifying Ma'at is a caught-up standby of Wadjet"
  [ "$(maat_psql 'SELECT pg_is_in_recovery()')" = "t" ] || { echo "ABORT: Ma'at is not a standby. Run rebuild-maat first."; exit 1; }
  LAG=$(wadjet_psql "SELECT COALESCE(pg_wal_lsn_diff(pg_current_wal_lsn(), replay_lsn),9e18)::bigint FROM pg_stat_replication WHERE client_addr='192.168.8.202' LIMIT 1")
  echo "    replay lag bytes: ${LAG:-unknown}"
  if [ -z "$LAG" ] || [ "$LAG" -gt 16000000 ] 2>/dev/null; then echo "ABORT: Ma'at not caught up (lag too high / unknown)."; exit 1; fi
  disarm_monitor
  echo "[1/6] Stop Wadjet Vaultwarden (halt writes)"; wadjet_root "docker stop vaultwarden"
  sleep 2
  echo "[2/6] Promote Ma'at"; [ "$(maat_psql 'SELECT pg_promote(true,60)')" = "t" ] || { echo "ABORT: Ma'at promote failed (Wadjet VW still stopped)."; exit 1; }
  echo "[3/6] Flip CF vault origin -> Ma'at + reload"
  cp "$CF_CONFIG" "${CF_CONFIG}.bak.failback.$(date +%Y%m%d_%H%M%S)"
  sed -i "s#$VAULT_ORIGIN_WADJET#$VAULT_ORIGIN_MAAT#g" "$CF_CONFIG"
  # wq#3575: cloudflared exits on SIGHUP; the actuator serves reload as a detached restart.
  /home/lumen/bin/cfd-actuator request --from wadjet-failback --kind reload \
    --reason "vault failback: CF origin flipped" || echo "    WARN: cfd-actuator request failed — restart cloudflared by hand"
  echo "[4/6] Start Ma'at Vaultwarden"; maat_root "docker start vaultwarden"; sleep 3
  echo "[5/6] Rebuild Wadjet as standby of Ma'at"
  echo "    pre-scan SOURCE (Ma'at) PGDATA before wiping Wadjet"
  prescan_source maat
  maat_psql "SELECT pg_create_physical_replication_slot('wadjet_slot') WHERE NOT EXISTS (SELECT 1 FROM pg_replication_slots WHERE slot_name='wadjet_slot')" >/dev/null 2>&1 || true
  wadjet_root "docker stop vaultwarden 2>/dev/null; systemctl stop postgresql@18-main; TS=\$(date +%Y%m%d_%H%M%S); mv /var/lib/postgresql/18/main /var/lib/postgresql/18/main.old.\$TS; install -d -o postgres -g postgres -m 700 /var/lib/postgresql/18/main"
  wadjet_root "runuser -u postgres -- env PGPASSWORD='$REPL_PW' pg_basebackup -h 192.168.8.202 -p 5432 -U replicator -D /var/lib/postgresql/18/main -R -S wadjet_slot -X stream -P"
  fix_listen_addresses wadjet
  wadjet_root "systemctl start postgresql@18-main"
  sleep 5
  echo "[6/6] Record primary=maat, reset counters"
  echo maat > "$HA_DIR/primary"; echo 0 > "$HA_DIR/maat_fail_count"
  echo "Wadjet in_recovery=$(wadjet_psql 'SELECT pg_is_in_recovery()') wal_recv=$(wadjet_psql "SELECT status FROM pg_stat_wal_receiver")"
  echo "SWITCHOVER COMPLETE: public vault -> Ma'at; Wadjet streaming standby restored."
}

case "$CMD" in
  status) show_status ;;
  rebuild-maat) rebuild_maat ;;
  switchover) switchover ;;
  *) echo "usage: $0 {status|rebuild-maat --confirm|switchover --confirm}"; exit 1 ;;
esac
