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

  [ -r "$GITHUB_ENV_FILE" ] || { echo "lumen-github-identity: cannot read $GITHUB_ENV_FILE" >&2; return 1; }
  GITHUB_TOKEN=$(grep -m1 -E '^GITHUB_TOKEN=' "$GITHUB_ENV_FILE" | cut -d= -f2- | tr -d '"'"'"' \r')
  [ -n "$GITHUB_TOKEN" ] || { echo "lumen-github-identity: GITHUB_TOKEN not set in $GITHUB_ENV_FILE" >&2; return 1; }
  export GITHUB_TOKEN
  TOKEN_SOURCE="$GITHUB_ENV_FILE"
  return 0
}
