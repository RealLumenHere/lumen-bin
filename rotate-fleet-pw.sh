#!/bin/bash
# rotate-fleet-pw.sh — serialized fleet sudo-password rotation (wq#304 plan section 2, staged wq#305).
# FRANK RUNS THIS. Invocation:  bash /home/lumen/bin/rotate-fleet-pw.sh [--revert NODE] [--include-khepri]
# Reads NEW pw from /home/lumen/secrets/.new_fleet_pw, OLD from /home/lumen/.fleet_pw.
# The password value NEVER appears in argv, xtrace, or any echoed command line:
#  - set +x enforced; values live only in shell variables inside this process
#  - remote change is `chpasswd` fed via SSH stdin (no remote argv, no remote history)
#  - positive-auth check uses sshpass -e (SSHPASS env), never -p
# Per-node: flip -> verify (key ssh / sudo -n / new-pw auth or passwd -S fallback / old-pw NEGATIVE) -> next.
# ANY verify failure: HALT with state table. Un-flipped nodes still accept the OLD pw = rollback path.
# Revert one node:  --revert NODE  (swaps file roles for that node only).
set -u +x
umask 077

NEW_F=/home/lumen/secrets/.new_fleet_pw
OLD_F=/home/lumen/.fleet_pw
KEY=/home/lumen/.ssh/id_ed25519
TKEY=/home/lumen/.ssh/thoth_ed25519
SSHO="-o BatchMode=yes -o ConnectTimeout=10 -o PreferredAuthentications=publickey"

[ -r "$NEW_F" ] || { echo "FATAL: $NEW_F missing/unreadable"; exit 1; }
[ -r "$OLD_F" ] || { echo "FATAL: $OLD_F missing/unreadable"; exit 1; }
NEW=$(tr -d '\n' < "$NEW_F")
OLD=$(tr -d '\n' < "$OLD_F")
[ -n "$NEW" ] || { echo "FATAL: new pw empty"; exit 1; }
[ "$NEW" != "$OLD" ] || { echo "FATAL: new == old"; exit 1; }

# node table: name|user|host|keyfile   (order = blast-radius ascending, vault pair LAST)
NODES="
bes|bes|100.110.156.91|$KEY
apep|apep|100.82.2.72|$KEY
seshat|seshat|192.168.8.219|$KEY
anubis|anubis|192.168.8.232|$KEY
isis|isis|100.100.129.24|$KEY
hathor|hathor|100.80.71.13|$KEY
nephthys|nephthys|192.168.8.176|$KEY
thor|thor|100.112.151.100|$KEY
thoth|thoth|192.168.8.218|$TKEY
lumen|lumen|LOCAL|-
maat|maam|100.105.219.34|$KEY
wadjet|wadjet|100.116.217.15|$KEY
"
# khepri excluded until audited (wq#304 section 1.2); --include-khepri appends it.
case "${1:-}" in
  --include-khepri) NODES="$NODES
khepri|khepri|100.120.16.78|$KEY"; shift ;;
esac

REVERT_NODE=""
if [ "${1:-}" = "--revert" ]; then
  REVERT_NODE="${2:?usage: --revert NODE}"
  T="$NEW"; NEW="$OLD"; OLD="$T"
  echo "REVERT MODE: $REVERT_NODE gets the OLD password back"
fi

run_remote() { # user host keyfile cmd... (stdin passes through)
  local u=$1 h=$2 k=$3; shift 3
  if [ "$h" = "LOCAL" ]; then bash -c "$*"; else ssh -i "$k" $SSHO "$u@$h" "$@"; fi
}

flip_node() { # name user host keyfile
  local name=$1 u=$2 h=$3 k=$4 rc
  echo "== $name ($u@$h) =="
  # 1) FLIP: chpasswd via stdin (sudo -n = NOPASSWD required, verified fleet-wide wq#304)
  printf '%s:%s\n' "$u" "$NEW" | run_remote "$u" "$h" "$k" 'sudo -n chpasswd' || { echo "FAIL[$name]: chpasswd rc=$?"; return 1; }
  # 2) VERIFY key ssh still good
  run_remote "$u" "$h" "$k" 'true' </dev/null || { echo "FAIL[$name]: key ssh broken post-flip"; return 1; }
  # 3) VERIFY sudo -n unaffected
  run_remote "$u" "$h" "$k" 'sudo -n true' </dev/null || { echo "FAIL[$name]: sudo -n broken post-flip"; return 1; }
  # 4) VERIFY new pw positive (password-auth ssh; skip gracefully if sshd refuses passwords)
  if [ "$h" != "LOCAL" ]; then
    if SSHPASS="$NEW" sshpass -e ssh -o PreferredAuthentications=password -o PubkeyAuthentication=no \
         -o ConnectTimeout=10 -o StrictHostKeyChecking=accept-new "$u@$h" true </dev/null 2>/dev/null; then
      echo "  ok: new pw auth positive"
      # 5) VERIFY old pw NEGATIVE
      if SSHPASS="$OLD" sshpass -e ssh -o PreferredAuthentications=password -o PubkeyAuthentication=no \
           -o ConnectTimeout=10 "$u@$h" true </dev/null 2>/dev/null; then
        echo "FAIL[$name]: OLD password still authenticates"; return 1
      fi
      echo "  ok: old pw negative"
    else
      # sshd may have PasswordAuthentication no — fall back to passwd -S date check
      local st; st=$(run_remote "$u" "$h" "$k" "sudo -n passwd -S $u" </dev/null 2>/dev/null)
      echo "  note: password-auth ssh unavailable; passwd -S: $st"
      echo "$st" | grep -q "$(date +%Y-%m-%d)" || { echo "FAIL[$name]: passwd -S does not show today's change"; return 1; }
    fi
  else
    local st; st=$(sudo -n passwd -S "$u" 2>/dev/null)
    echo "  local passwd -S: $st"
    echo "$st" | grep -q "$(date +%Y-%m-%d)" || { echo "FAIL[lumen]: passwd -S does not show today's change"; return 1; }
  fi
  echo "OK[$name]"
}

FLIPPED=""
while IFS='|' read -r name u h k; do
  [ -z "$name" ] && continue
  if [ -n "$REVERT_NODE" ] && [ "$name" != "$REVERT_NODE" ]; then continue; fi
  if ! flip_node "$name" "$u" "$h" "$k"; then
    echo ""
    echo "HALTED at $name. Flipped so far (have NEW pw): ${FLIPPED:-none}"
    echo "Remaining nodes still accept the OLD pw. Revert this node: $0 --revert $name"
    exit 2
  fi
  FLIPPED="$FLIPPED $name"
done <<EOF2
$NODES
EOF2

if [ -z "$REVERT_NODE" ]; then
  echo ""
  echo "ALL GREEN:$FLIPPED"
  echo "Finalizing: /home/lumen/.fleet_pw <- new value (feeds fleet-provision-ssh)"
  ( umask 077; printf '%s' "$NEW" > "$OLD_F" )
  echo "DONE. Next (manual, per plan): leak scrub (section 3), VW seed (section 4), creds-file + state updates (section 5)."
  echo "Do NOT shred $NEW_F until the VW cipher is confirmed (wq#306)."
fi
