#!/bin/bash
# Test-to-failure of ha-upgrade against a THROWAWAY Home Assistant on Bes (own compose project, own container ha-canary,
# minimal config, port 18123). Prod HA (container homeassistant, :8123, /home/bes/stack) is asserted untouched.
# Cases: no-state rollback, precheck, broken-config gate (must abort with nothing changed), forced verify failure (must
# roll back incl. config restore), real upgrade, idempotent re-apply, manual rollback.
S=${S:-$(cd "$(dirname "$0")/.." && pwd)/ha-upgrade}
B=bes@192.168.8.177
CN=/home/bes/ha-canary
export HOST_USER=bes HOST_LAN=192.168.8.177 STACK=$CN SERVICE=hacanary CNAME=ha-canary HA_PORT=18123 HTTP_PATH=${HTTP_PATH:-/manifest.json}
export E_PLUG_CHECK=0 STATE_NAME=ha-canary RB_REPO=ha-rollback-canary VERIFY_TIMEOUT=300
export STATE_DIR=/tmp/ha_test_state_wq2989; rm -rf $STATE_DIR
[ "$STACK" = "/home/bes/stack" ] && { echo "REFUSED: harness must never target prod stack"; exit 9; }
PASS=0; FAIL=0
check() { if [ "$2" = "$3" ]; then echo "  PASS $1 (got $2)"; PASS=$((PASS+1)); else echo "  FAIL $1 (want $3 got $2)"; FAIL=$((FAIL+1)); fi; }
RB() { ssh -n -o BatchMode=yes -o ConnectTimeout=8 $B "$@"; }
cver()    { RB "docker exec ha-canary python3 -c 'import homeassistant.const as c;print(c.__version__)'" 2>/dev/null | tr -d '\r'; }
cstart()  { RB "docker inspect -f '{{.State.StartedAt}}' ha-canary" 2>/dev/null; }
pstart()  { RB "docker inspect -f '{{.State.StartedAt}} {{.Image}}' homeassistant" 2>/dev/null; }
cimg()    { RB "grep -c 'image: ghcr.io/home-assistant/home-assistant:stable\$' $CN/compose.yaml" 2>/dev/null; }
setup() {
  RB "sudo rm -rf $CN; mkdir -p $CN/homeassistant" || return 1
  RB "cat > $CN/compose.yaml <<'EOF'
services:
  hacanary:
    image: ghcr.io/home-assistant/home-assistant:stable
    container_name: ha-canary
    restart: unless-stopped
    network_mode: host
    environment: [\"TZ=America/Los_Angeles\"]
    volumes:
      - $CN/homeassistant:/config
EOF
cat > $CN/homeassistant/configuration.yaml <<'EOF'
homeassistant:
  name: Canary
http:
  server_port: 18123
frontend:
recorder:
EOF
echo pre > $CN/homeassistant/sentinel.txt
cd $CN && docker compose up -d --no-deps --pull never hacanary" >/dev/null 2>&1
}
teardown() {
  RB "cd $CN 2>/dev/null && docker compose down 2>&1 | tail -1; sudo rm -rf $CN; for t in \$(docker images ha-rollback-canary --format '{{.Repository}}:{{.Tag}}'); do docker rmi \$t >/dev/null; done; docker ps -a --format '{{.Names}}' | grep -c ha-canary" 2>&1
}
P0=$(pstart)
echo "== start $(date -u +%H:%M:%SZ) prod=$P0"
echo "== setup canary on old image (prod :stable)"; setup; sleep 5
for i in $(seq 1 40); do [ "$(RB "curl -s -o /dev/null -m 4 -w '%{http_code}' http://127.0.0.1:18123$HTTP_PATH")" = "200" ] && break; sleep 5; done
check "canary up on old version" "$(cver)" "2026.7.2"
echo "== 1. rollback with no state must refuse loudly (want rc 2)"; bash $S --rollback 2>&1 | sed 's/^/    /'; check "no-state rollback rc" "${PIPESTATUS[0]}" 2
echo "== 2. precheck (want rc 0)"; bash $S --precheck 2>&1 | sed 's/^/    /'; check "precheck rc" "${PIPESTATUS[0]}" 0
echo "== 3. BROKEN config must be caught by check_config under the new image (want rc 3, canary untouched)"
RB "printf 'http:\n  server_port: [\n' > $CN/homeassistant/configuration.yaml.new; sudo cp $CN/homeassistant/configuration.yaml $CN/homeassistant/configuration.yaml.good; sudo cp $CN/homeassistant/configuration.yaml.new $CN/homeassistant/configuration.yaml"
T0=$(cstart)
bash $S --apply 2>&1 | sed 's/^/    /'; RC=${PIPESTATUS[0]}
check "broken-config rc" "$RC" 3; check "canary not restarted" "$(cstart)" "$T0"; check "compose untouched" "$(cimg)" "1"; check "still old version" "$(cver)" "2026.7.2"
RB "sudo cp $CN/homeassistant/configuration.yaml.good $CN/homeassistant/configuration.yaml"
echo "== 4. apply with INJECTED verify failure (want rc 1, old version, config restored, compose reverted)"
FORCE_VERIFY_FAIL=1 bash $S --apply 2>&1 | sed 's/^/    /'; RC=${PIPESTATUS[0]}
check "inject rc" "$RC" 1; check "version restored" "$(cver)" "2026.7.2"; check "compose reverted" "$(cimg)" "1"
check "sentinel restored" "$(RB "cat $CN/homeassistant/sentinel.txt")" "pre"
check "failed config kept aside" "$(RB "ls -d $CN/homeassistant.failed-* 2>/dev/null | wc -l")" "1"
echo "== 5. real apply (want rc 0, 2026.9.4, compose pinned)"
bash $S --apply 2>&1 | sed 's/^/    /'; RC=${PIPESTATUS[0]}
check "apply rc" "$RC" 0; check "version now" "$(cver)" "2026.9.4"
check "compose pinned" "$(RB "grep -c 'home-assistant:2026.9.4\$' $CN/compose.yaml")" "1"
echo "== 6. re-apply is idempotent (want rc 0, no restart)"
T1=$(cstart); bash $S --apply 2>&1 | sed 's/^/    /'; check "idempotent rc" "${PIPESTATUS[0]}" 0; check "not restarted" "$(cstart)" "$T1"
echo "== 7. manual rollback from upgraded state (want rc 1, back on 2026.7.2)"
bash $S --rollback 2>&1 | sed 's/^/    /'; check "manual rollback rc" "${PIPESTATUS[0]}" 1; check "version after rollback" "$(cver)" "2026.7.2"
echo "== 8. prod HA untouched"; check "prod start+image unchanged" "$(pstart)" "$P0"
echo "== teardown (remaining ha-canary containers should be 0)"; teardown
check "prod still unchanged after teardown" "$(pstart)" "$P0"
echo "SUMMARY pass=$PASS fail=$FAIL"
rm -rf $STATE_DIR
echo "=== DONE ==="
