"""wq#1702 self-test: `wq done` holds a close to the ticket's own terms.

design/wq1702-done-gate.md. Four gaps the wq#759/#899/#1673 gates left open, each driven
through the real `wq` binary against a scratch DB and throwaway git repos (same subprocess
pattern as test_wq_evidence_durability.py):
  D1 a deliverable named in the ticket TEXT (Deliverable:/Output:/->) is checked like the column
  D2 a "via lumen-pr" ticket needs proof it landed; --no-artifact does not excuse that (rc 8)
  D3 an untracked file in a PRIMARY checkout is not durable evidence (rc 6); ignored files are
  D4 github.com tokens come from lumen-github-identity.sh; an unverifiable PR is stamped
"""
import http.server
import json
import os
import subprocess
import threading

import pytest

WQ_BIN = os.environ.get("WQ_BIN") or os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "wq")


def make_env(tmp_path, agent="test-wq1702-agent"):
    env = dict(os.environ)
    env.pop("GITHUB_TOKEN", None)
    env["WQ_DB"] = str(tmp_path / "scratch_wq.db")
    env["WQ_AGENT"] = agent
    env["WQ_DISPATCHER_CONFIG"] = str(tmp_path / "no-such-dispatcher-config.json")
    env["WQ_MERGE_STATE_TIMEOUT"] = "3"
    tok = tmp_path / "gitea.token"
    tok.write_text("scratch-gitea-token\n")
    env["GITEA_TOKEN_FILE"] = str(tok)
    env["WQ_GITHUB_IDENTITY_LIB"] = str(tmp_path / "no-such-identity-lib.sh")
    return env


def wq(env, *args, cwd=None):
    return subprocess.run([WQ_BIN] + list(args), capture_output=True, text=True, env=env,
                          cwd=str(cwd) if cwd else None)


def git(cwd, *args):
    p = subprocess.run(["git", "-C", str(cwd)] + list(args), capture_output=True, text=True)
    assert p.returncode == 0, p.stdout + p.stderr
    return p


def open_ticket(env, title, detail=None):
    extra = ["--detail", detail] if detail else []
    p = wq(env, "add", "--lane", "auto", "--title", title, *extra)
    assert p.returncode == 0, p.stdout + p.stderr
    tid = str(int(p.stdout.strip().split()[2]))
    claim = wq(env, "claim", "--id", tid)
    assert claim.returncode == 0, claim.stdout + claim.stderr
    return tid


def notes_of(env, tid):
    show = wq(env, "show", tid, "--json")
    assert show.returncode == 0, show.stdout + show.stderr
    return json.loads(show.stdout)["notes"] or ""


def out(p):
    return p.stdout + p.stderr


@pytest.fixture
def plain(tmp_path):
    d = tmp_path / "plain"
    d.mkdir()
    (d / "evidence.md").write_text("an existing file outside any git repo\n")
    return d


@pytest.fixture
def repo(tmp_path):
    """Primary checkout: main carries landed.md and a .gitignore for state/; a linked worktree
    on branch `feature` carries one commit that is NOT on main."""
    r = tmp_path / "repo"
    r.mkdir()
    git(r, "init", "-q", "-b", "main")
    git(r, "config", "user.email", "test@example.invalid")
    git(r, "config", "user.name", "wq1702 test")
    (r / "landed.md").write_text("on main\n")
    (r / ".gitignore").write_text("state/\n")
    git(r, "add", "landed.md", ".gitignore")
    git(r, "commit", "-qm", "seed")
    wt = tmp_path / "wt"
    git(r, "worktree", "add", "-q", str(wt), "-b", "feature")
    (wt / "branch-only.md").write_text("never merged\n")
    git(wt, "add", "branch-only.md")
    git(wt, "commit", "-qm", "branch only")
    main_sha = git(r, "rev-parse", "main").stdout.strip()
    branch_sha = git(wt, "rev-parse", "HEAD").stdout.strip()
    return r, wt, main_sha, branch_sha


# ---------------------------------------------------------------- D1 derived deliverable
def test_derived_deliverable_missing_refuses_rc7(tmp_path, plain):
    env = make_env(tmp_path)
    tid = open_ticket(env, "wq1702 d1 missing", f"Build it. Deliverable: {tmp_path}/nope/out.md")
    p = wq(env, "done", tid, "--notes", f"see {plain}/evidence.md", cwd=plain)
    assert p.returncode == 7, out(p)
    assert "named in the ticket text" in out(p)
    assert notes_of(env, tid) == ""


def test_derived_deliverable_not_excused_by_no_artifact(tmp_path, plain):
    env = make_env(tmp_path)
    tid = open_ticket(env, "wq1702 d1 no-artifact", f"Output: {tmp_path}/nope/out.md")
    p = wq(env, "done", tid, "--no-artifact", "nothing to cite", cwd=plain)
    assert p.returncode == 7, out(p)


def test_derived_deliverable_present_closes(tmp_path, plain):
    env = make_env(tmp_path)
    tid = open_ticket(env, "wq1702 d1 present", f"Output: {plain}/evidence.md")
    p = wq(env, "done", tid, "--notes", f"{plain}/evidence.md", cwd=plain)
    assert p.returncode == 0, out(p)


def test_arrow_deliverable_missing_refuses_rc7(tmp_path, plain):
    env = make_env(tmp_path)
    tid = open_ticket(env, "wq1702 d1 arrow", f"measure and write the table -> {tmp_path}/nope/t.json")
    p = wq(env, "done", tid, "--notes", f"{plain}/evidence.md", cwd=plain)
    assert p.returncode == 7, out(p)


def test_report_marker_is_an_input_not_a_deliverable(tmp_path, plain):
    env = make_env(tmp_path)
    tid = open_ticket(env, "wq1702 d1 report", f"Report: {tmp_path}/gone/input_from_last_week.md")
    p = wq(env, "done", tid, "--notes", f"{plain}/evidence.md", cwd=plain)
    assert p.returncode == 0, out(p)


def test_relative_deliverable_resolves_through_notes_suffix(tmp_path, plain):
    env = make_env(tmp_path)
    deep = tmp_path / "somewhere" / "v4" / "reports"
    deep.mkdir(parents=True)
    (deep / "wq1702_sweep.md").write_text("result\n")
    tid = open_ticket(env, "wq1702 d1 suffix", "Deliverable: reports/wq1702_sweep.md")
    p = wq(env, "done", tid, "--notes", f"{deep}/wq1702_sweep.md", cwd=plain)
    assert p.returncode == 0, out(p)


def test_placeholder_date_deliverable_matches_the_dated_file(tmp_path, plain):
    env = make_env(tmp_path)
    (plain / "scan-20260910.md").write_text("dated\n")
    tid = open_ticket(env, "wq1702 d1 placeholder", f"Deliverable: {plain}/scan-YYYYMMDD.md")
    p = wq(env, "done", tid, "--notes", f"{plain}/scan-20260910.md", cwd=plain)
    assert p.returncode == 0, out(p)


def test_placeholder_date_deliverable_absent_refuses_rc7(tmp_path, plain):
    env = make_env(tmp_path)
    tid = open_ticket(env, "wq1702 d1 placeholder absent", f"Deliverable: {plain}/never-YYYYMMDD.md")
    p = wq(env, "done", tid, "--notes", f"{plain}/evidence.md", cwd=plain)
    assert p.returncode == 7, out(p)


def test_relative_deliverable_under_the_queue_dir_resolves(tmp_path, plain):
    env = make_env(tmp_path)
    (tmp_path / "reports").mkdir()
    (tmp_path / "reports" / "wq1702_queue_home.md").write_text("in work-queue/reports\n")
    tid = open_ticket(env, "wq1702 d1 queue dir", "Deliverable: reports/wq1702_queue_home.md")
    p = wq(env, "done", tid, "--notes", f"{plain}/evidence.md", cwd=plain)
    assert p.returncode == 0, out(p)
    assert "could not verify" not in out(p)


def test_unanchored_relative_deliverable_fails_open_visibly(tmp_path, plain):
    env = make_env(tmp_path)
    tid = open_ticket(env, "wq1702 d1 ambiguous", "Deliverable: pack-x/reports/wq1702_nowhere.md")
    p = wq(env, "done", tid, "--notes", f"{plain}/evidence.md", cwd=plain)
    assert p.returncode == 0, out(p)
    assert "could not verify" in out(p)


# ---------------------------------------------------------------- D2 landing demand
def test_landing_demand_no_artifact_refuses_rc8(tmp_path, plain):
    env = make_env(tmp_path)
    tid = open_ticket(env, "wq1702 d2 no-artifact", "Fix it. Land via lumen-pr, test both paths.")
    p = wq(env, "done", tid, "--no-artifact", "config on box", cwd=plain)
    assert p.returncode == 8, out(p)
    assert "wq#1702 landing gate" in out(p)
    assert notes_of(env, tid) == ""


def test_landing_demand_unlanded_override_is_on_the_record(tmp_path, plain):
    env = make_env(tmp_path)
    tid = open_ticket(env, "wq1702 d2 override", "Land via lumen-pr.")
    p = wq(env, "done", tid, "--no-artifact", "duplicate of wq#1", "--unlanded", "duplicate, nothing built",
           cwd=plain)
    assert p.returncode == 0, out(p)
    n = notes_of(env, tid)
    assert "[UNLANDED: duplicate, nothing built]" in n and "[NO-ARTIFACT:" in n


def test_landing_demand_path_outside_git_refuses_rc8(tmp_path, plain):
    env = make_env(tmp_path)
    tid = open_ticket(env, "wq1702 d2 plain path", "Land via lumen-pr.")
    p = wq(env, "done", tid, "--notes", f"{plain}/evidence.md", cwd=plain)
    assert p.returncode == 8, out(p)


def test_landing_demand_sha_on_main_closes(tmp_path, repo):
    env = make_env(tmp_path)
    r, _, main_sha, _ = repo
    tid = open_ticket(env, "wq1702 d2 sha main", "lumen-pr land when green")
    p = wq(env, "done", tid, "--notes", f"landed {main_sha}", cwd=r)
    assert p.returncode == 0, out(p)
    assert "landing gate" in out(p)


def test_landing_demand_sha_only_on_a_branch_refuses_rc8(tmp_path, repo):
    env = make_env(tmp_path)
    r, _, _, branch_sha = repo
    tid = open_ticket(env, "wq1702 d2 sha branch", "Land via lumen-pr.")
    p = wq(env, "done", tid, "--notes", f"pushed {branch_sha}", cwd=r)
    assert p.returncode == 8, out(p)


def test_landing_demand_path_committed_on_main_closes(tmp_path, repo):
    env = make_env(tmp_path)
    r, _, _, _ = repo
    tid = open_ticket(env, "wq1702 d2 path main", "Land via lumen-pr.")
    p = wq(env, "done", tid, "--notes", f"{r}/landed.md", cwd=r)
    assert p.returncode == 0, out(p)


def test_no_landing_demand_keeps_old_behaviour(tmp_path, plain):
    env = make_env(tmp_path)
    tid = open_ticket(env, "wq1702 d2 no demand", "Check the thing on the box.")
    p = wq(env, "done", tid, "--no-artifact", "verify-pass, config on box", cwd=plain)
    assert p.returncode == 0, out(p)


# ---------------------------------------------------------------- D3 primary checkout
def test_untracked_file_in_primary_checkout_refuses_rc6(tmp_path, repo):
    env = make_env(tmp_path)
    r, _, _, _ = repo
    (r / "live-edit.md").write_text("written live, never committed\n")
    tid = open_ticket(env, "wq1702 d3 untracked")
    p = wq(env, "done", tid, "--notes", f"{r}/live-edit.md", cwd=r)
    assert p.returncode == 6, out(p)
    assert "never landed" in out(p)


def test_gitignored_file_in_primary_checkout_closes(tmp_path, repo):
    env = make_env(tmp_path)
    r, _, _, _ = repo
    (r / "state").mkdir()
    (r / "state" / "run.json").write_text("{}\n")
    tid = open_ticket(env, "wq1702 d3 ignored")
    p = wq(env, "done", tid, "--notes", f"{r}/state/run.json", cwd=r)
    assert p.returncode == 0, out(p)


def test_committed_file_in_primary_checkout_closes(tmp_path, repo):
    env = make_env(tmp_path)
    r, _, _, _ = repo
    tid = open_ticket(env, "wq1702 d3 committed")
    p = wq(env, "done", tid, "--notes", f"{r}/landed.md", cwd=r)
    assert p.returncode == 0, out(p)


# ---------------------------------------------------------------- D4 forge
class _FakeGitea(http.server.BaseHTTPRequestHandler):
    prs = {}

    def do_GET(self):
        body = self.prs.get(self.path)
        if body is None:
            self.send_response(404); self.end_headers(); return
        data = json.dumps(body).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, *a):
        pass


@pytest.fixture
def gitea():
    srv = http.server.HTTPServer(("127.0.0.1", 0), _FakeGitea)
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    _FakeGitea.prs = {
        "/api/v1/repos/o/r/pulls/7": {"state": "closed", "merged": True,
                                      "head": {"sha": "a" * 40}, "base": {"ref": "main"}},
        "/api/v1/repos/o/r/pulls/8": {"state": "open", "merged": False,
                                      "head": {"sha": "b" * 40}, "base": {"ref": "main"}},
    }
    yield f"127.0.0.1:{srv.server_address[1]}"
    srv.shutdown()


def test_landing_demand_merged_pr_closes(tmp_path, plain, gitea):
    env = make_env(tmp_path)
    tid = open_ticket(env, "wq1702 d4 merged", "Land via lumen-pr.")
    p = wq(env, "done", tid, "--notes", f"http://{gitea}/o/r/pulls/7", cwd=plain)
    assert p.returncode == 0, out(p)
    assert "[UNVERIFIED-PR" not in notes_of(env, tid)


def test_landing_demand_open_pr_still_refuses_rc3(tmp_path, plain, gitea):
    env = make_env(tmp_path)
    tid = open_ticket(env, "wq1702 d4 open", "Land via lumen-pr.")
    p = wq(env, "done", tid, "--notes", f"http://{gitea}/o/r/pulls/8", cwd=plain)
    assert p.returncode == 3, out(p)


def test_unverifiable_pr_closes_but_is_stamped(tmp_path, plain):
    env = make_env(tmp_path)
    tid = open_ticket(env, "wq1702 d4 unknown", "Land via lumen-pr.")
    p = wq(env, "done", tid, "--notes", "http://127.0.0.1:9/o/r/pulls/1", cwd=plain)
    assert p.returncode == 0, out(p)
    assert "[UNVERIFIED-PR:" in notes_of(env, tid)


def test_github_without_any_token_is_stamped_no_token(tmp_path, plain):
    env = make_env(tmp_path)
    tid = open_ticket(env, "wq1702 d4 github no token")
    p = wq(env, "done", tid, "--notes", "https://github.com/o/r/pull/1", cwd=plain)
    assert p.returncode == 0, out(p)
    assert "[UNVERIFIED-PR: no forge token for github.com]" in notes_of(env, tid)


def test_github_token_comes_from_the_identity_lib(tmp_path, plain):
    env = make_env(tmp_path)
    lib = tmp_path / "fake-identity.sh"
    lib.write_text('resolve_github_token() { [ "$1" = "o/r" ] || return 1; '
                   'GITHUB_TOKEN=wq1702-sentinel-not-a-real-token; export GITHUB_TOKEN; }\n')
    env["WQ_GITHUB_IDENTITY_LIB"] = str(lib)
    tid = open_ticket(env, "wq1702 d4 github lib")
    p = wq(env, "done", tid, "--notes", "https://github.com/o/r/pull/1", cwd=plain)
    assert p.returncode == 0, out(p)
    n = notes_of(env, tid)
    # the token was found (the forge then refuses a fake token, or is unreachable) — either way
    # the answer is no longer "no forge token", which is what blinded the gate (audit F1)
    assert "[UNVERIFIED-PR:" in n, n
    assert "no forge token" not in n, n
    assert "wq1702-sentinel" not in out(p) + n, "the token must never be printed or stored"


def test_numeric_agent_refusal_names_claim_id(tmp_path):
    env = make_env(tmp_path)
    p = wq(env, "claim", "1702")
    assert p.returncode == 1
    assert "wq claim --id" in out(p)
