#!/usr/bin/env bash
# lumen-github-identity.sh — shared per-repo GitHub token resolution.
#
# WHY: wq#525. lumen-pr, lumen-ruleset and lumen-git-guard each read a single
# GITHUB_TOKEN out of /home/lumen/agent/.env, so every repo they touch is
# attributed to whichever identity owns that one token — even repos that have
# their own bot identity (e.g. RealLumenHere on lumen-plays-pokemon, wq#505).
# Repointing agent/.env itself would be a fleet-wide auth outage: that file
# backs every repo these tools touch, most of which the alternate identity
# cannot reach. This file is the narrow fix: an opt-in map from repo slug to
# token file, with the untouched agent/.env read as the fallback.
#
# Resolution order (wq#524 inserted the middle rung):
#   1. identity-map.conf entry for this exact slug      — per-repo bot identity
#   2. /home/lumen/.secrets/github-frank.token          — DEFAULT, the convention
#   3. /home/lumen/agent/.env GITHUB_TOKEN              — DEPRECATED, warns, one release
# Every rung falls through to the next on missing/unreadable/empty rather than
# failing, so moving a secret can never be a fleet-wide auth outage by itself.
# TOKEN_SOURCE always names the rung that answered, so `[token: ...]` output in
# lumen-ruleset/lumen-pr tells you which identity actually signed a call.
#
# Map format (/home/lumen/infra/github/identity-map.conf by default), one
# entry per line, '#' comments and blank lines ignored:
#   owner/repo = /path/to/token-file
#
# Usage: source this file, then per repo:
#   resolve_github_token "$SLUG" || die "no usable GitHub token for $SLUG"
# On success it exports GITHUB_TOKEN and sets TOKEN_SOURCE to the file it came
# from (a path, never the token — safe to log). Never prints the token itself.
set -uo pipefail

GITHUB_IDENTITY_MAP=${GITHUB_IDENTITY_MAP:-/home/lumen/infra/github/identity-map.conf}
# wq#524: the default identity now lives with every other fleet secret, one file
# per credential, mode 600 — the convention bes_ha_token/github-lumen.token/
# wrist_token/slack.env already follow. agent/.env keeps a copy for one release
# so nothing breaks mid-flight, but it is the DEPRECATED source: resolving from
# it emits a warning naming the file to fold, and it is not a rotation point.
GITHUB_DEFAULT_TOKEN_FILE=${GITHUB_DEFAULT_TOKEN_FILE:-/home/lumen/.secrets/github-frank.token}
GITHUB_ENV_FILE=${GITHUB_ENV_FILE:-/home/lumen/agent/.env}

_lgi_map_lookup() { # _lgi_map_lookup <slug> -> prints mapped token-file path, or nothing
  local slug="$1"
  [ -r "$GITHUB_IDENTITY_MAP" ] || return 0
  awk -F= -v s="$slug" '
    /^[[:space:]]*#/ { next }
    /^[[:space:]]*$/ { next }
    {
      k = $1; v = $2
      gsub(/^[[:space:]]+|[[:space:]]+$/, "", k)
      gsub(/^[[:space:]]+|[[:space:]]+$/, "", v)
      if (k == s) { print v; exit }
    }' "$GITHUB_IDENTITY_MAP"
}

resolve_github_token() { # resolve_github_token <owner/repo>
  local slug="${1:-}" mapped=""
  GITHUB_TOKEN=""
  TOKEN_SOURCE=""
  [ -n "$slug" ] && mapped=$(_lgi_map_lookup "$slug")

  if [ -n "$mapped" ]; then
    if [ -r "$mapped" ]; then
      GITHUB_TOKEN=$(tr -d '\n\r' < "$mapped")
      if [ -n "$GITHUB_TOKEN" ]; then
        export GITHUB_TOKEN
        TOKEN_SOURCE="$mapped"
        return 0
      fi
      echo "lumen-github-identity: $mapped (mapped for $slug) is empty — falling back to $GITHUB_ENV_FILE" >&2
    else
      echo "lumen-github-identity: $slug is mapped to $mapped but it is unreadable — falling back to $GITHUB_ENV_FILE" >&2
    fi
  fi

  # wq#524: the default identity, from the fleet secrets convention. Unreadable or
  # empty falls through to agent/.env exactly as a bad MAPPED file does — a secrets
  # move must not be able to take every repo offline on its own.
  if [ -r "$GITHUB_DEFAULT_TOKEN_FILE" ]; then
    GITHUB_TOKEN=$(tr -d '\n\r' < "$GITHUB_DEFAULT_TOKEN_FILE")
    if [ -n "$GITHUB_TOKEN" ]; then
      export GITHUB_TOKEN
      TOKEN_SOURCE="$GITHUB_DEFAULT_TOKEN_FILE"
      return 0
    fi
    echo "lumen-github-identity: $GITHUB_DEFAULT_TOKEN_FILE is empty — falling back to $GITHUB_ENV_FILE" >&2
  fi

  [ -r "$GITHUB_ENV_FILE" ] || { echo "lumen-github-identity: cannot read $GITHUB_ENV_FILE" >&2; return 1; }
  GITHUB_TOKEN=$(grep -m1 -E '^GITHUB_TOKEN=' "$GITHUB_ENV_FILE" | cut -d= -f2- | tr -d '"'"'"' \r')
  [ -n "$GITHUB_TOKEN" ] || { echo "lumen-github-identity: GITHUB_TOKEN not set in $GITHUB_ENV_FILE" >&2; return 1; }
  export GITHUB_TOKEN
  TOKEN_SOURCE="$GITHUB_ENV_FILE"
  # wq#524: reaching here means the credential is being served from its SECOND home.
  # Loud on purpose — this is the one release of grace, and a token with two homes
  # has two rotation points, which is how a rotated PAT comes back from the dead.
  echo "lumen-github-identity: DEPRECATED token source $GITHUB_ENV_FILE (GITHUB_TOKEN) — fold it into $GITHUB_DEFAULT_TOKEN_FILE (mode 600); agent/.env stops being read after this release" >&2
  return 0
}
