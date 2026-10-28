"""Tests for the repo-read broker (wq#4762, DESIGN.md §2.5): canonical repo-key +
40-hex sha + grant enforcement, git-objects-only reads. Two independent fixture
repos are used throughout (never just one) so a pass can't be an artifact of one
hardcoded repo name (wq#2630: a probe drawn only from a single default population
can't falsify the key-matching logic).

Run: python3 -m pytest tests/test_repo_read.py -q
"""
import hashlib
import importlib.util
import json
import os
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from importlib.machinery import SourceFileLoader

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
BIN_DIR = os.path.join(HERE, "..")
sys.path.insert(0, BIN_DIR)
sys.path.insert(0, "/home/lumen/foreman/harness")

_loader = SourceFileLoader("repo_read_cli", os.path.join(BIN_DIR, "repo-read"))
_spec = importlib.util.spec_from_loader(_loader.name, _loader)
repo_read = importlib.util.module_from_spec(_spec)
_loader.exec_module(repo_read)

import repo_sweep  # noqa: E402


def make_fixture_repo(tmp_path, name):
    """A real git repo with one tracked file and one UNTRACKED .env, both present
    on disk after the commit. Returns (repo_path, commit_sha)."""
    repo = tmp_path / name
    repo.mkdir()
    subprocess.run(["git", "init", "-q", "-b", "main", str(repo)], check=True)
    subprocess.run(["git", "-C", str(repo), "config", "user.email", "t@example.invalid"], check=True)
    subprocess.run(["git", "-C", str(repo), "config", "user.name", "t"], check=True)
    (repo / "tracked.txt").write_text(f"hello from {name}\n")
    subprocess.run(["git", "-C", str(repo), "add", "tracked.txt"], check=True)
    subprocess.run(["git", "-C", str(repo), "commit", "-q", "-m", "init"], check=True)
    (repo / ".env").write_text("SECRET=untracked\n")  # never added/committed
    sha = subprocess.run(["git", "-C", str(repo), "rev-parse", "HEAD"],
                          capture_output=True, text=True, check=True).stdout.strip()
    return str(repo), sha


def write_grant(tmp_path, grants, ticket="4762"):
    grant = {
        "ticket": ticket,
        "attempt": 1,
        "expires": (datetime.now(timezone.utc) + timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "grants": grants,
    }
    raw = json.dumps(grant, sort_keys=True).encode()
    path = tmp_path / "grant.json"
    path.write_bytes(raw)
    return str(path), hashlib.sha256(raw).hexdigest()


@pytest.fixture
def two_repos(tmp_path):
    a_path, a_sha = make_fixture_repo(tmp_path, "repo-a")
    b_path, b_sha = make_fixture_repo(tmp_path, "repo-b")
    return {"a": (a_path, a_sha), "b": (b_path, b_sha)}


@pytest.fixture
def env(two_repos, monkeypatch):
    # repo_read.py's `import repo_sweep` shares this same module object (sys.modules
    # cache), so patching the attribute here is visible through repo_read.repo_sweep too.
    monkeypatch.setattr(repo_sweep, "managed_repos",
                         lambda: {two_repos["a"][0], two_repos["b"][0]})
    monkeypatch.setenv("WQ_AGENT", "fworker-wq4762")
    return two_repos


def grant_for(monkeypatch, tmp_path, key, sha, ticket="4762"):
    # Shape shared with the already-landed harness/hold_gate.py._repo_granted:
    # one grant entry {"kind": "repo", "repo": "<key>@<sha>"}.
    path, sha256 = write_grant(tmp_path, [{"kind": "repo", "repo": f"{key}@{sha}"}], ticket=ticket)
    monkeypatch.setenv("FOREMAN_GRANT_FILE", path)
    monkeypatch.setenv("FOREMAN_GRANT_SHA", sha256)


# ---------------------------------------------------------------------------
# canonical repo-key
# ---------------------------------------------------------------------------

def test_unmanaged_repo_key_refused(env, monkeypatch, tmp_path):
    rogue = str(tmp_path / "not-managed")
    os.makedirs(rogue, exist_ok=True)
    with pytest.raises(SystemExit) as e:
        repo_read.main(["ls", rogue, "0" * 40])
    assert e.value.code == 2


@pytest.mark.parametrize("which", ["a", "b"])
def test_managed_key_resolves_both_repos(env, monkeypatch, tmp_path, which):
    """wq#2630: this must pass for EACH independent fixture repo, not just one."""
    key, sha = env[which]
    grant_for(monkeypatch, tmp_path, key, sha)
    with pytest.raises(SystemExit) as e:
        repo_read.main(["ls", key, sha])
    assert e.value.code == 0


# ---------------------------------------------------------------------------
# sha shape
# ---------------------------------------------------------------------------

def test_branch_name_refused(env, monkeypatch, tmp_path):
    key, sha = env["a"]
    grant_for(monkeypatch, tmp_path, key, sha)
    with pytest.raises(SystemExit) as e:
        repo_read.main(["ls", key, "main"])
    assert e.value.code == 2


def test_abbreviated_sha_refused(env, monkeypatch, tmp_path):
    key, sha = env["a"]
    grant_for(monkeypatch, tmp_path, key, sha)
    with pytest.raises(SystemExit) as e:
        repo_read.main(["ls", key, sha[:12]])
    assert e.value.code == 2


def test_unknown_sha_refused(env, monkeypatch, tmp_path):
    key, sha = env["a"]
    fake = ("0" * 39) + "1"
    grant_for(monkeypatch, tmp_path, key, fake)
    with pytest.raises(SystemExit) as e:
        repo_read.main(["ls", key, fake])
    assert e.value.code == 2


# ---------------------------------------------------------------------------
# grant enforcement
# ---------------------------------------------------------------------------

def test_no_grant_env_denies(env, monkeypatch):
    monkeypatch.delenv("FOREMAN_GRANT_FILE", raising=False)
    monkeypatch.delenv("FOREMAN_GRANT_SHA", raising=False)
    key, sha = env["a"]
    with pytest.raises(SystemExit) as e:
        repo_read.main(["ls", key, sha])
    assert e.value.code == 2


def test_ungranted_sha_refused(env, monkeypatch, tmp_path):
    """Granted for repo-a's commit, then probed against repo-b's commit at the SAME
    key@ slot shape — must still deny; the grant is (key, sha)-specific."""
    a_key, a_sha = env["a"]
    b_key, b_sha = env["b"]
    grant_for(monkeypatch, tmp_path, a_key, a_sha)
    with pytest.raises(SystemExit) as e:
        repo_read.main(["ls", b_key, b_sha])
    assert e.value.code == 2


def test_ungranted_other_sha_same_repo_refused(env, monkeypatch, tmp_path):
    key, sha = env["a"]
    grant_for(monkeypatch, tmp_path, key, sha)
    other_sha = "f" + sha[1:]  # a different-looking but still-40-hex, non-existent commit
    with pytest.raises(SystemExit) as e:
        repo_read.main(["ls", key, other_sha])
    assert e.value.code == 2


# ---------------------------------------------------------------------------
# working tree is never read
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("which", ["a", "b"])
def test_untracked_env_file_unreachable(env, monkeypatch, tmp_path, which):
    key, sha = env[which]
    grant_for(monkeypatch, tmp_path, key, sha)
    with pytest.raises(SystemExit) as e:
        repo_read.main(["show", key, sha, ".env"])
    assert e.value.code != 0


@pytest.mark.parametrize("which", ["a", "b"])
def test_tracked_file_readable_at_pinned_sha(env, monkeypatch, tmp_path, which, capsys):
    key, sha = env[which]
    grant_for(monkeypatch, tmp_path, key, sha)
    with pytest.raises(SystemExit) as e:
        repo_read.main(["show", key, sha, "tracked.txt"])
    assert e.value.code == 0
    assert "hello from" in capsys.readouterr().out
