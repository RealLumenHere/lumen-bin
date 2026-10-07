"""wq#3487 self-test: a `wq done` that overrides the ops-deploy gate on a bundle whose own
`--verify` confirms FAIL must leave a tracked follow-up, not just free text.

EVIDENCE: wq#1724/wq#2943 (test_wq_ops_deploy_gate.py, test_wq2943_ops_deploy_url_gate.py)
already refuse an undeployed ops bundle UNLESS the closer passes --deployed proof or records
--no-deploy-needed/--no-artifact. Both overrides are accepted as free text today — nothing
re-runs the bundle's own --verify to see whether the override is actually true. wq#3336
closed this way 2026-10-01 (PR-URL citation, timer bundle never installed); wq#3359's
verify-pass *noted* the gap in prose and still nobody ticketed it; wq#3479 did the actual root
install five days later, found only by an unrelated verify-pass. This is the class fix: the
override still closes the ticket (a sandboxed worker genuinely cannot do a root install — that
stays true), but a bundle whose own --verify says FAIL now gets a `[ops-root-apply]` follow-up
ticket filed (or reused, deduped on title) automatically, carrying
`Liveness: ops_verified(<bundle>)` so it is machine-closeable the moment someone actually runs
the install.

Same subprocess-driven pattern as test_wq2943_ops_deploy_url_gate.py; the PR-URL fixtures are
reused near-verbatim so the two gates are exercised identically.
"""
import http.server
import json
import os
import sqlite3
import subprocess
import threading
import urllib.parse

import pytest

WQ_BIN = os.environ.get("WQ_BIN") or os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "wq")

PAYLOAD = "# wq3487 scratch payload\nkey = value\n"


def make_env(tmp_path, agent="test-wq3487-agent"):
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


def out(p):
    return p.stdout + p.stderr


def open_ticket(env, title, extra=()):
    p = wq(env, "add", "--lane", "auto", "--title", title, *extra)
    assert p.returncode == 0, out(p)
    return next(tok for tok in p.stdout.split() if tok.isdigit())


def notes_of(env, tid):
    p = wq(env, "show", tid)
    assert p.returncode == 0, out(p)
    return p.stdout


def tasks(env):
    """Direct read of the scratch queue — simpler and more trustworthy than scraping `wq
    list` output for a ticket this test itself filed, not one it opened."""
    c = sqlite3.connect(env["WQ_DB"])
    try:
        rows = c.execute("SELECT id, title, detail, status, lane FROM tasks").fetchall()
    finally:
        c.close()
    return rows


def followups(env):
    return [r for r in tasks(env) if r[1].startswith("[ops-root-apply]")]


def git(cwd, *args):
    p = subprocess.run(["git", "-C", str(cwd)] + list(args), capture_output=True, text=True)
    assert p.returncode == 0, p.stdout + p.stderr
    return p


_VERIFY_SH = (
    'SRC="$(cd "$(dirname "$0")" && pwd)"\n'
    'TARGET={target}\n'
    'if [ "${{1:-}}" = --verify ]; then\n'
    '  cmp -s "$SRC/payload.conf" "$TARGET" || {{ echo "MISS absent: $TARGET"; '
    'echo "VERIFY FAIL (1)"; exit 1; }}\n'
    '  echo "VERIFY OK"; exit 0\n'
    'fi\n'
    'install -m 644 "$SRC/payload.conf" "$TARGET"\n')


def _make_bundle(root, name, target):
    """A real ops/<name>/ bundle with the ops/VERIFY_CONTRACT.md handler shape: exit 1 with a
    MISS line when the unit was never installed, exit 0 once it is — same contract real
    bundles answer, not a stand-in that always fails."""
    bundle = root / "ops" / name
    bundle.mkdir(parents=True)
    (bundle / "payload.conf").write_text(PAYLOAD)
    (bundle / "install.sh").write_text("#!/usr/bin/env bash\nset -euo pipefail\n" +
                                       _VERIFY_SH.format(target=target))
    return bundle


@pytest.fixture
def repo(tmp_path):
    r = tmp_path / "repo"
    target = tmp_path / "live" / "payload.conf"
    target.parent.mkdir(parents=True)
    bundle = _make_bundle(r, "wq9999-scratch-bundle", target)
    (r / "README.md").write_text("scratch\n")
    git(r, "init", "-q", "-b", "main")
    git(r, "config", "user.email", "t@example.invalid")
    git(r, "config", "user.name", "t")
    git(r, "add", "-A")
    git(r, "commit", "-qm", "scratch bundle")
    return r, bundle, target


class _FakeGitea(http.server.BaseHTTPRequestHandler):
    routes = {}

    def do_GET(self):
        body = self.routes.get(urllib.parse.urlparse(self.path).path)
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


def _serve(routes):
    _FakeGitea.routes = routes
    srv = http.server.HTTPServer(("127.0.0.1", 0), _FakeGitea)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


MERGED_PR = {"state": "closed", "merged": True, "head": {"sha": "a" * 40},
             "base": {"ref": "main"}}


@pytest.fixture
def gitea():
    srv = _serve({
        "/api/v1/repos/o/r/pulls/7": MERGED_PR,
        "/api/v1/repos/o/r/pulls/7/files": [
            {"filename": "README.md"},
            {"filename": "ops/wq9999-scratch-bundle/install.sh"},
            {"filename": "ops/wq9999-scratch-bundle/payload.conf"}],
    })
    yield f"127.0.0.1:{srv.server_address[1]}"
    srv.shutdown()


def test_no_deploy_needed_over_failing_verify_files_followup_via_pr_url(tmp_path, repo, gitea):
    env = make_env(tmp_path)
    r, _bundle, _target = repo
    tid = open_ticket(env, "wq3487 self-test: PR-URL override over failing verify")
    p = wq(env, "done", tid, "--notes", f"http://{gitea}/o/r/pulls/7",
           "--no-deploy-needed", "sandboxed worker cannot do a root install", cwd=r)
    assert p.returncode == 0, out(p)
    notes = notes_of(env, tid)
    assert "[NO-DEPLOY-NEEDED:" in notes
    fups = followups(env)
    assert len(fups) == 1, f"expected exactly one follow-up, got {fups}"
    fid, ftitle, fdetail, fstatus, flane = fups[0]
    assert ftitle == ("[ops-root-apply] wq9999-scratch-bundle: install.sh --verify fails, "
                      "needs privileged install")
    assert "Liveness: ops_verified(wq9999-scratch-bundle)" in fdetail
    assert fstatus == "open"
    assert f"[ROOT-APPLY-FILED: wq#{fid}]" in notes


def test_no_deploy_needed_over_failing_verify_files_followup_via_path(tmp_path, repo):
    env = make_env(tmp_path)
    r, bundle, _target = repo
    tid = open_ticket(env, "wq3487 self-test: path override over failing verify")
    p = wq(env, "done", tid, "--notes", f"shipped {bundle}/install.sh",
           "--no-deploy-needed", "sandboxed worker cannot do a root install", cwd=r)
    assert p.returncode == 0, out(p)
    fups = followups(env)
    assert len(fups) == 1, f"expected exactly one follow-up, got {fups}"
    assert f"[ROOT-APPLY-FILED: wq#{fups[0][0]}]" in notes_of(env, tid)


def test_repeated_close_over_the_same_bundle_reuses_one_followup(tmp_path, repo, gitea):
    env = make_env(tmp_path)
    r, _bundle, _target = repo
    tid1 = open_ticket(env, "wq3487 self-test: first close over the same bundle")
    p1 = wq(env, "done", tid1, "--notes", f"http://{gitea}/o/r/pulls/7",
            "--no-deploy-needed", "round 1", cwd=r)
    assert p1.returncode == 0, out(p1)
    tid2 = open_ticket(env, "wq3487 self-test: second close over the same bundle")
    p2 = wq(env, "done", tid2, "--notes", f"http://{gitea}/o/r/pulls/7",
            "--no-deploy-needed", "round 2", cwd=r)
    assert p2.returncode == 0, out(p2)
    fups = followups(env)
    assert len(fups) == 1, f"expected the second close to reuse the first follow-up, got {fups}"


def test_passing_verify_never_files_a_followup(tmp_path, repo, gitea):
    """Can-fail probe (wq#2630): drawn from OUTSIDE the fix's target population — a bundle
    whose --verify actually passes must never get a root-apply ticket, override or not."""
    env = make_env(tmp_path)
    r, _bundle, target = repo
    target.write_text(PAYLOAD)  # install it — --verify now says OK
    tid = open_ticket(env, "wq3487 self-test: no followup when verify passes")
    p = wq(env, "done", tid, "--notes", f"http://{gitea}/o/r/pulls/7", cwd=r)
    assert p.returncode == 0, out(p)
    assert followups(env) == []


def test_retired_marker_suppresses_the_gate_and_the_followup(tmp_path, repo, gitea):
    """Can-fail probe (wq#2630): a RETIRED-marked bundle is out of scope for the whole gate —
    the close does not even need --no-deploy-needed, and files no follow-up."""
    env = make_env(tmp_path)
    r, bundle, _target = repo
    (bundle / "RETIRED").write_text("superseded by lumen-budget-alert.timer (wq#3487 test)\n")
    git(r, "add", "-A"); git(r, "commit", "-qm", "retire the scratch bundle")
    tid = open_ticket(env, "wq3487 self-test: retired bundle needs no override")
    p = wq(env, "done", tid, "--notes", f"http://{gitea}/o/r/pulls/7", cwd=r)
    assert p.returncode == 0, out(p)
    assert followups(env) == []
    assert "NO-DEPLOY-NEEDED" not in notes_of(env, tid)


def test_no_artifact_citing_a_known_failing_bundle_files_followup(tmp_path, repo):
    """--no-artifact skips find_artifact entirely; the bundle has to be found some other way —
    here, the ticket's own title names it, the wq#901/wq#1652 shape."""
    env = make_env(tmp_path)
    r, _bundle, _target = repo
    tid = open_ticket(env, "wq3487 self-test: ops/wq9999-scratch-bundle/install.sh ready, "
                           "rollout pending")
    p = wq(env, "done", tid, "--no-artifact", "sandboxed worker cannot do a root install", cwd=r)
    assert p.returncode == 0, out(p)
    notes = notes_of(env, tid)
    assert "[NO-ARTIFACT:" in notes
    fups = followups(env)
    assert len(fups) == 1, f"expected exactly one follow-up, got {fups}"
    assert f"[ROOT-APPLY-FILED: wq#{fups[0][0]}]" in notes


def test_no_artifact_with_no_bundle_reference_is_unaffected(tmp_path, repo):
    """The text-scan is narrow: an ordinary --no-artifact close that never names an ops bundle
    must not be touched by this gate at all."""
    env = make_env(tmp_path)
    r, _bundle, _target = repo
    tid = open_ticket(env, "wq3487 self-test: ordinary no-artifact close")
    p = wq(env, "done", tid, "--no-artifact", "nothing to cite, design-only ticket", cwd=r)
    assert p.returncode == 0, out(p)
    assert followups(env) == []
    assert "ROOT-APPLY-FILED" not in notes_of(env, tid)
