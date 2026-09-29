#!/bin/bash
# Test-to-failure of ollama-upgrade against a THROWAWAY Ollama instance on wadjet (idle, LAN 192.168.8.173):
# own prefix (/opt/ollama-canary), own systemd unit (ollama-canary), own port (11435), own model dir - so
# a real prod ollama install on this or any other node is never touched.
# Cases: no-state rollback, precheck, forced verify failure (rolls back incl. unit-untouched check), real
# upgrade with a real generation, idempotent re-apply, manual rollback.
S=${S:-$(cd "$(dirname "$0")/.." && pwd)/ollama-upgrade}
B=wadjet@192.168.8.173
NAME=wadjet; USR=wadjet; LAN=192.168.8.173
CPREFIX=/opt/ollama-canary
export PREFIX=$CPREFIX SERVICE=ollama-canary API_PORT=11435 MODEL=smollm2:135m
export STATE_DIR=/tmp/ollama_test_state_wq3096; rm -rf "$STATE_DIR"
OLD_VERSION=0.32.6
OLD_SHA256=dec2fa50d24e6868ca3c4c977d69d059399372105f951a9acc320a5a79aadcfc
TARGET_VERSION=0.34.4
[ "$PREFIX" = "/usr/local" ] && { echo "REFUSED: harness must never target the real prefix"; exit 9; }
PASS=0; FAIL=0
check() { if [ "$2" = "$3" ]; then echo "  PASS $1 (got $2)"; PASS=$((PASS+1)); else echo "  FAIL $1 (want $3 got $2)"; FAIL=$((FAIL+1)); fi; }
RB() { ssh -n -o BatchMode=yes -o ConnectTimeout=8 "$B" "$@"; }
cver()   { RB "curl -s -m 5 http://127.0.0.1:11435/api/version" 2>/dev/null | python3 -c 'import json,sys; print(json.load(sys.stdin).get("version",""))' 2>/dev/null; }
cstart() { RB "systemctl show ollama-canary -p ActiveEnterTimestamp --value" 2>/dev/null; }

setup() {
  RB "sudo systemctl stop ollama-canary 2>/dev/null; sudo rm -rf $CPREFIX /etc/systemd/system/ollama-canary.service*; sudo mkdir -p $CPREFIX/bin $CPREFIX/lib/ollama $CPREFIX/models" || return 1
  RB "curl -fsSL -m 300 -o /tmp/ollama-canary-old.tar.zst https://github.com/ollama/ollama/releases/download/v$OLD_VERSION/ollama-linux-amd64.tar.zst" || return 1
  RB "echo '$OLD_SHA256  /tmp/ollama-canary-old.tar.zst' | sha256sum -c -" || return 1
  RB "sudo zstd -dc /tmp/ollama-canary-old.tar.zst | sudo tar -x -C $CPREFIX" || return 1
  RB "sudo tee /etc/systemd/system/ollama-canary.service >/dev/null <<'EOF'
[Unit]
Description=Ollama canary (wq#3096 harness)
After=network-online.target

[Service]
ExecStart=$CPREFIX/bin/ollama serve
Environment=\"OLLAMA_HOST=127.0.0.1:11435\"
Environment=\"OLLAMA_MODELS=$CPREFIX/models\"
Restart=always
RestartSec=3

[Install]
WantedBy=multi-user.target
EOF
sudo systemctl daemon-reload && sudo systemctl enable --now ollama-canary"
}

teardown() {
  RB "sudo systemctl stop ollama-canary 2>/dev/null; sudo systemctl disable ollama-canary 2>/dev/null; sudo rm -f /etc/systemd/system/ollama-canary.service; sudo systemctl daemon-reload; sudo rm -rf $CPREFIX /tmp/ollama-canary-old.tar.zst /tmp/ollama-upgrade-cache; systemctl list-units --all 2>/dev/null | grep -c ollama-canary" 2>&1
}

echo "== start $(date -u +%H:%M:%SZ)"
echo "== setup canary on old version $OLD_VERSION"
setup || { echo "SETUP FAILED"; exit 1; }
for i in $(seq 1 20); do [ -n "$(cver)" ] && break; sleep 3; done
pull_body="{\"model\":\"smollm2:135m\",\"stream\":false}"
RB "curl -s -m 300 http://127.0.0.1:11435/api/pull -d $(printf %q "$pull_body")" >/dev/null 2>&1
check "canary up on old version" "$(cver)" "$OLD_VERSION"

echo "== 1. rollback with no state must refuse loudly (want rc 2)"
bash "$S" --rollback $NAME $USR $LAN 2>&1 | sed 's/^/    /'; check "no-state rollback rc" "${PIPESTATUS[0]}" 2

echo "== 2. precheck (want rc 0)"
bash "$S" --precheck $NAME $USR $LAN 2>&1 | sed 's/^/    /'; check "precheck rc" "${PIPESTATUS[0]}" 0

echo "== 3. apply with INJECTED verify failure (want rc 1, old version restored)"
FORCE_VERIFY_FAIL=1 bash "$S" --apply $NAME $USR $LAN 2>&1 | sed 's/^/    /'; RC=${PIPESTATUS[0]}
check "inject rc" "$RC" 1; check "version restored" "$(cver)" "$OLD_VERSION"

echo "== 4. real apply (want rc 0, $TARGET_VERSION)"
bash "$S" --apply $NAME $USR $LAN 2>&1 | sed 's/^/    /'; RC=${PIPESTATUS[0]}
check "apply rc" "$RC" 0; check "version now" "$(cver)" "$TARGET_VERSION"

echo "== 5. re-apply is idempotent (want rc 0, no restart)"
T1=$(cstart)
bash "$S" --apply $NAME $USR $LAN 2>&1 | sed 's/^/    /'; check "idempotent rc" "${PIPESTATUS[0]}" 0
check "not restarted" "$(cstart)" "$T1"

echo "== 6. manual rollback (want rc 1, back to $OLD_VERSION)"
bash "$S" --rollback $NAME $USR $LAN 2>&1 | sed 's/^/    /'; check "manual rollback rc" "${PIPESTATUS[0]}" 1
check "version after rollback" "$(cver)" "$OLD_VERSION"

echo "== teardown (remaining ollama-canary units should be 0)"
teardown
echo "SUMMARY pass=$PASS fail=$FAIL"
rm -rf "$STATE_DIR"
echo "=== DONE ==="
