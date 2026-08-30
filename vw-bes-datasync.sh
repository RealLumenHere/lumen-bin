#!/bin/bash
# wq#324: hourly Vaultwarden data-dir sync Maat(primary) -> Bes(promote payload)
# Guard: only syncs while Maat is PG primary (in_recovery=f). Skips loudly otherwise.
set -u
MAAT=maam@100.105.219.34
BES=bes@100.110.156.91
S="ssh -o ConnectTimeout=10 -o BatchMode=yes"

REC=$($S $MAAT 'sudo -n -u postgres psql -tAc "select pg_is_in_recovery();"' 2>/dev/null | tr -d '[:space:]')
if [ "$REC" != "f" ]; then
  curl -s -H "Title: vw-bes-datasync SKIP" \
    -d "Maat not primary (in_recovery='$REC') — VW data sync to Bes skipped; verify vault topology" \
    http://localhost:8080/lumen-updates >/dev/null 2>&1
  echo "SKIP: maat in_recovery='$REC'"
  exit 0
fi

$S $MAAT 'sudo -n tar -C /opt/maat-vaultwarden/data -cf - .' | \
  $S $BES 'sudo -n tar -C /opt/bes-vaultwarden/data -xpf -'
RC=$?
if [ $RC -ne 0 ]; then
  curl -s -H "Title: vw-bes-datasync FAIL" -d "tar-stream sync Maat->Bes failed rc=$RC" \
    http://localhost:8080/lumen-updates >/dev/null 2>&1
  echo "FAIL rc=$RC"
  exit 1
fi
echo "OK $(date '+%F %T %Z')"
