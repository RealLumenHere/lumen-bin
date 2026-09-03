"""wq-hygiene-reconcile — kb/ISSUE-SOLUTIONS-fable.md #7 (wq#1257).

Three fixtures, per the spec's own test: done+merged (repaired), done+missing-evidence
(FRANK-flagged via ntfy, not touched), genuinely open (untouched). Same scratch-DB /
subprocess pattern as test_wq_reap.py: a real `wq` against a throwaway sqlite DB, plus a
throwaway git repo standing in for a "known repo" so sha-ancestor checks are real, not
mocked. ntfy-send is swapped for a stub that logs its argv instead of publishing.
"""
import importlib.machinery
import importlib.util
import json
import os
import sqlite3
import subprocess

import pytest

WQ_BIN = os.environ.get("WQ_BIN", "/home/lumen/bin/wq")
RECONCILE_BIN = os.environ.get("RECONCILE_BIN", "/home/lumen/bin/wq-hygiene-reconcile")
FMT = "%Y-%m-%d %H:%M:%S"


@pytest.fixture(scope="module")
def reconcile():
    """The reconcile script imported as a module, so the evidence logic can be tested
    directly with forge/git verdicts forced — the wq#1320 false-alarm scenario needs a
    PR that reads back as still-open, which no offline subprocess run can produce."""
    loader = importlib.machinery.SourceFileLoader("wq_hygiene_reconcile", RECONCILE_BIN)
    spec = importlib.util.spec_from_loader(loader.name, loader)
    mod = importlib.util.module_from_spec(spec)
    loader.exec_module(mod)
    return mod


def git(repo, *args, check=True):
    return subprocess.run(["git", "-C", str(repo), *args], check=check,
                          capture_output=True, text=True)


def make_repo_with_landed_commit(tmp_path):
    """A bare 'origin' plus a clone with one commit on main — stands in for a
    'known repo' wq-hygiene-reconcile searches for sha evidence. Returns (repo_dir, sha)."""
    origin = tmp_path / "origin.git"
    origin.mkdir()
    git(origin, "init", "-q", "--bare")

    repo = tmp_path / "known_repo"
    repo.mkdir()
    git(repo, "init", "-q")
    git(repo, "config", "user.email", "test@test")
    git(repo, "config", "user.name", "test")
    (repo / "f.txt").write_text("v1\n")
    git(repo, "add", "f.txt")
    git(repo, "commit", "-qm", "landed work")
    git(repo, "branch", "-M", "main")
    git(repo, "remote", "add", "origin", str(origin))
    git(repo, "push", "-q", "origin", "main")
    sha = git(repo, "rev-parse", "HEAD").stdout.strip()
    return repo, sha


def make_repo_with_squash_merged_branch(tmp_path):
    """wq#1322: a feature-branch commit whose PATCH landed on main via a squash
    merge — the branch sha itself is never an ancestor of main, but a different
    commit on main carries an identical diff. Returns (repo_dir, branch_sha)."""
    repo = tmp_path / "squash_repo"
    repo.mkdir()
    git(repo, "init", "-q")
    git(repo, "config", "user.email", "test@test")
    git(repo, "config", "user.name", "test")
    (repo / "base.txt").write_text("base\n")
    git(repo, "add", "base.txt")
    git(repo, "commit", "-qm", "base")
    git(repo, "branch", "-M", "main")

    git(repo, "checkout", "-qb", "feat/thing")
    (repo / "feature.txt").write_text("feature work\n")
    git(repo, "add", "feature.txt")
    git(repo, "commit", "-qm", "feat: thing")
    branch_sha = git(repo, "rev-parse", "HEAD").stdout.strip()

    git(repo, "checkout", "-q", "main")
    git(repo, "checkout", "-q", "feat/thing", "--", "feature.txt")
    git(repo, "commit", "-qm", "feat: thing (#1) squash-merged")
    return repo, branch_sha


def make_stub_ntfy(tmp_path):
    log = tmp_path / "ntfy.log"
    stub = tmp_path / "ntfy-send-stub"
    stub.write_text(f"#!/bin/bash\necho \"$@\" >> {log}\nexit 0\n")
    stub.chmod(0o755)
    return stub, log


def make_env(tmp_path, repos):
    env = dict(os.environ)
    env["WQ_DB"] = str(tmp_path / "scratch_wq.db")
    env["WQ_AGENT"] = "test-hygiene-agent"
    env["WQ_DISPATCHER_CONFIG"] = str(tmp_path / "no-such-dispatcher-config.json")
    env["WQ_HYGIENE_REPOS"] = ",".join(str(r) for r in repos)
    stub, log = make_stub_ntfy(tmp_path)
    env["WQ_HYGIENE_NTFY_BIN"] = str(stub)
    env["WQ_HYGIENE_STATE"] = str(tmp_path / "state.json")
    env["WQ_HYGIENE_STAMP"] = str(tmp_path / "stamp.json")
    env["GITHUB_TOKEN"] = ""
    env["GITEA_TOKEN_FILE"] = str(tmp_path / "no-such-token")
    return env, log


def wq(env, *args):
    return subprocess.run([WQ_BIN] + list(args), capture_output=True, text=True, env=env)


def wq_add(env, title, lane="auto"):
    p = wq(env, "add", "--lane", lane, "--title", title)
    assert p.returncode == 0, p.stdout + p.stderr
    return int(p.stdout.strip().split()[2])


def wq_show(env, ticket_id):
    p = wq(env, "show", str(ticket_id))
    out = {}
    for line in p.stdout.splitlines():
        if ":" in line:
            k, _, v = line.partition(":")
            out[k.strip()] = v.strip()
    return out


def set_notes(db_path, ticket_id, notes):
    c = sqlite3.connect(db_path)
    c.execute("UPDATE tasks SET notes=? WHERE id=?", (notes, ticket_id))
    c.commit()
    c.close()


def run_reconcile(env, *extra):
    return subprocess.run([RECONCILE_BIN] + list(extra), capture_output=True, text=True,
                          env=env, timeout=60)


def test_open_ticket_with_landed_sha_is_repaired(tmp_path):
    repo, sha = make_repo_with_landed_commit(tmp_path)
    env, ntfy_log = make_env(tmp_path, [repo])
    ticket_id = wq_add(env, "done-but-open: work actually landed")
    set_notes(env["WQ_DB"], ticket_id,
             f"WIP — landed as {sha}, forgot to close the ticket")

    result = run_reconcile(env)
    assert result.returncode == 0, result.stdout + result.stderr
    assert f"REPAIR wq#{ticket_id}: repaired" in result.stdout

    after = wq_show(env, ticket_id)
    assert after["status"] == "done"
    assert "reconciled-by=wq-hygiene" in after.get("notes", "")
    assert sha in after.get("notes", "")


def test_done_ticket_with_vanished_evidence_is_flagged_not_touched(tmp_path):
    repo, _sha = make_repo_with_landed_commit(tmp_path)
    env, ntfy_log = make_env(tmp_path, [repo])
    ticket_id = wq_add(env, "done citing evidence that later vanished")
    # A sha-shaped citation `wq done` accepts at close time (shape-only gate, wq#759)
    # but that was never actually landed in any known repo — the rot this sweep exists
    # to catch.
    fake_sha = "deadbeefcafe0123456789abcdef0123456789"
    done = wq(env, "done", str(ticket_id), "--notes", f"closed, evidence {fake_sha}")
    assert done.returncode == 0, done.stdout + done.stderr
    before = wq_show(env, ticket_id)

    result = run_reconcile(env)
    assert result.returncode == 0, result.stdout + result.stderr
    assert f"FLAG wq#{ticket_id}: flagged" in result.stdout
    assert ntfy_log.exists()
    assert f"wq#{ticket_id}" in ntfy_log.read_text()

    after = wq_show(env, ticket_id)
    assert after["status"] == "done"
    assert after["notes"] == before["notes"]


def test_genuinely_open_ticket_untouched(tmp_path):
    repo, _sha = make_repo_with_landed_commit(tmp_path)
    env, ntfy_log = make_env(tmp_path, [repo])
    ticket_id = wq_add(env, "genuinely still open, no evidence of anything")
    set_notes(env["WQ_DB"], ticket_id, "still working on it, nothing to cite yet")

    result = run_reconcile(env)
    assert result.returncode == 0, result.stdout + result.stderr
    assert f"wq#{ticket_id}" not in result.stdout

    after = wq_show(env, ticket_id)
    assert after["status"] == "open"
    assert not ntfy_log.exists()


def test_flag_is_transition_based_not_repeated_every_run(tmp_path):
    repo, _sha = make_repo_with_landed_commit(tmp_path)
    env, ntfy_log = make_env(tmp_path, [repo])
    ticket_id = wq_add(env, "done citing evidence that never verifies")
    fake_sha = "0000000cafe0123456789abcdef0123456789ab"
    wq(env, "done", str(ticket_id), "--notes", f"closed, evidence {fake_sha}")

    first = run_reconcile(env)
    assert f"FLAG wq#{ticket_id}: flagged" in first.stdout
    first_log = ntfy_log.read_text()

    second = run_reconcile(env)
    assert f"FLAG wq#{ticket_id}: flag-suppressed(no-transition)" in second.stdout
    assert ntfy_log.read_text() == first_log  # no new ntfy-send call


def test_dry_run_reports_without_mutating_or_alerting(tmp_path):
    repo, sha = make_repo_with_landed_commit(tmp_path)
    env, ntfy_log = make_env(tmp_path, [repo])
    ticket_id = wq_add(env, "dry-run repair candidate")
    set_notes(env["WQ_DB"], ticket_id, f"landed as {sha}")

    result = run_reconcile(env, "--dry-run")
    assert f"REPAIR wq#{ticket_id}: would-repair" in result.stdout

    after = wq_show(env, ticket_id)
    assert after["status"] == "open"
    assert not ntfy_log.exists()
    assert not (tmp_path / "state.json").exists()
    assert not (tmp_path / "stamp.json").exists()


# ---------------------------------------------------------------- wq#1320: multi-artifact notes
# Real done-ticket notes on this fleet routinely cite several artifacts at once
# ("foreman PR#219 <url> + lumen-bin PR#53 <url> + kb 65a5c39"). The first cut of
# find_artifact stopped at the first regex hit, and looked for URLs before shas
# regardless of order in the text, so an incidental PR link could decide a ticket's fate
# while the real evidence next to it was never inspected.

LANDED_SHA = "1111111aaaa2222222bbbb3333333cccc4444444"
OPEN_PR = "http://forge.example/lumen/foreman/pulls/219"
MERGED_PR = "http://forge.example/lumen/lumen-bin/pulls/53"


def force_verdicts(reconcile, monkeypatch, merged_urls=(), landed_shas=()):
    monkeypatch.setattr(reconcile, "sha_landed", lambda sha: (
        ("/home/lumen/kb", True) if sha in landed_shas else (None, None)))
    monkeypatch.setattr(reconcile, "pr_merge_state", lambda url: (
        ("merged", f"{url} merged") if url in merged_urls
        else ("open", f"{url} is still OPEN")))


def test_find_artifacts_collects_every_citation_in_text_order(reconcile):
    notes = f"foreman PR#219 {OPEN_PR} + lumen-bin PR#53 {MERGED_PR} + kb {LANDED_SHA}"
    assert reconcile.find_artifacts(notes) == [
        ("url", OPEN_PR), ("url", MERGED_PR), ("sha", LANDED_SHA)]


def test_find_artifacts_sees_a_sha_that_precedes_a_url(reconcile):
    """The old URL-search-first order made this sha invisible wherever it sat."""
    assert reconcile.find_artifacts(f"landed {LANDED_SHA}, tracked in {OPEN_PR}") == [
        ("sha", LANDED_SHA), ("url", OPEN_PR)]


def test_done_ticket_not_flagged_when_a_later_citation_still_verifies(
        reconcile, monkeypatch):
    """The wq#1320 false page: a merely-referenced, still-open PR matched first must not
    page Frank about a done ticket whose own sha evidence is landed."""
    force_verdicts(reconcile, monkeypatch, landed_shas={LANDED_SHA})
    notes = f"tracking {OPEN_PR}; this ticket's work landed as {LANDED_SHA}"
    assert reconcile.rot_reason(notes) is None


def test_done_ticket_still_flagged_when_nothing_verifies(reconcile, monkeypatch):
    force_verdicts(reconcile, monkeypatch)
    notes = f"tracking {OPEN_PR}; landed as {LANDED_SHA}"
    reason = reconcile.rot_reason(notes)
    assert reason is not None
    assert LANDED_SHA in reason and OPEN_PR in reason


def test_open_ticket_repaired_from_a_sha_cited_after_an_unrelated_url(
        reconcile, monkeypatch):
    """The symmetric false negative: the sha is real and landed, but the URL ahead of it
    was the only thing ever inspected."""
    force_verdicts(reconcile, monkeypatch, merged_urls={MERGED_PR},
                   landed_shas={LANDED_SHA})
    evidence = reconcile.repair_candidate(f"see {MERGED_PR}, landed as {LANDED_SHA}")
    assert evidence is not None
    assert MERGED_PR in evidence and LANDED_SHA in evidence


def test_mixed_evidence_neither_repairs_nor_flags(reconcile, monkeypatch):
    """One citation landed, another definitively did not: too ambiguous to auto-close a
    ticket whose other cited work is still open, and too weak to page on."""
    force_verdicts(reconcile, monkeypatch, landed_shas={LANDED_SHA})
    notes = f"{OPEN_PR} still open; {LANDED_SHA} landed"
    assert reconcile.evidence_verdict(notes)[0] == reconcile.UNKNOWN
    assert reconcile.repair_candidate(notes) is None
    assert reconcile.rot_reason(notes) is None


def test_unreachable_forge_does_not_veto_a_landed_sha(reconcile, monkeypatch):
    """An UNKNOWN citation carries no information, so it must not block a repair the
    remaining evidence justifies — otherwise a forge outage stalls the sweep."""
    monkeypatch.setattr(reconcile, "sha_landed", lambda sha: ("/home/lumen/kb", True))
    monkeypatch.setattr(reconcile, "pr_merge_state",
                        lambda url: ("unknown", "forge unreachable"))
    assert reconcile.repair_candidate(f"{OPEN_PR} and {LANDED_SHA}") is not None


def test_repair_finds_landed_sha_cited_after_a_pr_link_end_to_end(tmp_path):
    """Same regression through the real CLI: no forge token, so the PR link resolves
    UNKNOWN, and the landed sha behind it is what must be found."""
    repo, sha = make_repo_with_landed_commit(tmp_path)
    env, _ntfy_log = make_env(tmp_path, [repo])
    ticket_id = wq_add(env, "cites a tracking PR before its own landed sha")
    set_notes(env["WQ_DB"], ticket_id, f"tracked in {OPEN_PR} — landed as {sha}")

    result = run_reconcile(env)
    assert result.returncode == 0, result.stdout + result.stderr
    assert f"REPAIR wq#{ticket_id}: repaired" in result.stdout
    assert wq_show(env, ticket_id)["status"] == "done"


def test_done_ticket_not_flagged_over_a_stale_citation_end_to_end(tmp_path):
    """A done note citing a bogus sha ahead of its real one used to flag on the bogus
    one alone; the real landed sha beside it now settles it."""
    repo, sha = make_repo_with_landed_commit(tmp_path)
    env, ntfy_log = make_env(tmp_path, [repo])
    ticket_id = wq_add(env, "done, cites a stale sha ahead of the real one")
    stale = "deadbeefcafe0123456789abcdef0123456789"
    wq(env, "done", str(ticket_id), "--notes", f"superseded {stale}; shipped as {sha}")

    result = run_reconcile(env)
    assert result.returncode == 0, result.stdout + result.stderr
    assert f"FLAG wq#{ticket_id}" not in result.stdout
    assert not ntfy_log.exists()


def test_live_claim_is_not_touched_even_with_landed_evidence(tmp_path):
    """A ticket under an active, unexpired claim is real in-flight work — wq's own
    owner_ok would refuse the close anyway, but the sweep should not even attempt it
    (or alert on it): the fence-guard is defense in depth, not the only guard."""
    repo, sha = make_repo_with_landed_commit(tmp_path)
    env, ntfy_log = make_env(tmp_path, [repo])
    ticket_id = wq_add(env, "actively claimed, coincidentally sha-shaped note")
    claim = wq(env, "claim", "--id", str(ticket_id))
    assert claim.returncode == 0, claim.stdout + claim.stderr
    set_notes(env["WQ_DB"], ticket_id, f"progress note, not evidence: {sha}")

    result = run_reconcile(env)
    assert f"wq#{ticket_id}" not in result.stdout

    after = wq_show(env, ticket_id)
    assert after["status"] == "claimed"


def test_sha_landed_recognizes_a_squash_merged_patch(reconcile, tmp_path, monkeypatch):
    """wq#1322: the branch sha a worker cites is never an ancestor of main once the
    forge squash-merges its PR — only the patch survives, under a different sha. Without
    the fix this would report (repo, False) forever, an unrepairable, unrepairing false
    rot flag."""
    repo, branch_sha = make_repo_with_squash_merged_branch(tmp_path)
    monkeypatch.setenv("WQ_HYGIENE_REPOS", str(repo))
    found_repo, verified = reconcile.sha_landed(branch_sha)
    assert found_repo == str(repo)
    assert verified is True


def test_sha_landed_recognizes_a_squash_merged_patch_from_an_abbreviated_sha(
        reconcile, tmp_path, monkeypatch):
    """The done-note convention cites abbreviated shas (`be7f0f6`, not the full 40
    chars), but `git cherry` always prints full shas — a raw string match against the
    abbreviated form silently never hits, which is what actually produced wq#1322's 14
    false flags in the live dry-run."""
    repo, branch_sha = make_repo_with_squash_merged_branch(tmp_path)
    monkeypatch.setenv("WQ_HYGIENE_REPOS", str(repo))
    found_repo, verified = reconcile.sha_landed(branch_sha[:7])
    assert found_repo == str(repo)
    assert verified is True


def test_sha_landed_still_fails_a_sha_whose_patch_never_landed(reconcile, tmp_path,
                                                                monkeypatch):
    """The patch-id equivalence check must not widen into 'sha exists anywhere' — a
    branch sha whose diff was never applied to main stays unverified."""
    repo, _branch_sha = make_repo_with_squash_merged_branch(tmp_path)
    git(repo, "checkout", "-q", "-b", "feat/other", "main")
    (repo / "unrelated.txt").write_text("never merged\n")
    git(repo, "add", "unrelated.txt")
    git(repo, "commit", "-qm", "feat: never merged")
    stray_sha = git(repo, "rev-parse", "HEAD").stdout.strip()
    monkeypatch.setenv("WQ_HYGIENE_REPOS", str(repo))

    found_repo, verified = reconcile.sha_landed(stray_sha)
    assert found_repo == str(repo)
    assert verified is False


def test_repair_finds_squash_merged_sha_end_to_end(tmp_path):
    """Same regression through the real CLI: a done-but-open ticket citing a
    squash-merged branch sha must be repaired, not left as a standing false page."""
    repo, branch_sha = make_repo_with_squash_merged_branch(tmp_path)
    env, _ntfy_log = make_env(tmp_path, [repo])
    ticket_id = wq_add(env, "done-but-open: squash-merged pre-merge sha cited")
    set_notes(env["WQ_DB"], ticket_id, f"landed as {branch_sha}")

    result = run_reconcile(env)
    assert result.returncode == 0, result.stdout + result.stderr
    assert f"REPAIR wq#{ticket_id}: repaired" in result.stdout
    assert wq_show(env, ticket_id)["status"] == "done"
