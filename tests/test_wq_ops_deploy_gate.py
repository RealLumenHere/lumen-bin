"""wq#1724 self-test: `wq done` must refuse to close over an ops/*/install.sh artifact that
never reached its target.

EVIDENCE (wq#1724): wq#1632 closed done 2026-09-10 citing foreman/ops/wq1632-*/install.sh —
the wq#1673 durability gate passed because the script was committed on main, but nothing ever
checked the script RAN. lumen_state.py never saw a write. This fixture reproduces that shape:
a repo with an ops/wqNNNN-*/install.sh landed on main, and asserts `wq done` refuses to close
over it (rc=9) unless --deployed names a live file whose bytes match one the artifact ships, or
the caller records --no-deploy-needed on the record. Extends the wq#1673 gate in cmd_done
(ops_deploy_check) rather than adding a parallel evidence path — same subprocess-driven pattern
as test_wq_evidence_durability.py.

wq#2813 mirrors wq#2762/wq#1734 (foreman/harness/acceptance.py's identical ops-deploy gate)
into this bin/wq copy of the same algorithm: (1) a --deployed path resolving inside the
artifact's own directory byte-matches itself and must be rejected (wq#1734's escalated-but-
never-landed bypass); (2) a real deploy must also carry --deploy-verify naming a premise
re-check against live state AND a dry-run/read-only probe against the real target, checked
structurally per wq#2630 ("a probe must be able to fail") so a bare non-empty string can't
satisfy it (wq#2713/2472/1688 each passed fixture-only tests and would have caused live
damage on deploy).
"""
import os
import subprocess

import pytest

WQ_BIN = os.environ.get("WQ_BIN") or os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "wq")


def make_env(tmp_path, agent="test-ops-deploy-gate-agent"):
    env = dict(os.environ)
    env["WQ_DB"] = str(tmp_path / "scratch_wq.db")
    env["WQ_AGENT"] = agent
    env["WQ_DISPATCHER_CONFIG"] = str(tmp_path / "no-such-dispatcher-config.json")
    return env


def wq(env, *args, cwd=None):
    return subprocess.run([WQ_BIN] + list(args), capture_output=True, text=True, env=env,
                          cwd=str(cwd) if cwd else None)


def git(cwd, *args):
    p = subprocess.run(["git", "-C", str(cwd)] + list(args), capture_output=True, text=True)
    assert p.returncode == 0, p.stdout + p.stderr
    return p


def open_ticket(env, title, extra=()):
    p = wq(env, "add", "--lane", "auto", "--title", title, *extra)
    assert p.returncode == 0, p.stdout + p.stderr
    tid = str(int(p.stdout.strip().split()[2]))
    claim = wq(env, "claim", "--id", tid)
    assert claim.returncode == 0, claim.stdout + claim.stderr
    return tid


def notes_of(env, tid):
    show = wq(env, "show", tid, "--json")
    assert show.returncode == 0, show.stdout + show.stderr
    import json
    return json.loads(show.stdout)["notes"] or ""


@pytest.fixture
def repo_with_ops_artifact(tmp_path):
    """A repo whose main carries foreman/ops/wq1632-fake-fix/{install.sh,lumen_state.py} —
    same shape as the real wq1632 artifact dir — plus a separate live target file the install
    script is supposed to deploy to."""
    repo = tmp_path / "repo"
    repo.mkdir()
    git(repo, "init", "-q", "-b", "main")
    git(repo, "config", "user.email", "test@example.invalid")
    git(repo, "config", "user.name", "wq1724 test")
    art_dir = repo / "foreman" / "ops" / "wq1632-fake-fix"
    art_dir.mkdir(parents=True)
    (art_dir / "install.sh").write_text("#!/bin/bash\ncp lumen_state.py /target/lumen_state.py\n")
    (art_dir / "lumen_state.py").write_text("# fixed lifecycle closure logic\n")
    git(repo, "add", "foreman")
    git(repo, "commit", "-qm", "seed ops artifact")
    target = tmp_path / "target"
    target.mkdir()
    return repo, art_dir, target


def test_ops_install_citation_without_deploy_refuses_rc9(tmp_path, repo_with_ops_artifact):
    """wq1632 replay: install.sh landed on main, notes cite it, nothing proves it ran."""
    env = make_env(tmp_path)
    repo, art_dir, _target = repo_with_ops_artifact
    tid = open_ticket(env, "wq1724 self-test: undeployed ops artifact")

    done = wq(env, "done", tid, "--notes", f"shipped {art_dir}/install.sh", cwd=repo)
    assert done.returncode == 9, (
        f"expected rc=9 (wq#1724 ops-deploy gate), got {done.returncode}: "
        f"{done.stdout}{done.stderr}")
    assert "wq#1724" in done.stdout + done.stderr
    assert notes_of(env, tid) == "", "a refused close must not write notes"


def test_ops_payload_citation_without_deploy_refuses_rc9(tmp_path, repo_with_ops_artifact):
    """wq#4641: citing a file the bundle ships, not its install.sh, is the same unproven
    deploy; the path branch used to match only install.sh and close it."""
    env = make_env(tmp_path)
    repo, art_dir, _target = repo_with_ops_artifact
    tid = open_ticket(env, "wq4641 self-test: undeployed ops payload citation")

    done = wq(env, "done", tid, "--notes", f"shipped {art_dir}/lumen_state.py", cwd=repo)
    assert done.returncode == 9, done.stdout + done.stderr
    assert f"{art_dir}/install.sh" in done.stdout + done.stderr


_DEPLOY_VERIFY = ("premise: re-checked lumen_state.py's lifecycle-closure bug is still "
                  "live on the target before deploying; "
                  "probe: dry-run diff of install.sh's cp against the live target showed "
                  "only the intended file changing")


def test_ops_install_citation_with_matching_deployed_target_closes(tmp_path, repo_with_ops_artifact):
    env = make_env(tmp_path)
    repo, art_dir, target = repo_with_ops_artifact
    live = target / "lumen_state.py"
    live.write_text("# fixed lifecycle closure logic\n")  # byte-identical to the shipped copy
    tid = open_ticket(env, "wq1724 self-test: deployed ops artifact")

    done = wq(env, "done", tid, "--notes", f"shipped {art_dir}/install.sh",
              "--deployed", str(live), "--deploy-verify", _DEPLOY_VERIFY, cwd=repo)
    assert done.returncode == 0, done.stdout + done.stderr
    notes = notes_of(env, tid)
    assert f"[DEPLOYED: {live}]" in notes
    assert f"[DEPLOY-VERIFY: {_DEPLOY_VERIFY}]" in notes


def test_ops_install_citation_with_self_referential_deployed_target_refuses(
        tmp_path, repo_with_ops_artifact):
    """wq#2762 (ISSUE-LOG #859, closes wq#1734): pointing --deployed at the artifact's own
    shipped payload byte-matches itself and would pass with zero real deployment."""
    env = make_env(tmp_path)
    repo, art_dir, _target = repo_with_ops_artifact
    self_target = art_dir / "lumen_state.py"
    tid = open_ticket(env, "wq1734 self-test: self-referential deployed target")

    done = wq(env, "done", tid, "--notes", f"shipped {art_dir}/install.sh",
              "--deployed", str(self_target), "--deploy-verify", _DEPLOY_VERIFY, cwd=repo)
    assert done.returncode == 9, done.stdout + done.stderr
    assert "resolves inside" in done.stdout + done.stderr


def test_ops_install_citation_with_hardlinked_self_target_refuses(tmp_path, repo_with_ops_artifact):
    """wq#2813: a hard link outside the artifact dir to its own shipped payload survives the
    realpath check (a hard link has no single real path) and is still zero real deployment."""
    env = make_env(tmp_path)
    repo, art_dir, target = repo_with_ops_artifact
    linked = target / "lumen_state.py"
    if linked.exists():
        linked.unlink()
    os.link(art_dir / "lumen_state.py", linked)
    tid = open_ticket(env, "wq2813 self-test: hard-linked self target")

    done = wq(env, "done", tid, "--notes", f"shipped {art_dir}/install.sh",
              "--deployed", str(linked), "--deploy-verify", _DEPLOY_VERIFY, cwd=repo)
    assert done.returncode == 9, done.stdout + done.stderr
    assert "hard link" in done.stdout + done.stderr
    assert notes_of(env, tid) == "", "a refused close must not write notes"


def test_ops_install_citation_with_matching_deployed_target_but_no_deploy_verify_refuses(
        tmp_path, repo_with_ops_artifact):
    """wq#2762 (ISSUE-LOG #859): a byte-match alone proves delivery, not that the ticket's
    premise still held or that the install was tried read-only first (wq#2713, wq#2472)."""
    env = make_env(tmp_path)
    repo, art_dir, target = repo_with_ops_artifact
    live = target / "lumen_state.py"
    live.write_text("# fixed lifecycle closure logic\n")
    tid = open_ticket(env, "wq2762 self-test: deployed with no deploy-verify")

    done = wq(env, "done", tid, "--notes", f"shipped {art_dir}/install.sh",
              "--deployed", str(live), cwd=repo)
    assert done.returncode == 9, done.stdout + done.stderr
    assert "deploy-verify" in (done.stdout + done.stderr).lower()
    assert notes_of(env, tid) == "", "a refused close must not write notes"


@pytest.mark.parametrize("deploy_verify", [
    "premise: checked the target is still live",  # no probe: half
    "probe: dry-run diff came back clean",  # no premise: half
    "verified everything works fine before deploying",  # bare non-empty string, no structure
])
def test_ops_install_citation_with_incomplete_deploy_verify_refuses(
        tmp_path, repo_with_ops_artifact, deploy_verify):
    """wq#2762 (ISSUE-LOG #859, per wq#2630 'a probe must be able to fail'): deploy_verify is
    checked structurally so a bare claim can't satisfy it — both halves are required."""
    env = make_env(tmp_path)
    repo, art_dir, target = repo_with_ops_artifact
    live = target / "lumen_state.py"
    live.write_text("# fixed lifecycle closure logic\n")
    tid = open_ticket(env, "wq2762 self-test: incomplete deploy-verify")

    done = wq(env, "done", tid, "--notes", f"shipped {art_dir}/install.sh",
              "--deployed", str(live), "--deploy-verify", deploy_verify, cwd=repo)
    assert done.returncode == 9, done.stdout + done.stderr
    assert "deploy-verify" in (done.stdout + done.stderr).lower()


def test_ops_install_citation_with_stale_deployed_target_refuses(tmp_path, repo_with_ops_artifact):
    """--deployed pointing at a real file whose bytes don't match anything the artifact ships
    is exactly the wq#1632 failure mode with a plausible-looking flag slapped on — still rc=9."""
    env = make_env(tmp_path)
    repo, art_dir, target = repo_with_ops_artifact
    stale = target / "lumen_state.py"
    stale.write_text("# old code, install.sh never actually ran\n")
    tid = open_ticket(env, "wq1724 self-test: stale deployed target")

    done = wq(env, "done", tid, "--notes", f"shipped {art_dir}/install.sh",
              "--deployed", str(stale), cwd=repo)
    assert done.returncode == 9, done.stdout + done.stderr


def test_ops_install_citation_with_no_deploy_needed_closes(tmp_path, repo_with_ops_artifact):
    env = make_env(tmp_path)
    repo, art_dir, _target = repo_with_ops_artifact
    tid = open_ticket(env, "wq1724 self-test: no deploy needed escape hatch")

    done = wq(env, "done", tid, "--notes", f"shipped {art_dir}/install.sh",
              "--no-deploy-needed", "test: superseded before deploy", cwd=repo)
    assert done.returncode == 0, done.stdout + done.stderr
    assert "[NO-DEPLOY-NEEDED: test: superseded before deploy]" in notes_of(env, tid)


def test_non_ops_path_citation_is_unaffected(tmp_path, repo_with_ops_artifact):
    """The gate is scoped to ops/*/install.sh citations — an ordinary path evidence close
    (e.g. a report) must not be touched by it."""
    env = make_env(tmp_path)
    repo, _art_dir, _target = repo_with_ops_artifact
    (repo / "report.md").write_text("ordinary evidence\n")
    git(repo, "add", "report.md")
    git(repo, "commit", "-qm", "add report")
    tid = open_ticket(env, "wq1724 self-test: non-ops evidence unaffected")

    done = wq(env, "done", tid, "--notes", "see report.md", cwd=repo)
    assert done.returncode == 0, done.stdout + done.stderr
