"""wq#1672 self-test: the wq#899 merge-state gate must actually gate github.com PRs.

Before this fix, `_forge_token('github.com')` read only `$GITHUB_TOKEN` -- a var wq#1480
stripped from agent/.env, and fworker never exported it -- so every github.com PR resolved
'unknown' and `done` failed open (TICKET_CLOSURE_AUDIT.md F1: 13 tickets closed `done`
against still-open LPP PRs, none carrying [UNVERIFIED-PR]). This drives real `wq done`
against a known-open PR and asserts the gate actually refuses it, rc=3 -- same
scratch-DB/subprocess pattern as test_wq_reap.py, skipped if this box has neither a
usable github token nor network reach to api.github.com.
"""
import os
import subprocess
import urllib.request

import pytest

WQ_BIN = os.environ.get("WQ_BIN") or os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "wq")
GITHUB_TOKEN_FILE = os.environ.get("GITHUB_TOKEN_FILE", "/home/lumen/.secrets/github-frank.token")
GITHUB_CLASSIC_TOKEN_FILE = os.environ.get(
    "GITHUB_CLASSIC_TOKEN_FILE", "/home/lumen/.secrets/github-lumen-classic.token")
# frankstupak/lumen-plays-pokemon#92 -- stacked on an unmerged sibling PR by design
# (wq#1192/wq#1195 notes), not deployed. Stable known-open fixture for this test.
KNOWN_OPEN_PR = "https://github.com/frankstupak/lumen-plays-pokemon/pull/92"


def _have_token():
    for path in (GITHUB_TOKEN_FILE, GITHUB_CLASSIC_TOKEN_FILE):
        try:
            with open(path) as fh:
                if fh.read().strip():
                    return True
        except OSError:
            continue
    return False


def _have_network():
    try:
        urllib.request.urlopen("https://api.github.com", timeout=5)
        return True
    except Exception:
        return False


def make_env(tmp_path, agent="test-merge-gate-agent"):
    env = dict(os.environ)
    env["WQ_DB"] = str(tmp_path / "scratch_wq.db")
    env["WQ_AGENT"] = agent
    # wq#912: scratch lane names are never real dispatcher lanes -- point the lane guard
    # at a path that doesn't exist so it fails open instead of refusing against the live
    # dispatcher config.
    env["WQ_DISPATCHER_CONFIG"] = str(tmp_path / "no-such-dispatcher-config.json")
    return env


def wq(env, *args):
    return subprocess.run([WQ_BIN] + list(args), capture_output=True, text=True, env=env)


def wq_add(env, title, lane="auto"):
    p = wq(env, "add", "--lane", lane, "--title", title)
    assert p.returncode == 0, p.stdout + p.stderr
    return int(p.stdout.strip().split()[2])


@pytest.mark.skipif(not _have_token(), reason="no github token file on this box")
@pytest.mark.skipif(not _have_network(), reason="no network reach to api.github.com")
def test_done_against_a_known_open_pr_refuses_rc3(tmp_path):
    env = make_env(tmp_path)
    ticket_id = wq_add(env, "wq1672 self-test: merge-state gate")
    claim = wq(env, "claim", "--id", str(ticket_id))
    assert claim.returncode == 0, claim.stdout + claim.stderr

    result = wq(env, "done", str(ticket_id), "--notes", f"PR {KNOWN_OPEN_PR} -- not merged")
    assert result.returncode == 3, (
        f"expected rc=3 (wq#899 gate refuses an open PR), got {result.returncode}: "
        f"{result.stdout}{result.stderr}")
    assert "wq#899" in result.stdout + result.stderr
    assert "still OPEN" in result.stdout + result.stderr or "not on" in result.stdout + result.stderr


@pytest.mark.skipif(not _have_token(), reason="no github token file on this box")
@pytest.mark.skipif(not _have_network(), reason="no network reach to api.github.com")
def test_done_against_a_known_open_pr_accepts_with_unmerged_flag(tmp_path):
    env = make_env(tmp_path)
    ticket_id = wq_add(env, "wq1672 self-test: merge-state gate --unmerged escape hatch")
    claim = wq(env, "claim", "--id", str(ticket_id))
    assert claim.returncode == 0, claim.stdout + claim.stderr

    result = wq(env, "done", str(ticket_id), "--notes", f"PR {KNOWN_OPEN_PR} -- not merged",
                "--unmerged", "test: intentionally closing over an open PR")
    assert result.returncode == 0, result.stdout + result.stderr

    show = wq(env, "show", str(ticket_id), "--json")
    assert "[UNMERGED-PR: test: intentionally closing over an open PR]" in show.stdout
