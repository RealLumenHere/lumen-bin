#!/usr/bin/env bash
# tests/test_branch_guard.sh — test-to-failure for .githooks/branch-guard (wq#1476).
# Runs entirely in a throwaway bare origin + clone (+ linked worktree) under mktemp.
# Never touches /home/lumen/bin.
#   (i)    direct commit on main is refused with a pointer to the worktree flow; staged work kept
#   (ii)   commit + push on a feature branch pass clean; tracked wq#886 pre-commit still chained
#   (iii)  --no-verify bypass lands, and the existing after-the-fact detectors still see it
#   (iv)   install is idempotent (re-run = all "current", no new backups, hooksPath stays unset)
#   (v)    install touches NO working-tree file (no checkout landmine on a dirty/feature tree)
#   (vi)   push feature->main and delete main are refused; tag push allowed
#   (vii)  detached HEAD commit allowed
#   (viii) a LINKED worktree on an OLD branch (no tracked hooks) is guarded too
#   (ix)   guard runs once per event when both dispatcher and tracked copy exist
#   (x)    uninstall restores hooksPath and removes only our files; status flips to NOT GUARDED
set -uo pipefail
HERE=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
SRC=$(cd "$HERE/.." && pwd)/.githooks
RECONCILE=${RECONCILE:-/home/lumen/foreman/harness/repo_reconcile.py}
SWEEP=${SWEEP:-/home/lumen/foreman/harness/repo_sweep.py}
T=$(mktemp -d /tmp/branch-guard-test.XXXXXX); trap 'rm -rf "$T"' EXIT
export HOME="$T/home"; mkdir -p "$HOME"
export GIT_AUTHOR_NAME=t GIT_AUTHOR_EMAIL=t@t GIT_COMMITTER_NAME=t GIT_COMMITTER_EMAIL=t@t
pass=0; fail=0; LAST_OUT=""
ok()  { pass=$((pass+1)); echo "PASS $*"; }
bad() { fail=$((fail+1)); echo "FAIL $*" >&2; }
expect_rc() { local want="$1"; shift; local out rc; out=$("$@" 2>&1); rc=$?
  [ "$rc" = "$want" ] && ok "$* -> rc=$rc" || { bad "$* -> rc=$rc (want $want)"; printf '%s\n' "$out" | sed 's/^/    /'; }; LAST_OUT=$out; }
expect_grep()   { printf '%s' "$LAST_OUT" | grep -qF -- "$1" && ok "output has: $1" || { bad "output lacks: $1"; printf '%s\n' "$LAST_OUT" | sed 's/^/    /'; }; }
expect_nogrep() { printf '%s' "$LAST_OUT" | grep -qF -- "$1" && { bad "output should NOT have: $1"; printf '%s\n' "$LAST_OUT" | sed 's/^/    /'; } || ok "output lacks (good): $1"; }

# --- fixture: bare origin; clone on main with the PRE-guard tracked pre-commit (as the live
# canonical tree has today), pushed; then a feature branch carrying the NEW tracked hooks ---
git init -q --bare "$T/origin.git" && git -C "$T/origin.git" symbolic-ref HEAD refs/heads/main
git clone -q "$T/origin.git" "$T/work" 2>/dev/null; W="$T/work"
git -C "$W" checkout -q -b main 2>/dev/null
mkdir -p "$W/.githooks"; printf '#!/usr/bin/env bash\necho "OLD-TRACKED-PRECOMMIT-RAN"\nexit 0\n' > "$W/.githooks/pre-commit"; chmod +x "$W/.githooks/pre-commit"
echo base > "$W/base"; git -C "$W" add -A; git -C "$W" commit -q -m "chore: base"
git -C "$W" push -q -u origin main 2>/dev/null; git -C "$W" remote set-head origin main
git -C "$W" config core.hooksPath .githooks         # the live convention
git -C "$W" worktree add -q "$T/oldwt" -b feat/old   # linked worktree on a branch WITHOUT the new hooks
# feature branch that carries the new tracked hooks (what origin/main looks like after this PR)
git -C "$W" checkout -q -b feat/hooks; cp "$SRC/branch-guard" "$SRC/pre-push" "$SRC/pre-commit" "$W/.githooks/"; git -C "$W" add .githooks; git -C "$W" commit -q -m "ci: hooks"
git -C "$W" checkout -q main
# sandboxed installer: BACKUP_DIR redirected into the temp dir
GUARD="$T/branch-guard"; sed "s#^BACKUP_DIR=.*#BACKUP_DIR=$T/backups#" "$SRC/branch-guard" > "$GUARD"; chmod +x "$GUARD"
HOOKS="$W/.git/hooks"

echo "== install into common hooks dir =="
snap_before=$(cd "$W" && git status --porcelain --untracked-files=all | sort | md5sum)
expect_rc 0 "$GUARD" install "$W"; expect_grep "GUARDED"; expect_grep "common-dir dispatchers"; expect_grep "core.hooksPath: unset (was '.githooks'"
snap_after=$(cd "$W" && git status --porcelain --untracked-files=all | sort | md5sum)
[ "$snap_before" = "$snap_after" ] && ok "(v) install changed nothing in the working tree" || bad "(v) install dirtied the working tree"
[ -z "$(git -C "$W" config --get core.hooksPath)" ] && ok "hooksPath unset" || bad "hooksPath still set"
[ "$(cat "$HOOKS/lumen-branch-guard.hooksPath.was")" = ".githooks" ] && ok "previous hooksPath recorded" || bad "was-file missing"
expect_rc 0 "$GUARD" status "$W"

echo "== (i) direct commit on main refused =="
echo x > "$W/f1"; git -C "$W" add f1
expect_rc 1 git -C "$W" commit -q -m "feat: direct"; expect_grep "REFUSED commit straight to 'main'"; expect_grep "lumen-pr start"; expect_nogrep "OLD-TRACKED-PRECOMMIT-RAN"
[ "$(git -C "$W" rev-list --count main)" = 1 ] && ok "main unchanged (1 commit)" || bad "main gained a commit"
git -C "$W" diff --cached --quiet && bad "staged work lost" || ok "staged work preserved"

echo "== (ii) feature branch commit + push pass; old tracked pre-commit still chained =="
git -C "$W" switch -q -c fix/thing
expect_rc 0 git -C "$W" commit -m "feat: on branch"; expect_grep "OLD-TRACKED-PRECOMMIT-RAN"
expect_rc 0 git -C "$W" push -q -u origin fix/thing

echo "== (vi) feature->main push, main delete refused; tag push allowed =="
expect_rc 1 git -C "$W" push -q origin fix/thing:main; expect_grep "REFUSED push straight to 'main'"
expect_rc 1 git -C "$W" push -q origin :main; expect_grep "DELETE of main"
git -C "$W" tag -a v0-test -m t; expect_rc 0 git -C "$W" push -q origin v0-test
[ "$(git -C "$T/origin.git" rev-parse main)" = "$(git -C "$W" rev-parse main)" ] && ok "origin main untouched" || bad "origin main moved"

echo "== (viii) linked worktree on an old branch is guarded by the common dispatcher =="
echo w > "$T/oldwt/w"; git -C "$T/oldwt" add w
expect_rc 0 git -C "$T/oldwt" commit -q -m "feat: old wt"         # feature branch: allowed
expect_rc 1 git -C "$T/oldwt" push -q origin feat/old:main; expect_grep "REFUSED push straight to 'main'"

echo "== (ix) tracked + dispatcher present: guard runs once, chained tracked hook still runs =="
git -C "$W" checkout -q feat/hooks; echo h > "$W/h"; git -C "$W" add h
out=$(git -C "$W" commit -m "feat: with tracked hooks" 2>&1); rc=$?
[ "$rc" = 0 ] && ok "commit on feat/hooks rc=0" || { bad "commit on feat/hooks rc=$rc"; printf '%s\n' "$out"; }
git -C "$W" checkout -q main; git -C "$W" merge -q --no-ff --no-verify feat/hooks -m "merge hooks" 2>/dev/null
echo m > "$W/m"; git -C "$W" add m
out=$(git -C "$W" commit -q -m "feat: main with tracked hooks" 2>&1); rc=$?
n=$(printf '%s\n' "$out" | grep -c "REFUSED commit straight to 'main'")
[ "$rc" = 1 ] && [ "$n" = 1 ] && ok "refused exactly once (dispatcher ran, tracked copy skipped) n=$n" || { bad "expected rc=1 and one refusal, got rc=$rc n=$n"; printf '%s\n' "$out"; }
git -C "$W" reset -q --hard origin/main 2>/dev/null; git -C "$W" reset -q --hard "$(git -C "$W" rev-list --max-parents=0 main)"   # back to the 1-commit main

echo "== (iii) --no-verify bypass lands locally and is caught after the fact =="
echo y > "$W/f2"; git -C "$W" add f2
expect_rc 0 git -C "$W" commit -q --no-verify -m "feat: bypass"
[ "$(git -C "$W" rev-list --count origin/main..main)" = 1 ] && ok "main is 1 ahead of origin (the drift the sweeps look for)" || bad "expected 1 ahead"
if [ -f "$RECONCILE" ]; then
  out=$(python3 "$RECONCILE" status --repo "$W" --no-fetch 2>&1); rc=$?
  printf '%s' "$out" | grep -qE 'IN_SYNC' && bad "repo_reconcile reported IN_SYNC after bypass" || ok "repo_reconcile status sees drift (rc=$rc): $(printf '%s' "$out" | head -1)"
fi
if [ -f "$SWEEP" ]; then
  out=$(python3 "$SWEEP" --dry-run --repo "$W" 2>&1); rc=$?
  printf '%s' "$out" | grep -q 'would file' && ok "repo_sweep dry-run would file a ticket: $(printf '%s' "$out" | head -1)" || { bad "repo_sweep dry-run did not flag it (rc=$rc)"; printf '%s\n' "$out" | sed 's/^/    /'; }
fi
expect_rc 0 git -C "$W" push -q --no-verify origin main
ok "push --no-verify lands (documented limit: no server-side protection; sweeps are the backstop)"

echo "== (iv) idempotent re-install =="
n_bak_before=$(ls "$T/backups" 2>/dev/null | wc -l)
expect_rc 0 "$GUARD" install "$W"; expect_grep "pre-commit: current"; expect_grep "pre-push: current"; expect_grep "branch-guard: current"; expect_grep "core.hooksPath: unset (current)"
n_bak_after=$(ls "$T/backups" 2>/dev/null | wc -l)
[ "$n_bak_before" = "$n_bak_after" ] && ok "no new backups on re-install ($n_bak_after)" || bad "re-install created backups"
[ "$(grep -c 'lumen-bin branch-guard dispatcher' "$HOOKS/pre-commit")" = 1 ] && ok "one dispatcher header in common pre-commit" || bad "dispatcher duplicated"

echo "== (vii) detached HEAD commit is allowed =="
git -C "$W" checkout -q --detach; echo z > "$W/f3"; git -C "$W" add f3
expect_rc 0 git -C "$W" commit -q -m "chore: detached"; git -C "$W" checkout -q main

echo "== (x) uninstall reverses =="
expect_rc 0 "$GUARD" uninstall "$W"; expect_grep "core.hooksPath: restored to '.githooks'"
[ "$(git -C "$W" config --get core.hooksPath)" = ".githooks" ] && ok "hooksPath restored" || bad "hooksPath not restored"
[ ! -f "$HOOKS/pre-commit" ] && [ ! -f "$HOOKS/pre-push" ] && [ ! -f "$HOOKS/branch-guard" ] && ok "dispatchers removed" || bad "dispatcher files remain"
expect_rc 1 "$GUARD" status "$W"; expect_grep "NOT GUARDED"
echo q > "$W/f4"; git -C "$W" add f4
expect_rc 0 git -C "$W" commit -q -m "feat: unguarded main commit (old tracked hook only)"

echo; echo "RESULT: $pass passed, $fail failed"; [ "$fail" = 0 ]
