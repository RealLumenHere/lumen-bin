#!/bin/bash
# require-vault-backup.sh — shared gate for credential rotation scripts (wq#3747).
#
# RULE: the old credential MUST be backed up BEFORE the live value is changed.
# This function enforces that rule at the rotation chokepoint.
#
# Usage (bash scripts):
#   source /home/lumen/bin/lib/require-vault-backup.sh
#   require_vault_backup "fleet-pw"
#   require_vault_backup "github-frank.token" "/home/lumen/.secrets/github-frank.token"
#
# Usage (Python scripts via subprocess, or standalone):
#   /home/lumen/bin/lib/require-vault-backup.sh KEY_NAME [LIVE_FILE]
#
# Arguments:
#   $1  KEY_NAME   — logical credential name (e.g. "fleet-pw", "CEREBRAS_API_KEY")
#   $2  LIVE_FILE  — (optional) path to the live credential file for sha256 audit
#
# Checks (any ONE passing = gate open):
#   1. Dated backup in /home/lumen/.secrets/backups/ matching KEY_NAME*  (within 7 days)
#      (when LIVE_FILE is given the backup must also be byte-identical to it)
#   2. Dated backup in /home/lumen/.secrets/bak/ matching KEY_NAME*  (within 7 days)
#   3. If LIVE_FILE given: a .bak.* sibling of LIVE_FILE (within 7 days)
#
# Exit codes:
#   0  backup found — rotation may proceed
#   2  NO backup found — rotation REFUSED
#   1  usage error

# RVB_BACKUP_DIRS / RVB_LOG: test overrides only (space-separated dirs).
read -r -a BACKUP_DIRS <<< "${RVB_BACKUP_DIRS:-/home/lumen/.secrets/backups /home/lumen/.secrets/bak}"
LOG="${RVB_LOG:-/home/lumen/logs/credential-rotation.log}"
MAX_AGE_DAYS=7

_rvb_log() {
    local ts
    ts=$(date -u +%Y-%m-%dT%H:%M:%SZ)
    mkdir -p "$(dirname "$LOG")"
    printf '%s %s\n' "$ts" "$*" >> "$LOG"
}

_rvb_sha256() {
    # sha256 of file content; prints "none" if unreadable
    if [ -f "$1" ] && [ -r "$1" ]; then
        sha256sum "$1" | cut -d" " -f1
    else
        echo "none"
    fi
}

_rvb_find_backup() {
    local key="$1"
    local live="${2:-}"
    local cutoff live_hash="none"
    cutoff=$(date -d "-${MAX_AGE_DAYS} days" +%s 2>/dev/null) || cutoff=0
    # When the live value is known, a backup only counts if it IS that value
    # (byte-identical): a stale .bak holding an OLDER credential does not protect
    # the one about to be destroyed. Without LIVE_FILE: name + age only.
    [ -n "$live" ] && [ -f "$live" ] && live_hash=$(_rvb_sha256 "$live")

    # Search backup directories for KEY_NAME* files
    for dir in "${BACKUP_DIRS[@]}"; do
        [ -d "$dir" ] || continue
        while IFS= read -r -d $'\0' f; do
            local mtime
            mtime=$(stat -c %Y "$f" 2>/dev/null) || continue
            if [ "$mtime" -ge "$cutoff" ]; then
                if [ "$live_hash" != "none" ] && [ "$(_rvb_sha256 "$f")" != "$live_hash" ]; then
                    continue
                fi
                echo "$f"
                return 0
            fi
        done < <(find "$dir" -maxdepth 1 -name "${key}*" -print0 2>/dev/null)
    done

    # Search sibling .bak.* files of the live credential
    if [ -n "$live" ] && [ -f "$live" ]; then
        local dir base
        dir=$(dirname "$live")
        base=$(basename "$live")
        while IFS= read -r -d $'\0' f; do
            local mtime
            mtime=$(stat -c %Y "$f" 2>/dev/null) || continue
            if [ "$mtime" -ge "$cutoff" ]; then
                if [ "$live_hash" != "none" ] && [ "$(_rvb_sha256 "$f")" != "$live_hash" ]; then
                    continue
                fi
                echo "$f"
                return 0
            fi
        done < <(find "$dir" -maxdepth 1 -name "${base}.bak.*" -print0 2>/dev/null)
    fi

    return 1
}

require_vault_backup() {
    local key="${1:-}"
    local live="${2:-}"

    if [ -z "$key" ]; then
        echo "require_vault_backup: KEY_NAME required" >&2
        return 1
    fi

    local backup_file
    backup_file=$(_rvb_find_backup "$key" "$live")
    if [ $? -ne 0 ]; then
        echo "ROTATION REFUSED: no backup found for credential '${key}'." >&2
        echo "  Searched: ${BACKUP_DIRS[*]}" >&2
        [ -n "$live" ] && echo "  Also searched sibling .bak.* of: ${live}" >&2
        echo "  Back up the OLD value first:" >&2
        echo "    cp <live-file> /home/lumen/.secrets/backups/${key}.bak.\$(date +%Y%m%d_%H%M%S)   (must match the CURRENT value)" >&2
        echo "  Then re-run this rotation." >&2
        _rvb_log "REFUSED key=${key} live=${live:-none} reason=no_backup_found"
        return 2
    fi

    local live_hash="none" backup_hash
    [ -n "$live" ] && [ -f "$live" ] && live_hash=$(_rvb_sha256 "$live")
    backup_hash=$(_rvb_sha256 "$backup_file")

    _rvb_log "ALLOWED key=${key} live=${live:-none} live_sha256=${live_hash} backup=${backup_file} backup_sha256=${backup_hash}"
    return 0
}

# When invoked as a standalone script (not sourced), run the gate
if [ "${BASH_SOURCE[0]}" = "$0" ] || [ "${BASH_SOURCE[0]:-}" = "" ]; then
    require_vault_backup "$@"
    exit $?
fi
