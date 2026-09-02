"""wq-lane-init lane staging — kb/ISSUE-LOG.md #23 (wq#1282).

Opening a lane used to edit the TRACKED dispatcher/config.json in place. That dirtied the
primary foreman tree, so lumen-pr's target-tree guard (wq#771/wq#792) false-blocked every
subsequent land and workers reached for LUMEN_PR_ALLOW_DIRTY=1 — and the edit never
dispatched anything anyway, because foreman_dispatcher.load_config() reads config.json at
HEAD (wq#772).

A lane is now STAGED into dispatcher/runtime/lane_state.json, which is gitignored. Staged
is not live: it takes a commit into config.json to become policy, and
dispatch_config_lint.py reports the gap until then.
"""
import json
import os
import subprocess

BIN_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LANE_INIT = os.environ.get("WQ_LANE_INIT_BIN", os.path.join(BIN_DIR, "wq-lane-init"))


def git(repo, *args, check=True):
    return subprocess.run(["git", "-C", str(repo), *args], check=check,
                          capture_output=True, text=True)


def make_foreman(tmp_path, committed_lanes):
    """A repo shaped like foreman/: config.json committed, runtime/ gitignored."""
    repo = tmp_path / "foreman"
    (repo / "dispatcher").mkdir(parents=True)
    git(repo, "init", "-q")
    git(repo, "config", "user.email", "test@test")
    git(repo, "config", "user.name", "test")
    (repo / ".gitignore").write_text("runtime/\n")
    config = repo / "dispatcher" / "config.json"
    config.write_text(json.dumps(
        {"lanes": {ln: {"enabled": True, "max_per_day": 5} for ln in committed_lanes}},
        indent=2) + "\n")
    git(repo, "add", ".gitignore", "dispatcher/config.json")
    git(repo, "commit", "-qm", "init")
    assert git(repo, "status", "--porcelain").stdout == ""
    return repo, config


def run_lane_init(tmp_path, config, lane):
    """Run the real script. It registers the lane FIRST (wq#876), then scaffolds from
    $WQDIR's template — which this fixture deliberately does not provide, so the run
    stops right after the part under test without writing into the live work-queue."""
    project = tmp_path / "project"
    project.mkdir(exist_ok=True)
    wqdir = tmp_path / "wq"
    wqdir.mkdir(exist_ok=True)
    env = dict(os.environ,
               WQ_LANE_INIT_WQDIR=str(wqdir),
               FOREMAN_DISPATCHER_CONFIG=str(config))
    return subprocess.run(["bash", LANE_INIT, lane, str(project), "a test lane"],
                          capture_output=True, text=True, env=env)


def lane_state(config):
    return os.path.join(os.path.dirname(str(config)), "runtime", "lane_state.json")


def test_new_lane_is_staged_under_runtime_and_leaves_the_tree_clean(tmp_path):
    repo, config = make_foreman(tmp_path, ["build"])
    before = config.read_text()

    run_lane_init(tmp_path, config, "nest")

    staged = json.load(open(lane_state(config)))
    assert staged["lanes"]["nest"]["enabled"] is False
    assert "commit into config.json" in staged["lanes"]["nest"]["note"]
    assert config.read_text() == before, "config.json must not be edited in place"
    assert git(repo, "status", "--porcelain").stdout == "", \
        "staging a lane must not dirty the tracked tree (the wq#771 false-block)"


def test_a_lane_already_committed_is_reported_live_not_restaged(tmp_path):
    _repo, config = make_foreman(tmp_path, ["build", "nest"])

    run_lane_init(tmp_path, config, "nest")

    assert not os.path.exists(lane_state(config))


def test_staging_the_same_lane_twice_is_idempotent(tmp_path):
    repo, config = make_foreman(tmp_path, ["build"])

    run_lane_init(tmp_path, config, "nest")
    first = json.load(open(lane_state(config)))
    run_lane_init(tmp_path, config, "nest")
    second = json.load(open(lane_state(config)))

    assert first == second
    assert git(repo, "status", "--porcelain").stdout == ""


def test_staging_a_second_lane_keeps_the_first(tmp_path):
    _repo, config = make_foreman(tmp_path, ["build"])

    run_lane_init(tmp_path, config, "nest")
    run_lane_init(tmp_path, config, "mailbot")

    assert set(json.load(open(lane_state(config)))["lanes"]) == {"nest", "mailbot"}
