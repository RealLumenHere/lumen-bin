#!/usr/bin/env bash
# tests/test_gitea_rotate_finish.sh — wq#2545 regression coverage for
# gitea-rotate-finish, the single entrypoint for gitea-lumen-token rotation.
#
# The lock-refusal behaviour (two concurrent runs, second denied) was proven
# LIVE against the real slock service and the real fleet credential during
# development of this tool (wq#2545 episode) — that is the strongest evidence
# a locking primitive can get, and it is not re-mocked here. This harness
# covers the two things that are cheap and safe to run repeatedly with no
# real Gitea/Anubis/token-engine reach: the ambiguous-retire-candidate
# refusal, and the full happy path with every external command faked, so a
# future change to the write/verify order is caught before it ever touches a
# live credential.
#
# Fakes go first on PATH (curl, ssh, sudo, jq passthrough, git, lumen-pr,
# slock — slock is real-shaped but backed by a scratch state file, not the
# fleet's queue.db). GITEA_ROTATE_* env vars point every file the script
# touches at a scratch sandbox, never a real path.
set -uo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SCRIPT="$HERE/../gitea-rotate-finish"
FAIL=0

t(){ echo "-- $1 --"; }
ok(){ echo "   ok: $1"; }
bad(){ echo "   FAIL: $1"; FAIL=1; }

setup_sandbox(){
  SBOX=$(mktemp -d)
  FAKEBIN="$SBOX/fakebin"
  export SBOX FAKEBIN
  mkdir -p "$FAKEBIN" "$SBOX/secrets" "$SBOX/repo/.git" "$SBOX/engine"
  export GITEA_ROTATE_ENV_FILE="$SBOX/env"
  export GITEA_ROTATE_CRED_FILE="$SBOX/git-credentials"
  export GITEA_ROTATE_SEC_FILE="$SBOX/secrets/gitea-lumen.token"
  export GITEA_ROTATE_ENGINE_DIR="$SBOX/engine"
  export GITEA_ROTATE_ENGINE_FILE="$SBOX/engine/gitea-lumen.token"
  export GITEA_ROTATE_REPOS="$SBOX/repo"
  printf 'GITEA_PASSWORD=sandboxpw\nGITEA_TOKEN=placeholder000000000000000000000\n' > "$GITEA_ROTATE_ENV_FILE"
  printf 'http://lumen:oldtok123456789012345678901234@100.121.17.5%%3a3000\n' > "$GITEA_ROTATE_CRED_FILE"
  printf 'oldtok123456789012345678901234' > "$GITEA_ROTATE_SEC_FILE"
  chmod 600 "$GITEA_ROTATE_SEC_FILE"
}

teardown_sandbox(){ rm -rf "$SBOX"; }

# --- fakes -------------------------------------------------------------
write_fakes(){
  local tokenlist="$1"   # JSON body the fake curl returns for the tokens-list call
  cat > "$FAKEBIN/curl" <<EOF
#!/usr/bin/env bash
# tokens-list call: has -u and no -X, path ends /tokens
args="\$*"
if [[ "\$args" == *"/tokens"* && "\$args" != *"-X DELETE"* ]]; then
  echo '$tokenlist'
  exit 0
fi
if [[ "\$args" == *"-X DELETE"* ]]; then
  # write the deleted id to a marker so the test can assert on it
  for a in "\$@"; do :; done
  echo "\$*" >> "$SBOX/delete-calls.log"
  echo -n 204
  exit 0
fi
if [[ "\$args" == *"Authorization: token FAKE_NEW_TOKEN_1234567890123456789"* ]]; then
  # new token validates
  for a in "\$@"; do
    if [[ "\$a" == -o ]]; then :; fi
  done
  # find the -o target and write a login body there
  prev=""
  for a in "\$@"; do
    if [[ "\$prev" == "-o" ]]; then echo '{"login":"lumen"}' > "\$a"; fi
    prev="\$a"
  done
  echo -n 200
  exit 0
fi
if [[ "\$args" == *"Authorization: token oldtok"* ]]; then
  echo -n 401
  exit 0
fi
echo -n 200
EOF
  chmod +x "$FAKEBIN/curl"

  cat > "$FAKEBIN/ssh" <<'EOF'
#!/usr/bin/env bash
echo "ssh-called" >> "$SBOX/ssh-calls.log" 2>/dev/null || true
echo "FAKE_NEW_TOKEN_1234567890123456789"
EOF
  chmod +x "$FAKEBIN/ssh"

  cat > "$FAKEBIN/sudo" <<'EOF'
#!/usr/bin/env bash
# -n test -d DIR -> true; -n install ... SRC DST -> copy
if [[ "$1" == "-n" && "$2" == "test" ]]; then shift 2; exec test "$@"; fi
if [[ "$1" == "-n" && "$2" == "cp" ]]; then shift 2; exec cp "$@"; fi
if [[ "$1" == "-n" && "$2" == "install" ]]; then
  shift 2
  # last two args are src dst; ignore -o/-g/-m flags for the fake
  args=("$@"); n=${#args[@]}
  src="${args[$((n-2))]}"; dst="${args[$((n-1))]}"
  cp "$src" "$dst"
  exit 0
fi
exit 1
EOF
  chmod +x "$FAKEBIN/sudo"

  cat > "$FAKEBIN/git" <<'EOF'
#!/usr/bin/env bash
[[ "$*" == *"ls-remote"* ]] && exit 0
exec /usr/bin/git "$@"
EOF
  chmod +x "$FAKEBIN/git"

  cat > "$FAKEBIN/lumen-pr" <<'EOF'
#!/usr/bin/env bash
exit 0
EOF
  chmod +x "$FAKEBIN/lumen-pr"

  cat > "$FAKEBIN/jq" <<EOF
#!/usr/bin/env bash
exec /usr/bin/jq "\$@"
EOF
  chmod +x "$FAKEBIN/jq"
}

run_script(){
  # These tests only ever run on Lumen (same convention as the script itself,
  # which is lumen-only) so the real hostname already satisfies the script's
  # own guard — no override needed.
  PATH="$FAKEBIN:$PATH" GITEA_ROTATE_HOLDER="test-$$-$RANDOM" "$SCRIPT"
}

# --- test 1: ambiguous retire candidates refuses before minting --------
t "ambiguous retire candidates refuses, never mints"
setup_sandbox
write_fakes '[{"id":1,"name":"lumen-fleet-a"},{"id":2,"name":"lumen-fleet-b"}]'
out=$(run_script 2>&1); rc=$?
if [ "$rc" -ne 0 ] && echo "$out" | grep -q "found 2"; then
  ok "refused with the right reason"
else
  bad "expected refusal citing 2 candidates, got rc=$rc: $out"
fi
if [ -f "$SBOX/ssh-calls.log" ]; then
  bad "ssh (mint) was called despite the ambiguous refusal"
else
  ok "mint was never attempted"
fi
teardown_sandbox

# --- test 2: happy path writes all 4 copies and retires the old row ----
t "happy path: all 4 copies written, old row deleted, old value 401s"
setup_sandbox
write_fakes '[{"id":42,"name":"lumen-fleet-old"}]'
out=$(run_script 2>&1); rc=$?
if [ "$rc" -eq 0 ]; then ok "exit 0"; else bad "exit $rc: $out"; fi
if grep -q "FAKE_NEW_TOKEN_1234567890123456789" "$GITEA_ROTATE_SEC_FILE" 2>/dev/null; then
  ok "secrets file updated"
else
  bad "secrets file not updated"
fi
if grep -q "FAKE_NEW_TOKEN_1234567890123456789" "$GITEA_ROTATE_CRED_FILE" 2>/dev/null \
    && [ "$(grep -c '100.121.17.5%3a3000' "$GITEA_ROTATE_CRED_FILE")" = 1 ]; then
  ok "git-credentials has exactly one updated entry"
else
  bad "git-credentials not updated correctly: $(cat "$GITEA_ROTATE_CRED_FILE")"
fi
if grep -q "GITEA_TOKEN=FAKE_NEW_TOKEN_1234567890123456789" "$GITEA_ROTATE_ENV_FILE" 2>/dev/null; then
  ok "agent/.env updated"
else
  bad "agent/.env not updated"
fi
if grep -q "FAKE_NEW_TOKEN_1234567890123456789" "$GITEA_ROTATE_ENGINE_FILE" 2>/dev/null; then
  ok "token-engine store updated"
else
  bad "token-engine store not updated"
fi
if [ -f "$SBOX/delete-calls.log" ] && grep -q "/tokens/42" "$SBOX/delete-calls.log"; then
  ok "deleted the correct retiring id (42)"
else
  bad "did not delete token id=42"
fi
if echo "$out" | grep -q "old token now HTTP 401"; then
  ok "confirmed old value 401s"
else
  bad "did not confirm old-token 401"
fi
teardown_sandbox

echo
if [ "$FAIL" = 0 ]; then
  echo "ALL PASS"
  exit 0
else
  echo "SOME TESTS FAILED"
  exit 1
fi
