"""wq#3095 test-to-failure for bin-drift-rescue: throwaway repos + a stub lumen-pr, never touches the real tree."""
import os, re, stat, subprocess, textwrap, time
import pytest

TOOL = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "bin-drift-rescue")
GENV = {"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t", "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"}

def sh(*a, cwd=None, env=None, check=True):
    e = {**os.environ, **GENV, **(env or {})}
    r = subprocess.run(a, cwd=cwd, env=e, capture_output=True, text=True)
    if check and r.returncode:
        raise AssertionError(f"{a} rc={r.returncode}\n{r.stdout}\n{r.stderr}")
    return r

STUB_PR = textwrap.dedent('''\
    #!/bin/bash
    # stub of lumen-pr: same verbs, plain git against the bare origin
    set -e
    cmd=$1; shift
    case $cmd in
      start)  repo=$1; br=$2
              printf '%s' "$br" | grep -qE "$STUB_BRANCH_RE" || { echo "lumen-pr: branch name violates §2: got : $br" >&2; exit 1; }
              wt="$STUB_WT_ROOT/$(echo $br | tr / -)"; git -C $repo fetch -q origin
              git -C $repo worktree add -q -b $br $wt origin/main; echo "created worktree" >&2; echo $wt ;;
      commit) wt=$1; git -C $wt commit -q -m "$2" ;;
      open)   bf=""; while [ $# -gt 0 ]; do [ "$1" = --body-file ] && bf=$2; shift; done
              for h in "## What" "## Why" "## How" "## Testing" "## Risk / rollback"; do
                grep -qF "$h" "$bf" || { echo "lumen-pr: PR body is missing required section: $h" >&2; exit 1; }
              done; echo "opened PR #7" >&2 ;;
      land)   [ "${STUB_LAND_FAIL:-0}" = 1 ] && { echo "land refused" >&2; exit 1; }
              if [ "${STUB_LAND_ALTER:-0}" = 1 ]; then
                for f in $(git -C $1 diff-tree --no-commit-id --name-only -r HEAD); do echo altered >> $1/$f; done
                git -C $1 commit -qa --amend --no-edit
              fi
              git -C $1 push -q origin HEAD:main ;;
      ffprimary) [ "${STUB_FF_FAIL:-0}" = 1 ] && { echo "ffprimary refused" >&2; exit 1; }
              git -C $1 fetch -q origin; git -C $1 merge -q --ff-only origin/main ;;
    esac
''')
STUB_AQ = textwrap.dedent('''\
    #!/bin/bash
    echo "$@" >> "$STUB_AQ_LOG"
    if [ "$1" = list ]; then cat "$STUB_AQ_ROWS" 2>/dev/null; fi
''')

@pytest.fixture
def env(tmp_path):
    t = tmp_path
    # provenance repo (foreman) with one reviewed ops source
    sh("git", "init", "-q", "--bare", "-b", "main", str(t / "foreman.git"))
    sh("git", "clone", "-q", str(t / "foreman.git"), str(t / "foreman"))
    os.makedirs(t / "foreman/ops/wq1-x")
    (t / "foreman/ops/wq1-x/tool.py").write_text("print('reviewed v2')\n")
    (t / "foreman/ops/wq1-x/other.sh").write_text("echo reviewed other\n")
    sh("git", "add", "-A", cwd=t / "foreman"); sh("git", "commit", "-qm", "ops", cwd=t / "foreman")
    sh("git", "push", "-q", "origin", "HEAD:main", cwd=t / "foreman")
    # lumen-bin: bare origin + primary clone with two tracked files
    sh("git", "init", "-q", "--bare", "-b", "main", str(t / "bin.git"))
    sh("git", "clone", "-q", str(t / "bin.git"), str(t / "bin"))
    (t / "bin/mytool").write_text("print('old v1')\n"); (t / "bin/wq").write_text("wq\n")
    sh("git", "add", "-A", cwd=t / "bin"); sh("git", "commit", "-qm", "init", cwd=t / "bin")
    sh("git", "push", "-q", "origin", "HEAD:main", cwd=t / "bin")
    sh("git", "branch", "-q", "--set-upstream-to=origin/main", "main", cwd=t / "bin")
    (t / "wt").mkdir()
    for n, body in (("lumen-pr", STUB_PR), ("aq", STUB_AQ)):
        p = t / n; p.write_text(body); p.chmod(p.stat().st_mode | stat.S_IEXEC)
    e = {"BDR_REPO": str(t / "bin"), "BDR_SRC_REPO": str(t / "foreman"), "BDR_STATE": str(t / "state.json"),
         "BDR_BACKUP_ROOT": str(t / "backups"), "BDR_AUTO_FLAG": str(t / "auto.flag"), "BDR_LUMEN_PR": str(t / "lumen-pr"),
         "BDR_AQ": str(t / "aq"), "STUB_WT_ROOT": str(t / "wt"), "STUB_AQ_LOG": str(t / "aq.log"), "STUB_AQ_ROWS": str(t / "aq.rows"),
         "BDR_QUIET_S": "60", "BDR_ALERT_AFTER_S": "0", "STUB_BRANCH_RE": real_branch_re()}
    return t, e

def real_branch_re():
    """The branch lint of the real lumen-pr next to the tool, so the stub cannot drift from it (wq#3095 first live run failed on this)."""
    src = open(os.path.join(os.path.dirname(TOOL), "lumen-pr")).read()
    types = re.search(r"^CONV_TYPES='([^']+)'", src, re.M).group(1)
    tail = re.search(r'^BRANCH_RE="\^\(\$\{CONV_TYPES\}\)(.+)"$', src, re.M).group(1)
    return "^(" + types + ")" + tail

def age(path, secs=3600):
    old = time.time() - secs; os.utime(path, (old, old))

def run(e, *args, extra=None):
    return sh("python3", TOOL, *args, env={**e, **(extra or {})}, check=False)

def status(t):
    return sh("git", "status", "--porcelain", cwd=t / "bin").stdout.rstrip("\n")

def test_clean(env):
    t, e = env; r = run(e); assert r.returncode == 0 and "CLEAN" in r.stdout

def test_proven_modified_rescued(env):
    t, e = env
    (t / "bin/mytool").write_text("print('reviewed v2')\n"); age(t / "bin/mytool")  # same bytes as ops/wq1-x/tool.py, different name
    r = run(e); assert r.returncode == 1 and "PROVEN via ops/wq1-x/tool.py" in r.stdout, r.stdout
    r = run(e, "--apply"); assert "dry-run" in r.stdout and status(t) == " M mytool"
    r = run(e, "--apply", "--confirm"); assert r.returncode == 0 and "RESCUED" in r.stdout, r.stdout + r.stderr
    assert status(t) == "" and (t / "bin/mytool").read_text() == "print('reviewed v2')\n"
    assert "reviewed v2" in sh("git", "show", "origin/main:mytool", cwd=t / "bin").stdout
    assert list((t / "backups").glob("*/mytool"))
    assert run(e).returncode == 0  # idempotent

def test_proven_untracked_rescued(env):
    t, e = env
    (t / "bin/newtool").write_text("echo reviewed other\n"); age(t / "bin/newtool")
    r = run(e, "--apply", "--confirm"); assert r.returncode == 0 and "RESCUED" in r.stdout, r.stdout + r.stderr
    assert status(t) == "" and (t / "bin/newtool").read_text() == "echo reviewed other\n"

def test_unproven_never_touched(env):
    t, e = env
    (t / "bin/wq").write_text("hand edit no backing copy\n"); age(t / "bin/wq")
    r = run(e, "--aq"); assert r.returncode == 2 and "UNPROVEN" in r.stdout
    assert "bin-drift/wq" in (t / "aq.log").read_text() and "Do NOT discard" in (t / "aq.log").read_text()
    r = run(e, "--apply", "--confirm"); assert "nothing PROVEN" in r.stdout
    assert (t / "bin/wq").read_text() == "hand edit no backing copy\n" and status(t) == " M wq"

def test_mixed_rescues_only_proven(env):
    t, e = env
    (t / "bin/mytool").write_text("print('reviewed v2')\n"); (t / "bin/wq").write_text("hand edit\n")
    age(t / "bin/mytool"); age(t / "bin/wq")
    r = run(e, "--apply", "--confirm"); assert r.returncode == 0 and "RESCUED: mytool" in r.stdout, r.stdout + r.stderr
    assert (t / "bin/wq").read_text() == "hand edit\n" and status(t) == " M wq"

def test_active_writer_refused(env):
    t, e = env
    (t / "bin/mytool").write_text("print('reviewed v2')\n")  # fresh mtime
    r = run(e); assert r.returncode == 4
    r = run(e, "--apply", "--confirm"); assert r.returncode == 4 and "may still be active" in r.stdout
    assert (t / "bin/mytool").read_text() == "print('reviewed v2')\n" and not list((t / "backups").glob("*")) 

@pytest.mark.parametrize("fail", ["STUB_FF_FAIL", "STUB_LAND_FAIL"])
@pytest.mark.parametrize("kind", ["modified", "untracked"])
def test_failure_restores_byte_identical(env, fail, kind):
    t, e = env
    name = "mytool" if kind == "modified" else "newtool"
    body = "print('reviewed v2')\n" if kind == "modified" else "echo reviewed other\n"
    (t / "bin" / name).write_text(body); age(t / "bin" / name)
    r = run(e, "--apply", "--confirm", extra={fail: "1"}); assert r.returncode == 3, r.stdout + r.stderr
    assert (t / "bin" / name).read_text() == body            # the live file is exactly what it was
    assert "FAILED" in r.stderr and list((t / "backups").glob("*/" + name))

def test_unreadable_provenance_proves_nothing(env):
    t, e = env
    (t / "bin/mytool").write_text("print('reviewed v2')\n"); age(t / "bin/mytool")
    r = run(e, extra={"BDR_SRC_REF": "origin/does-not-exist"}); assert r.returncode == 2 and "nothing can be PROVEN" in r.stdout

def test_staged_and_deleted_are_other(env):
    t, e = env
    (t / "bin/wq").write_text("staged edit\n"); sh("git", "add", "wq", cwd=t / "bin"); os.remove(t / "bin/mytool")
    r = run(e); assert r.returncode == 2 and "never auto-handled" in r.stdout
    r = run(e, "--apply", "--confirm"); assert "nothing PROVEN" in r.stdout

def test_aq_row_resolved_when_cleared(env):
    t, e = env
    (t / "aq.rows").write_text("[ 41] open     x2   bin-drift   bin-drift/gone-file   title  owner=unassigned 2026-09-29T00:00:00Z  m\\n".replace("\\n", "\n"))
    run(e, "--aq"); assert "resolve 41" in (t / "aq.log").read_text()

def test_auto_flag_gates_apply(env):
    t, e = env
    (t / "bin/mytool").write_text("print('reviewed v2')\n"); age(t / "bin/mytool")
    r = run(e, "--apply", "--auto"); assert "auto-apply disabled" in r.stdout and status(t) == " M mytool"
    (t / "auto.flag").write_text("1")
    r = run(e, "--apply", "--auto"); assert "RESCUED" in r.stdout and status(t) == ""

@pytest.mark.parametrize("kind", ["modified", "untracked"])
def test_landed_bytes_differ_leaves_live_file_alone(env, kind):
    t, e = env
    name = "mytool" if kind == "modified" else "newtool"
    body = "print('reviewed v2')\n" if kind == "modified" else "echo reviewed other\n"
    (t / "bin" / name).write_text(body); age(t / "bin" / name)
    r = run(e, "--apply", "--confirm", extra={"STUB_LAND_ALTER": "1"})
    assert r.returncode == 3 and "differs from the live file" in r.stderr, r.stdout + r.stderr
    assert (t / "bin" / name).read_text() == body


def test_stub_branch_lint_matches_real_lumen_pr():
    rx = re.compile(real_branch_re())
    assert rx.match("chore/bin-drift-rescue-20260929-043223")
    assert not rx.match("chore/bin-drift-rescue-20260929_043223")
