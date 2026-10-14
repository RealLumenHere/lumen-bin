#!/bin/bash
# rotate-fleet-pw.sh — serialized fleet sudo-password rotation (wq#304 plan section 2, staged wq#305).
# FRANK RUNS THIS. Invocation:  bash /home/lumen/bin/rotate-fleet-pw.sh [--include-bench] [--only N[,N]] [--new-file F] [--old-file F] [--revert NODE] [--list]
# Catch-up for stragglers (wq#2474): --only khepri,wadjet --new-file /home/lumen/.fleet_pw --old-file /home/lumen/secrets/.fleet_pw.old-pending
#   --only/--old-file runs NEVER overwrite the old-pw file at the end (other nodes may still need it).
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
SSHO="-o BatchMode=yes -o ConnectTimeout=10 -o PreferredAuthentications=publickey"
FLEET_INV=${FLEET_INV:-/home/lumen/bin/fleet-inv}

# wq#1456: the node table comes from the fleet inventory (`fleet-inv ssh-list`, DESIGN_SOT.md) —
# no list lives here. This script deliberately uses ALL live statuses (--all), not MONITORED():
# a parked/standby box (hathor, wadjet) still holds the OLD password and must be flipped, so it
# is powered on for the rotation or the run HALTs at it — the fail-safe this script wants.
# Decommissioned/dead/in-storage boxes (nephthys) are gone for good and never appear.
#   --order maint  = blast-radius ascending per A11 §5 (ops.maint_tier), vault pair among the last
#   --self LOCAL   = this host flips its own password locally (no ssh)
#   ssh_key column = per-node key file (thoth: network.ssh.key); '-' = $KEY
# Bench boxes (khepri) join only with --include-bench (alias --include-khepri; wq#304 section
# 1.2: excluded until audited).   --list prints the resolved table and exits (no secrets read).
STATUSES=active,standby
ONLY=""; CATCHUP=0
while :; do
  case "${1:-}" in
    --include-khepri|--include-bench) STATUSES=active,standby,bench; shift ;;
    --only)     ONLY="${2:?usage: --only NODE[,NODE...]}"; CATCHUP=1; shift 2 ;;
    --new-file) NEW_F="${2:?usage: --new-file FILE}"; shift 2 ;;
    --old-file) OLD_F="${2:?usage: --old-file FILE}"; CATCHUP=1; shift 2 ;;
    *) break ;;
  esac
done
ROSTER=$("$FLEET_INV" ssh-list --all --status "$STATUSES" --order maint --self LOCAL --fields name,user,host,ssh_key) \
  || { echo "FATAL: fleet-inv ssh-list failed (inventory unreadable?) — no node table, nothing flipped"; exit 1; }
[ -n "$ROSTER" ] || { echo "FATAL: empty roster from inventory — nothing flipped"; exit 1; }
# node table: name|user|host|keyfile
NODES=$(printf '%s\n' "$ROSTER" | awk -F'\t' -v k="$KEY" '{ kf=($4=="-")?k:$4; print $1"|"$2"|"$3"|"kf }')
if [ -n "$ONLY" ]; then  # keep roster (blast-radius) order; every requested name must resolve or nothing runs
  for w in ${ONLY//,/ }; do
    printf '%s\n' "$NODES" | awk -F'|' -v n="$w" '$1==n{f=1} END{exit !f}' \
      || { echo "FATAL: --only node '$w' not in resolved roster (statuses=$STATUSES) — nothing flipped"; exit 1; }
  done
  NODES=$(printf '%s\n' "$NODES" | awk -F'|' -v want=",${ONLY}," 'index(want, "," $1 ",")')
fi
if [ "${1:-}" = "--list" ]; then printf '%s\n' "$NODES"; exit 0; fi

[ -r "$NEW_F" ] || { echo "FATAL: $NEW_F missing/unreadable"; exit 1; }
[ -r "$OLD_F" ] || { echo "FATAL: $OLD_F missing/unreadable"; exit 1; }
NEW=$(tr -d '\n' < "$NEW_F")
OLD=$(tr -d '\n' < "$OLD_F")
[ -n "$NEW" ] || { echo "FATAL: new pw empty"; exit 1; }
[ "$NEW" != "$OLD" ] || { echo "FATAL: new == old"; exit 1; }

# --- credential rotation safety gate (wq#3747) ---
source /home/lumen/bin/lib/require-vault-backup.sh
require_vault_backup "fleet-pw" "$OLD_F" || exit $?
# --- end gate ---


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
  if [ "$CATCHUP" = 1 ]; then
    echo "catch-up run: $OLD_F left untouched (nodes outside --only may still hold it); shred it only after the last straggler is done"
  else
    echo "Finalizing: /home/lumen/.fleet_pw <- new value (feeds fleet-provision-ssh)"
    ( umask 077; printf '%s' "$NEW" > "$OLD_F" )
    echo "DONE. Next (manual, per plan): leak scrub (section 3), VW seed (section 4), creds-file + state updates (section 5)."
    echo "Do NOT shred $NEW_F until the VW cipher is confirmed (wq#306)."
  fi
fi
