"""wq#2139 (wq#2127) — `wq add` / `wq edit --lane` refuse a lane that is enabled for dispatch
but has no LANE_REPO row in the foreman router, unless the ticket carries a needs repo(...) tag.

Same subprocess-against-a-scratch-DB pattern as test_wq_needs.py. The foreman policy files are
scratch copies in a throwaway git repo, wired in through the WQ_* path overrides, so the real
foreman checkout is never read.
"""
import json
import os
import subprocess
import sys

BIN_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
WQ_BIN = os.environ.get("WQ_BIN", os.path.join(BIN_DIR, "wq"))
MIGRATION = os.path.join(BIN_DIR, "migrations", "wq1331_needs.py")

CONFIG = {"lanes": {
    "build": {"enabled": True, "max_per_day": 5},
    "auto": {"enabled": True, "max_per_day": 5},
    "simpill": {"enabled": True, "max_per_day": 5, "multi_repo": True},
    "docs": {"enabled": True, "max_per_day": 5},
    "user": {"enabled": True, "max_per_day": 5},
    "off": {"enabled": False},
}}
ROUTER = 'FOREMAN_ROOT = "/f"\nLANE_REPO = {\n    "build": FOREMAN_ROOT,\n    "pokemon": "/p",  # c\n}\n'
DISPATCHER = 'REPO_HINT_LANE = "auto"\nHUMAN_LANES = {"user", "frank-re"}\n'
REPO_SWEEP = 'EXTRA_REPOS = [\n    "/home/lumen/bin",\n]\n'
# managed repos in this scratch policy: LANE_REPO values ("/f" via FOREMAN_ROOT, "/p") + EXTRA_REPOS
SWEEP_TITLE = "[repo-sweep] /f diverged"
SWEEP_DETAIL = "Automated sweep detected DIVERGED in /f.\nAuto-escalated by aq_escalate."


def git(repo, *args):
    subprocess.run(["git", "-C", str(repo), "-c", "user.name=t", "-c", "user.email=t@t", *args],
                   check=True, capture_output=True)


def make_foreman(tmp_path):
    repo = tmp_path / "foreman"
    (repo / "dispatcher").mkdir(parents=True)
    (repo / "orchestrator").mkdir()
    (repo / "harness").mkdir()
    (repo / "harness" / "repo_sweep.py").write_text(REPO_SWEEP)
    (repo / "dispatcher" / "config.json").write_text(json.dumps(CONFIG))
    (repo / "dispatcher" / "foreman_dispatcher.py").write_text(DISPATCHER)
    (repo / "orchestrator" / "router.py").write_text(ROUTER)
    git(repo, "init", "-q")
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "policy")
    return repo


def make_env(tmp_path, repo=None):
    repo = repo or make_foreman(tmp_path)
    env = dict(os.environ)
    env["WQ_DB"] = str(tmp_path / "scratch_wq.db")
    env["WQ_AGENT"] = "test-lane-repo-agent"
    env["WQ_LANE_REPO_GUARD"] = "refuse"   # the refusal path is what this suite pins; default is warn
    env["WQ_DISPATCHER_CONFIG"] = str(repo / "dispatcher" / "config.json")
    env["WQ_FOREMAN_ROUTER"] = str(repo / "orchestrator" / "router.py")
    env["WQ_FOREMAN_DISPATCHER"] = str(repo / "dispatcher" / "foreman_dispatcher.py")
    env["WQ_FOREMAN_REPO_SWEEP"] = str(repo / "harness" / "repo_sweep.py")
    return env


def wq(env, *args):
    return subprocess.run([WQ_BIN] + list(args), capture_output=True, text=True, env=env)


def migrate(env):
    subprocess.run([sys.executable, MIGRATION, "--db", env["WQ_DB"], "--yes"],
                   check=True, capture_output=True)


def add(env, lane, title, *extra):
    return wq(env, "add", "--lane", lane, "--title", title, *extra)


def show(env, tid):
    return json.loads(wq(env, "show", str(tid), "--json").stdout)


def tid_of(p):
    return int(p.stdout.split()[2])


def test_unmapped_enabled_lane_refused(tmp_path):
    env = make_env(tmp_path)
    for lane in ("simpill", "auto"):
        p = add(env, lane, f"lands nowhere {lane}")
        assert p.returncode == 1, p.stdout + p.stderr
        assert "REFUSED" in p.stdout and repr(lane) in p.stdout and "LANE_REPO" in p.stdout
        assert "pick a mapped lane" in p.stdout and "repo(" in p.stdout
    assert "added task" not in wq(env, "list").stdout


def test_needs_repo_tag_accepted(tmp_path):
    env = make_env(tmp_path)
    wq(env, "list")
    migrate(env)
    p = add(env, "auto", "tagged", "--needs", "repo(/p)")
    assert p.returncode == 0, p.stdout + p.stderr
    p2 = add(env, "auto", "tagged in detail", "--detail", "body\nNeeds: repo(/home/lumen/bin)")
    assert p2.returncode == 0, p2.stdout + p2.stderr
    p3 = add(env, "auto", "wrong tag", "--needs", "dir(/p)")
    assert p3.returncode == 1 and "REFUSED" in p3.stdout


def test_repo_sweep_shape_accepted_into_auto(tmp_path):
    """wq#2144: aq_escalate files repo-sweep escalations as `wq add --lane auto` with no --needs;
    the dispatcher resolves them through repo_sweep.repo_hint, so the guard must let them in."""
    env = make_env(tmp_path)
    p = add(env, "auto", SWEEP_TITLE, "--detail", SWEEP_DETAIL)
    assert p.returncode == 0, p.stdout + p.stderr
    # the same shape with a repo repo_sweep does not manage is not resolvable, so still refused
    for title, detail in ((SWEEP_TITLE + " 2", SWEEP_DETAIL.replace("/f.", "/elsewhere.")),
                          ("plain title", SWEEP_DETAIL),
                          (SWEEP_TITLE + " 3", "Some other prose.\n" + SWEEP_DETAIL)):
        p = add(env, "auto", title, "--detail", detail)
        assert p.returncode == 1 and "REFUSED" in p.stdout, (title, p.stdout + p.stderr)


def test_needs_tag_does_not_rescue_unmapped_non_auto_lane(tmp_path):
    """wq#2144: the dispatcher skips lane_unmapped for every lane but 'auto' (wq#2652: or a lane
    flagged multi_repo) before it reads the needs tag, so a tag (even a managed path) or the
    repo-sweep shape must not admit a plain unmapped lane like 'docs'."""
    env = make_env(tmp_path)
    wq(env, "list")
    migrate(env)
    for extra in (("--needs", "repo(/p)"), ("--detail", "body\nNeeds: repo(/f)"),
                  ("--detail", SWEEP_DETAIL)):
        p = add(env, "docs", SWEEP_TITLE + " s", *extra)
        assert p.returncode == 1, (extra, p.stdout + p.stderr)
        assert "REFUSED" in p.stdout and "LANE_REPO" in p.stdout and "'docs'" in p.stdout
    assert "does NOT rescue" in p.stdout


def test_multi_repo_lane_rescued_by_managed_needs_tag(tmp_path):
    """wq#2652: a lane flagged multi_repo in dispatcher/config.json (e.g. simpill) is rescued the
    same way the hint lane 'auto' is (wq#2144) -- a needs repo(...) tag naming a managed repo, or
    the repo-sweep title/detail shape -- mirrors test_needs_repo_tag_accepted but for lane simpill
    instead of auto."""
    env = make_env(tmp_path)
    wq(env, "list")
    migrate(env)
    p = add(env, "simpill", "tagged", "--needs", "repo(/p)")
    assert p.returncode == 0, p.stdout + p.stderr
    p2 = add(env, "simpill", "tagged in detail", "--detail", "body\nNeeds: repo(/home/lumen/bin)")
    assert p2.returncode == 0, p2.stdout + p2.stderr
    p3 = add(env, "simpill", SWEEP_TITLE + " s", "--detail", SWEEP_DETAIL)
    assert p3.returncode == 0, p3.stdout + p3.stderr
    p4 = add(env, "simpill", "wrong tag", "--needs", "dir(/p)")
    assert p4.returncode == 1 and "REFUSED" in p4.stdout


def test_multi_repo_lane_unmanaged_tag_still_refused(tmp_path):
    """wq#2652: the multi_repo rescue is scoped to a real managed repo, not a blanket lane
    exemption -- same as test_unmanaged_needs_path_refused_into_auto, but for simpill."""
    env = make_env(tmp_path)
    wq(env, "list")
    migrate(env)
    p = add(env, "simpill", "unmanaged tag", "--needs", "repo(/home/lumen/questfree)")
    assert p.returncode == 1 and "REFUSED" in p.stdout, p.stdout + p.stderr
    assert "managed_repos" in p.stdout


def test_unmanaged_needs_path_refused_into_auto(tmp_path):
    """wq#2144: router.needs_repo_hint ignores a path outside repo_sweep.managed_repos()."""
    env = make_env(tmp_path)
    wq(env, "list")
    migrate(env)
    p = add(env, "auto", "unmanaged tag", "--needs", "repo(/home/lumen/questfree)")
    assert p.returncode == 1 and "REFUSED" in p.stdout, p.stdout + p.stderr
    assert "managed_repos" in p.stdout


def test_mapped_disabled_and_human_lanes_accepted(tmp_path):
    env = make_env(tmp_path)
    for lane, extra in (("build", ()), ("off", ()), ("user", ("--no-probe", "physical task"))):
        p = add(env, lane, f"fine in {lane}", *extra)
        assert p.returncode == 0, (lane, p.stdout + p.stderr)


def test_unreadable_policy_fails_open_with_warning(tmp_path):
    env = make_env(tmp_path)
    env["WQ_DISPATCHER_CONFIG"] = str(tmp_path / "nope" / "config.json")
    p = add(env, "simpill", "config gone")
    assert p.returncode == 0, p.stdout + p.stderr
    assert "WARNING (wq#2139)" in p.stderr and "skipped" in p.stderr
    assert len([ln for ln in p.stderr.splitlines() if "wq#2139" in ln]) == 1

    (tmp_path / "b").mkdir()
    env2 = make_env(tmp_path / "b")
    (tmp_path / "b" / "foreman" / "orchestrator" / "router.py").write_text("LANE_REPO = (\n")
    git(tmp_path / "b" / "foreman", "commit", "-qam", "break")
    p2 = add(env2, "simpill", "router unparseable")
    assert p2.returncode == 0 and "WARNING (wq#2139)" in p2.stderr


def test_policy_read_from_git_head_not_working_tree(tmp_path):
    env = make_env(tmp_path)
    router = tmp_path / "foreman" / "orchestrator" / "router.py"
    router.write_text(ROUTER.replace('"pokemon"', '"simpill": "/s", "pokemon"'))  # uncommitted
    p = add(env, "simpill", "uncommitted mapping does not count")
    assert p.returncode == 1 and "REFUSED" in p.stdout
    git(tmp_path / "foreman", "commit", "-qam", "map it")
    assert add(env, "simpill", "now committed").returncode == 0


def test_force_overrides_and_is_noted_on_ticket(tmp_path):
    env = make_env(tmp_path)
    p = add(env, "simpill", "forced in", "--force")
    assert p.returncode == 0 and "--force" in p.stderr
    notes = show(env, tid_of(p))["notes"]
    assert "--force wq#2139" in notes and "simpill" in notes
    ok = add(env, "build", "plain ticket, no note")
    assert not (show(env, tid_of(ok)).get("notes") or "")


def test_edit_lane_guarded(tmp_path):
    env = make_env(tmp_path)
    wq(env, "list")
    migrate(env)
    tid = tid_of(add(env, "build", "movable"))
    p = wq(env, "edit", str(tid), "--lane", "docs")
    assert p.returncode == 1 and "REFUSED" in p.stdout
    assert show(env, tid)["lane"] == "build"
    # an existing managed repo tag rescues only the hint lane or a multi_repo lane (wq#2144/wq#2652),
    # never a plain unmapped lane like docs
    assert wq(env, "edit", str(tid), "--needs", "repo(/p)").returncode == 0
    assert wq(env, "edit", str(tid), "--lane", "docs").returncode == 1
    assert wq(env, "edit", str(tid), "--lane", "auto").returncode == 0
    # edits that do not touch the lane never trip it, even on an unmapped lane
    assert wq(env, "edit", str(tid), "--pri", "1").returncode == 0
    # the repo-sweep shape (title + detail already on the ticket) also satisfies an edit into auto
    ts = tid_of(add(env, "build", SWEEP_TITLE + " edit", "--detail", SWEEP_DETAIL))
    assert wq(env, "edit", str(ts), "--lane", "auto").returncode == 0
    # human lane exempt; --force on record
    t2 = tid_of(add(env, "build", "second"))
    assert wq(env, "edit", str(t2), "--lane", "user").returncode == 0
    t3 = tid_of(add(env, "build", "third"))
    p3 = wq(env, "edit", str(t3), "--lane", "auto", "--force")
    assert p3.returncode == 0
    assert "--force wq#2139" in show(env, t3)["notes"]


def test_default_mode_warns_and_files_the_ticket(tmp_path):
    """wq#2127: without WQ_LANE_REPO_GUARD=refuse an unmapped enabled lane is filed with a warning
    and a note, because the CLI default lane is one and unattended producers must not lose tickets."""
    env = make_env(tmp_path)
    env.pop("WQ_LANE_REPO_GUARD")
    wq(env, "list")
    migrate(env)
    p = add(env, "simpill", "default mode files it")
    assert p.returncode == 0, p.stdout + p.stderr
    assert "added task" in p.stdout
    assert "lane_unmapped" in p.stderr
