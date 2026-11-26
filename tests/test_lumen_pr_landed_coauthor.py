"""wq#5342: the commit `lumen-pr land` puts on main must carry no Co-authored-by trailer.

Gitea appended "Co-authored-by: Lumen Industries <lumen@lumennode.uk>" to every squash land
(ADD_CO_COMMITTER_TRAILERS, default true, fires when the merging user's signature differs from
the squash author). That happened AFTER the explicit merge_message_field, so lint_no_coauthor
(which reads only the text lumen-pr writes) never saw it. The forge fix is the Anubis app.ini
setting. This file tests the lumen-pr tripwire that would catch the trailer coming back:

  * `lumen-pr landcheck` on its own (the function cmd_land calls), against a bare origin;
  * the REAL `lumen-pr land`, end to end, with a fake token-engine client whose pr-merge
    performs the squash into the bare origin the way Gitea does, with and without the
    trailer. The clean case asserts the ticket's liveness check verbatim:
        git log -1 --format=%B origin/main | grep -ci '^co-authored-by:'  ->  0

LUMEN_PR_BIN overrides the script under test; default is the copy next to this tests/ dir.
"""
import os
import subprocess
import textwrap
from pathlib import Path

import pytest

LUMEN_PR = os.environ.get("LUMEN_PR_BIN", str(Path(__file__).resolve().parents[1] / "lumen-pr"))
BASE_ENV = {
    **{k: v for k, v in os.environ.items()
       if not k.startswith(("GIT_", "LUMEN_PR_", "WQ_", "GITEA_"))},
    "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
    "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t",
    "GIT_CONFIG_GLOBAL": "/dev/null", "GIT_CONFIG_SYSTEM": "/dev/null",
    "LUMEN_PR_SCOPE_CHECK": "0",
}
FOOTERS = ("Refs: wq#5342\n"
           "Assisted-by: Claude (Anthropic) via Lumen Industries automation\n"
           "Signed-off-by: Lumen Industries <lumen@lumennode.uk>")
GITEA_TRAILER = "Co-authored-by: Lumen Industries <lumen@lumennode.uk>"
LIVENESS = "git log -1 --format=%B origin/main | grep -ci '^co-authored-by:'"


def git(tree, *args, env=None):
    return subprocess.run(["git", "-C", str(tree), *args], env=env or BASE_ENV,
                          capture_output=True, text=True, check=True).stdout.strip()


def commit(tree, name, text, msg):
    (Path(tree) / name).write_text(text)
    git(tree, "add", name)
    git(tree, "commit", "-q", "-m", msg)
    return git(tree, "rev-parse", "HEAD")


def liveness(repo):
    p = subprocess.run(["bash", "-c", LIVENESS], cwd=str(repo), env=BASE_ENV,
                       capture_output=True, text=True)
    return p.stdout.strip()


@pytest.fixture
def env(tmp_path):
    """bare origin lumen/repo.git on main; `seed` pushes to main; `repo` is the primary clone."""
    origin = tmp_path / "srv" / "lumen" / "repo.git"
    origin.parent.mkdir(parents=True)
    subprocess.run(["git", "init", "-q", "--bare", "-b", "main", str(origin)], env=BASE_ENV, check=True)
    seed = tmp_path / "seed"
    subprocess.run(["git", "clone", "-q", str(origin), str(seed)], env=BASE_ENV, check=True,
                   capture_output=True)
    commit(seed, "f.txt", "v1\n", "chore: seed")
    git(seed, "push", "-q", "origin", "main")
    repo = tmp_path / "repo"
    subprocess.run(["git", "clone", "-q", str(origin), str(repo)], env=BASE_ENV, check=True)
    return {"tmp": tmp_path, "origin": origin, "seed": seed, "repo": repo}


def push_landed(env, body):
    """what a forge squash would leave on origin/main: one commit with <body> as its message."""
    git(env["seed"], "pull", "-q", "--ff-only", "origin", "main")
    sha = commit(env["seed"], "landed.txt", body, body)
    git(env["seed"], "push", "-q", "origin", "main")
    return sha


def landcheck(env, *extra):
    p = subprocess.run([LUMEN_PR, "landcheck", str(env["repo"]), *extra],
                       capture_output=True, text=True, env=BASE_ENV)
    return p.returncode, p.stdout + p.stderr


# --- landcheck: the function cmd_land calls -------------------------------------------------

def test_clean_landed_commit_passes(env):
    push_landed(env, f"fix(x): clean squash\n\n{FOOTERS}\n")
    rc, out = landcheck(env)
    assert rc == 0, out
    assert "carries no Co-authored-by trailer" in out
    assert liveness(env["repo"]) == "0"


def test_gitea_default_trailer_is_caught(env):
    sha = push_landed(env, f"fix(x): squash with gitea trailer\n\n{FOOTERS}\n{GITEA_TRAILER}\n")
    rc, out = landcheck(env)
    assert rc == 1, out
    assert f"LANDED commit {sha[:10]}" in out
    assert GITEA_TRAILER in out
    assert "ADD_CO_COMMITTER_TRAILERS" in out and "force-push" in out
    assert liveness(env["repo"]) == "1"


@pytest.mark.parametrize("line", ["Co-Authored-By: Someone <s@s>", "co-authored-by: x <x@x>"])
def test_trailer_match_is_case_insensitive(env, line):
    push_landed(env, f"fix(x): casing\n\n{FOOTERS}\n{line}\n")
    rc, out = landcheck(env)
    assert rc == 1 and "LANDED commit" in out and line in out, out


def test_mid_line_mention_is_not_a_trailer(env):
    push_landed(env, f"docs(x): explain the ban on the Co-authored-by: trailer\n\n{FOOTERS}\n")
    assert landcheck(env)[0] == 0


def test_explicit_rev_and_no_fetch(env):
    bad = push_landed(env, f"fix(x): bad\n\n{FOOTERS}\n{GITEA_TRAILER}\n")
    push_landed(env, f"fix(x): good on top\n\n{FOOTERS}\n")
    git(env["repo"], "fetch", "-q", "origin")
    assert landcheck(env, "--no-fetch")[0] == 0          # tip is clean
    assert landcheck(env, bad, "--no-fetch")[0] == 1      # the named commit is not


def test_unresolvable_rev_is_rc2_not_a_pass(env):
    rc, out = landcheck(env, "no-such-ref", "--no-fetch")
    assert rc == 2 and "did not run" in out


# --- the real `lumen-pr land`, end to end, against a fake broker -----------------------------
# The fake te_client answers the four verbs land uses. Its pr-merge does what Gitea's squash
# does: squash the head branch onto main with title + footers as the message, author
# "Lumen Industries", committer "lumen", and (FAKE_GITEA_TRAILER=1, Gitea's default before
# wq#5342) the Co-authored-by line Gitea appends because those two signatures differ.
FAKE_TE = textwrap.dedent(r'''
    import json, os, subprocess, sys, tempfile
    a = sys.argv[1:]
    verb = a[a.index("--verb") + 1]
    rest = a[a.index("--argv") + 2:]
    origin, ref, title = os.environ["FAKE_ORIGIN"], os.environ["FAKE_HEAD_REF"], os.environ["FAKE_TITLE"]
    state = os.environ["FAKE_STATE"]
    def g(*args, cwd=None, env=None):
        return subprocess.run(["git", *args], cwd=cwd, env=env, check=True,
                              capture_output=True, text=True).stdout.strip()
    def head():
        out = g("ls-remote", origin, "refs/heads/" + ref)
        return out.split()[0] if out else ""
    merged = os.path.exists(state)
    if verb == "pr-view":
        res = {"state": "closed" if merged else "open", "merged": merged, "draft": False,
               "head_sha": open(state).read().strip() if merged else head(),
               "head_ref": ref, "title": title, "mergeable": True, "mergeable_state": "clean"}
    elif verb == "pr-checks":
        res = {"total": 0, "pending": 0, "failed": 0, "cancelled": 0}
    elif verb == "pr-files":
        res = {"files": []}
    elif verb == "pr-merge":
        sha = head()
        w = tempfile.mkdtemp()
        g("clone", "-q", origin, w)
        g("merge", "--squash", "origin/" + ref, cwd=w)
        msg = title + "\n\n" + os.environ["FAKE_FOOTERS"]
        if os.environ.get("FAKE_GITEA_TRAILER") == "1":
            msg += "\nCo-authored-by: Lumen Industries <lumen@lumennode.uk>"
        e = dict(os.environ, GIT_AUTHOR_NAME="Lumen Industries", GIT_AUTHOR_EMAIL="lumen@lumennode.uk",
                 GIT_COMMITTER_NAME="lumen", GIT_COMMITTER_EMAIL="lumen@lumennode.uk")
        g("commit", "-q", "-m", msg, cwd=w, env=e)
        g("push", "-q", "origin", "HEAD:main", cwd=w)
        g("push", "-q", "origin", "--delete", ref, cwd=w)
        open(state, "w").write(sha)
        res = {"merged": True, "sha": sha, "head_ref": ref, "branch_deleted": True, "already": False}
    else:
        print(json.dumps({"rule": "deny", "reason": "fake: unexpected verb " + verb})); sys.exit(1)
    print(json.dumps({"rule": "allow", "reason": "", "result": res}))
''')
FAKE_SLOCK = "#!/bin/bash\ncase \"$1\" in who) echo '(no active locks)' ;; esac\nexit 0\n"
BRANCH, TITLE = "fix/landed-trailer", "fix(x): landed trailer probe"


def land(env, trailer):
    t, repo = env["tmp"], env["repo"]
    te = t / "fake_te.py"
    te.write_text(FAKE_TE)
    slock = t / "fake-slock"
    slock.write_text(FAKE_SLOCK)
    slock.chmod(0o755)
    (t / "grants").mkdir(exist_ok=True)
    (t / "suites.conf").write_text("# none\n")
    git(repo, "checkout", "-q", "-b", BRANCH)
    commit(repo, "x.txt", "x\n", f"{TITLE}\n\n{FOOTERS}")
    git(repo, "push", "-q", "origin", BRANCH)
    e = {**BASE_ENV,
         "LUMEN_PR_TE_CLIENT": str(te), "LUMEN_PR_TE_GRANT_DIR": str(t / "grants"),
         "LUMEN_PR_SLOCK": str(slock), "LUMEN_PR_SUITES_CONF": str(t / "suites.conf"),
         "LUMEN_PR_SUITE_STATE": str(t / "suite-state"), "LUMEN_PR_WORKTREE_ROOT": str(t / "wt"),
         "FAKE_ORIGIN": str(env["origin"]), "FAKE_HEAD_REF": BRANCH, "FAKE_TITLE": TITLE,
         "FAKE_FOOTERS": FOOTERS, "FAKE_STATE": str(t / "merged.state"),
         "FAKE_GITEA_TRAILER": "1" if trailer else "0"}
    p = subprocess.run([LUMEN_PR, "land", str(repo), "7"], capture_output=True, text=True,
                       env=e, timeout=120)
    return p.returncode, p.stdout + p.stderr


def test_land_clean_squash_passes_liveness_check(env):
    rc, out = land(env, trailer=False)
    assert rc == 0, out
    assert "merged PR #7" in out
    assert "carries no Co-authored-by trailer" in out
    assert liveness(env["repo"]) == "0", out          # the wq#5342 liveness check, verbatim
    body = git(env["repo"], "log", "-1", "--format=%B", "origin/main")
    assert body.startswith(TITLE) and "Assisted-by:" in body and "Signed-off-by:" in body


def test_land_fails_loudly_when_forge_appends_trailer(env):
    rc, out = land(env, trailer=True)
    assert rc != 0, out
    assert "merged PR #7" in out                      # the merge happened...
    assert "IS MERGED" in out and "Do not re-land" in out
    assert "LANDED commit" in out and GITEA_TRAILER in out
    # ...and land still finished its local cleanup before refusing
    assert git(env["repo"], "branch", "--show-current") == "main"
    assert git(env["repo"], "rev-parse", "HEAD") == git(env["repo"], "rev-parse", "origin/main")
    assert liveness(env["repo"]) == "1"
