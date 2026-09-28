#!/bin/bash
# vault-sobek-rebuild.sh — rebuild the vault promote-target standby (ha.env WADJET_* == Sobek) as a
# streaming standby of Ma'at. Created for wq#2918 (ISSUE-LOG #847 split-brain repair).
#
# Why this exists: wadjet-ha-failback.sh is Wadjet-shaped; its `switchover` pg_promote()s Ma'at, which
# is already a primary here, and would abort. This tool reuses only the proven mechanics from it
# (source PGDATA prescan, pg_basebackup -R -S <slot> -X stream, pin listen_addresses to the target's
# OWN ip pre-start).
#
# Safety: Ma'at is only read from (plus an idempotent slot create). The target's old PGDATA is MOVED
# aside (never deleted) and restored automatically if basebackup or startup fails. Refuses to run if
# the two DBs' logical content differs, if Ma'at has no streaming standby (would go single-node), or
# if the target VW container is running.
#
# Usage (run on Lumen):
#   vault-sobek-rebuild.sh preflight             # read-only checks + fresh dumps
#   vault-sobek-rebuild.sh rebuild --confirm     # destructive on TARGET only; run detached:
#     sudo systemd-run --unit=vault-sobek-rebuild --collect --property=Type=oneshot \
#          /home/lumen/bin/vault-sobek-rebuild.sh rebuild --confirm
set -uo pipefail
HA_DIR=/home/lumen/ha
source "$HA_DIR/ha.env"
SSH=(ssh -T -o BatchMode=yes -o IdentitiesOnly=yes -o ConnectTimeout=8 -i "$SSH_KEY")
SLOT="${HA_STANDBY_SLOT:?HA_STANDBY_SLOT unset in ha.env}"
TGT_SSH="$WADJET_SSH"; TGT_LAN="$WADJET_LAN"
PGDATA=/var/lib/postgresql/18/main
CMD="${1:-preflight}"; CONFIRM="${2:-}"
BK="/home/lumen/backups/vault-rebuild-$(date +%Y%m%d_%H%M%S)"

log(){ echo "$(date -u +%H:%M:%S) $*"; }
die(){ log "ABORT: $*"; exit 1; }
tgt(){ "${SSH[@]}" "$TGT_SSH" 'sudo -n bash -s' <<<"$1"; }
src(){ "${SSH[@]}" "$MAAT_SSH" 'sudo -n bash -s' <<<"$1"; }
psql_src(){ src "runuser -u postgres -- psql -X -At -d vaultwarden -c \"$1\"" 2>/dev/null; }
psql_tgt(){ tgt "runuser -u postgres -- psql -X -At -d vaultwarden -c \"$1\"" 2>/dev/null; }
norm(){ grep -Ev '^(--|\\(un)?restrict)' "$1" | sha256sum | cut -d' ' -f1; }   # pg_dump 18 emits a random \restrict nonce

preflight(){
  log "[pf1] source (Ma'at) is a serving primary with a streaming standby"
  [ "$(psql_src 'select pg_is_in_recovery()')" = f ] || die "Ma'at is not a primary"
  curl -sf -m 6 -o /dev/null "$VAULT_ORIGIN_MAAT/alive" || die "Ma'at Vaultwarden /alive failing"
  NS=$(psql_src "select count(*) from pg_stat_replication where state='streaming'")
  [ "${NS:-0}" -ge 1 ] 2>/dev/null || die "Ma'at has no streaming standby; rebuilding would leave it single-node"
  log "    OK: primary, VW alive, $NS streaming standby(s)"

  log "[pf2] target is currently a rw primary with VW stopped"
  [ "$(psql_tgt 'select pg_is_in_recovery()')" = f ] || die "target is not a rw primary (already a standby? nothing to do)"
  [ -z "$(tgt 'docker ps --filter name=vaultwarden --filter status=running -q' 2>/dev/null)" ] || die "target Vaultwarden container is RUNNING"
  log "    OK"

  log "[pf3] logical content parity (fresh dumps -> $BK)"
  mkdir -p "$BK"; chmod 700 "$BK"
  src "runuser -u postgres -- pg_dump -Fp --no-owner vaultwarden" > "$BK/maat.sql" 2>"$BK/maat.err" || die "Ma'at dump failed (see $BK/maat.err)"
  tgt "runuser -u postgres -- pg_dump -Fp --no-owner vaultwarden" > "$BK/target.sql" 2>"$BK/target.err" || die "target dump failed (see $BK/target.err)"
  chmod 600 "$BK"/*.sql
  A=$(norm "$BK/maat.sql"); B=$(norm "$BK/target.sql")
  [ -s "$BK/maat.sql" ] && [ "$A" = "$B" ] || die "CONTENT DIVERGED (maat=$A target=$B). Target NOT touched. Dumps in $BK — reconcile by hand"
  log "    OK: identical ($A, $(stat -c%s "$BK/maat.sql") bytes)"

  log "[pf4] source PGDATA has only postgres-owned files (a stray root file aborts basebackup AFTER the target is wiped)"
  OUT=$(src "find $PGDATA -xdev ! -user postgres -printf '%u:%g %m %p\n' 2>/dev/null")
  [ -z "$OUT" ] || die "non-postgres-owned files in Ma'at PGDATA: $OUT"
  log "    OK"

  log "[pf5] target -> source replication auth (passfile), slot state, disk"
  tgt "runuser -u postgres -- psql -w \"host=$MAAT_LAN port=5432 user=replicator dbname=postgres replication=true\" -Atc IDENTIFY_SYSTEM" >/dev/null 2>&1 \
    || die "target cannot open a replication connection to Ma'at"
  SA=$(psql_src "select active from pg_replication_slots where slot_name='$SLOT'")
  [ "$SA" != t ] || die "slot $SLOT is already ACTIVE on Ma'at"
  AV=$(tgt "df -Pk /var/lib/postgresql | tail -1 | awk '{print \$4}'"); NEED=$(tgt "du -sk $PGDATA | cut -f1")
  [ "${AV:-0}" -gt $(( ${NEED:-0} * 3 + 1048576 )) ] || die "insufficient disk on target (avail=${AV}k need~${NEED}k*3)"
  log "    OK: slot=$SLOT ($( [ -z "$SA" ] && echo will-create || echo exists-inactive )), disk ok"
  log "PREFLIGHT PASS"
}

rollback(){ # $1 = TS ; restore the moved-aside primary PGDATA on the target
  local ts="$1"
  log "ROLLBACK: restoring target's previous PGDATA"
  tgt "systemctl stop postgresql@18-main; [ -d $PGDATA.old.$ts ] && { [ -d $PGDATA ] && mv $PGDATA $PGDATA.failed.$ts; mv $PGDATA.old.$ts $PGDATA; }; systemctl start postgresql@18-main; sleep 3; systemctl is-active postgresql@18-main"
}

rebuild(){
  [ "$CONFIRM" = "--confirm" ] || die "re-run with --confirm"
  preflight
  TS=$(date +%Y%m%d_%H%M%S)
  log "[1] ensure replication slot $SLOT on Ma'at (idempotent)"
  psql_src "select pg_create_physical_replication_slot('$SLOT') where not exists (select 1 from pg_replication_slots where slot_name='$SLOT')" >/dev/null
  log "[2] stop target PG; move PGDATA aside -> $PGDATA.old.$TS"
  tgt "systemctl stop postgresql@18-main; sleep 2; systemctl is-active postgresql@18-main && exit 1; mv $PGDATA $PGDATA.old.$TS && install -d -o postgres -g postgres -m 700 $PGDATA && echo MOVED" | grep -q MOVED \
    || { rollback "$TS"; die "could not move PGDATA aside"; }
  log "[3] pg_basebackup from Ma'at ($MAAT_LAN) into target, slot $SLOT"
  if ! tgt "runuser -u postgres -- pg_basebackup -w -h $MAAT_LAN -p 5432 -U replicator -D $PGDATA -R -S $SLOT -X stream -P 2>&1 | tail -3"; then
    rollback "$TS"; die "pg_basebackup failed; target restored to its previous state"
  fi
  tgt "test -f $PGDATA/standby.signal && test -f $PGDATA/PG_VERSION && echo BB_OK" | grep -q BB_OK || { rollback "$TS"; die "basebackup output incomplete (no standby.signal/PG_VERSION)"; }
  log "[4] pin listen_addresses to the target's OWN ip ($TGT_LAN) before start (basebackup inherits Ma'at's)"
  tgt "AC=$PGDATA/postgresql.auto.conf; touch \$AC; grep -vE '^[[:space:]]*listen_addresses' \$AC > \$AC.tmp; mv \$AC.tmp \$AC; printf \"listen_addresses = 'localhost,%s'\\n\" $TGT_LAN >> \$AC; chown postgres:postgres \$AC; grep -c '^listen_addresses' \$AC" | grep -qx 1 \
    || { rollback "$TS"; die "listen_addresses pin failed"; }
  log "[5] start target PG; expect in_recovery=t"
  tgt "systemctl start postgresql@18-main" >/dev/null 2>&1
  IR=""; for i in $(seq 1 30); do sleep 2; IR=$(psql_tgt 'select pg_is_in_recovery()'); [ "$IR" = t ] && break; done
  [ "$IR" = t ] || { rollback "$TS"; die "target did not come up as a standby (in_recovery='$IR')"; }
  log "[6] verify streaming + parity"
  sleep 6
  WR=$(psql_tgt "select status from pg_stat_wal_receiver")
  log "    target wal_receiver=$WR"
  psql_src "select '    maat sees: '||application_name||' '||client_addr||' '||state||' replay_lag_bytes='||coalesce(pg_wal_lsn_diff(sent_lsn,replay_lsn),-1) from pg_stat_replication order by client_addr"
  log "    slot: $(psql_src "select slot_name||' active='||active from pg_replication_slots where slot_name='$SLOT'")"
  M=$(psql_src "select count(*)||'/'||coalesce(max(updated_at)::text,'') from ciphers"); T=$(psql_tgt "select count(*)||'/'||coalesce(max(updated_at)::text,'') from ciphers")
  log "    ciphers maat=$M target=$T"
  [ "$WR" = streaming ] && [ "$M" = "$T" ] || { log "WARN: not fully verified (wal_receiver='$WR'); old primary PGDATA kept at $PGDATA.old.$TS — NOT rolling back a running standby automatically"; exit 2; }
  log "[7] HA state files -> primary=maat"
  echo maat > "$HA_DIR/primary"; echo 0 > "$HA_DIR/maat_fail_count"
  log "target VW container: $(tgt 'docker ps -a --filter name=vaultwarden --format {{.Status}}' 2>/dev/null) (must stay stopped; standby DB is read-only)"
  log "REBUILD OK. Old (timeline-8) primary PGDATA kept on target: $PGDATA.old.$TS  (delete after a few clean days)"
}

case "$CMD" in
  preflight) preflight ;;
  rebuild)   rebuild ;;
  *) echo "usage: $0 {preflight|rebuild --confirm}"; exit 1 ;;
esac
