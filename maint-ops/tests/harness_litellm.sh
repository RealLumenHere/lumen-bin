#!/bin/bash
# Test-to-failure harness for litellm-upgrade. Throwaway compose project on 127.0.0.1:4002 + scratch DB. Prod untouched.
export WQ_AGENT=deps-drain-98190c
SCRIPT=${SCRIPT:-$(cd "$(dirname "$0")/.." && pwd)/litellm-upgrade}
DUMP=${DUMP:-$(ls -t /home/lumen/backups/litellm_db_pre_*.sql.gz | head -1)}
SCRATCH=litellm_canary2
H=/tmp/harness_wq2989
OLD=sha256:6c82d338a60e7b273ae46bf1d8db814d2856ae010f96c44eeadde574d3893f76
NEW=sha256:e29d25e1952337eb7c9b605213865ae935c60f1689ad465cb5fc940b598174c9
DCH() { docker compose -p litellm-harness -f $H/compose.yml "$@"; }
psqlc() { docker exec lumen-pgvector psql -U lumen -d "$1" -tA -c "$2" 2>&1; }
cleanup() {
  DCH down >/dev/null 2>&1; docker rm -f litellm-harness >/dev/null 2>&1
  docker exec lumen-pgvector psql -U lumen -d postgres -qc "DROP DATABASE IF EXISTS $SCRATCH" 2>&1 | head -1
  rm -rf $H; echo "cleanup done (harness project, scratch db, temp files)"
}
trap cleanup EXIT
PASS=0; FAIL=0
check() { if [ "$2" = "$3" ]; then echo "  PASS $1 (got $2)"; PASS=$((PASS+1)); else echo "  FAIL $1 (want $3 got $2)"; FAIL=$((FAIL+1)); fi; }
running() { docker inspect litellm-harness --format '{{.Config.Image}}' 2>/dev/null | grep -oE 'sha256:[0-9a-f]{64}'; }
compose_dig() { grep -m1 -oE 'ghcr.io/berriai/litellm@sha256:[0-9a-f]{64}' $H/compose.yml | cut -d@ -f2; }

mkdir -p $H/state; chmod 700 $H
echo "== prod before"; docker inspect litellm --format 'litellm started={{.State.StartedAt}} health={{.State.Health.Status}}'
echo "== scratch db restore"; docker exec lumen-pgvector psql -U lumen -d postgres -qc "DROP DATABASE IF EXISTS $SCRATCH" 2>&1 | head -1
docker exec lumen-pgvector psql -U lumen -d postgres -qc "CREATE DATABASE $SCRATCH" 2>&1 | head -1
nice -n 19 gunzip -c $DUMP | docker exec -i lumen-pgvector psql -U lumen -d $SCRATCH -q >/dev/null 2>&1
echo "scratch migrations=$(psqlc $SCRATCH 'select count(*) from _prisma_migrations')"
python3 - $H/db.env $SCRATCH <<'PY'
import sys, urllib.parse
dsn=None
for l in open("/home/lumen/agent/.env"):
    if l.startswith("DATABASE_URL="): dsn=l.split("=",1)[1].strip().strip('"').strip("'")
u=urllib.parse.urlsplit(dsn)
open(sys.argv[1],"w").write("DATABASE_URL="+urllib.parse.urlunsplit((u.scheme,u.netloc,"/"+sys.argv[2],u.query,u.fragment))+"\n")
PY
chmod 600 $H/db.env
cat > $H/compose.yml <<EOF
services:
  litellm:
    image: ghcr.io/berriai/litellm@$OLD
    container_name: litellm-harness
    ports: ["127.0.0.1:4002:4000"]
    volumes:
      - /home/lumen/agent/litellm_config.yaml:/app/config.yaml:ro
      - /home/lumen/agent/master_key_guard.py:/app/master_key_guard.py:ro
    env_file: [/home/lumen/agent/.env, /home/lumen/.secrets/litellm-master.env, /home/lumen/.secrets/gemini.env, $H/db.env]
    extra_hosts: ["host.docker.internal:host-gateway"]
    networks: [docker_default]
    command: ["--config","/app/config.yaml","--port","4000","--num_workers","1"]
networks:
  docker_default:
    external: true
EOF
echo "== boot harness on OLD image"; DCH up -d 2>&1 | tail -1 | cut -c1-80
for i in $(seq 1 40); do curl -s -m3 http://127.0.0.1:4002/health/readiness 2>/dev/null | grep -q '"healthy"' && break; sleep 5; done
echo "harness old image up: running=$(running | cut -c1-19)"
python3 - $H/smoke.json <<'PY'
import json, os, sys, urllib.request
master=None
for l in open("/home/lumen/.secrets/litellm-master.env"):
    if l.startswith("LITELLM_MASTER_KEY="): master=l.split("=",1)[1].strip().strip('"').strip("'")
r=urllib.request.Request("http://127.0.0.1:4002/key/generate",data=json.dumps({"key_alias":"harness-smoke","models":["deepseek-v4-flash","qwen36-local"],"max_budget":0.1}).encode(),headers={"Authorization":"Bearer "+master,"Content-Type":"application/json"})
k=json.loads(urllib.request.urlopen(r,timeout=60).read())["key"]
fd=os.open(sys.argv[1],os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600); open(fd,"w").write(json.dumps({"key":k})); print("harness smoke key created")
PY
export COMPOSE_FILE=$H/compose.yml PROJECT=litellm-harness CONTAINER=litellm-harness PORT=4002 SMOKE_KEY_FILE=$H/smoke.json STATE_DIR=$H/state PGDUMP=0 HEALTH_WAIT=150 NEG_CONTROL=1
echo; echo "== A. precheck on healthy old instance (want rc 0)"
bash $SCRIPT --precheck 2>&1 | sed 's/^/    /'; check "A precheck rc" "${PIPESTATUS[0]}" 0
echo; echo "== B. apply with INJECTED verify failure (want rc 1, rolled back to OLD)"
FORCE_VERIFY_FAIL=1 bash $SCRIPT --apply 2>&1 | sed 's/^/    /'; RC=${PIPESTATUS[0]}
check "B rc" "$RC" 1; check "B running image is OLD" "$(running)" "$OLD"; check "B compose digest is OLD" "$(compose_dig)" "$OLD"
echo; echo "== C. real apply (want rc 0, running NEW)"
bash $SCRIPT --apply 2>&1 | sed 's/^/    /'; RC=${PIPESTATUS[0]}
check "C rc" "$RC" 0; check "C running image is NEW" "$(running)" "$NEW"; check "C compose digest is NEW" "$(compose_dig)" "$NEW"
echo; echo "== D. apply again (idempotent, want rc 0 no recreate)"
S1=$(docker inspect litellm-harness --format '{{.State.StartedAt}}')
bash $SCRIPT --apply 2>&1 | sed 's/^/    /'; RC=${PIPESTATUS[0]}
check "D rc" "$RC" 0; check "D container not recreated" "$(docker inspect litellm-harness --format '{{.State.StartedAt}}')" "$S1"
echo; echo "== E. manual --rollback (want rc 1 = rolled back cleanly, running OLD)"
bash $SCRIPT --rollback 2>&1 | sed 's/^/    /'; RC=${PIPESTATUS[0]}
check "E rc" "$RC" 1; check "E running image is OLD" "$(running)" "$OLD"
echo; echo "== F. no backup recorded -> rollback must refuse loudly (want rc 2)"
STATE_DIR=$H/state_empty bash $SCRIPT --rollback 2>&1 | sed 's/^/    /'; check "F rc" "${PIPESTATUS[0]}" 2
echo; echo "== prod after (must be unchanged)"; docker inspect litellm --format 'litellm started={{.State.StartedAt}} health={{.State.Health.Status}}'
grep -c "sha256:6c82d338" /home/lumen/docker/docker-compose.yml | sed 's/^/prod compose still pins old digest lines=/'
echo; echo "SUMMARY pass=$PASS fail=$FAIL"
echo "=== DONE ==="
