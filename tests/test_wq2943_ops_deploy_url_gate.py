"""wq#2943 self-test: the ops-deploy gate must also fire when the evidence is a PR URL.

EVIDENCE: wq#2910 shipped `ops/wq2910-a1-backup-important-hardening/` (logrotate cap +
`OnFailure=ntfy-onfailure` drop-in), closed `done` citing its PR URL, and neither file ever
reached the box — `ops-deploy-drift` read ABSENT 0/2 a day later, found by wq#2932's
verify-pass (reports/wq2932_verify_wq2910_undeployed_ops_bundle.md). The wq#1724/wq#2762
gate that exists to stop exactly that ran only on the `kind == "path"` citation branch, so
the PR-URL shape — how most PR-landed tickets cite evidence — walked past it.

These drive the real `wq done` against a scratch DB and a fake Gitea, same subprocess pattern
as test_wq_done_gate_terms.py, and assert: a PR carrying an undeployed bundle is refused
(rc 9), a bundle whose own `--verify` handler says live closes, the wq#1724 --deployed /
--deploy-verify proof still works for a handler-less bundle, --no-deploy-needed remains the
on-the-record override, and forge trouble fails open rather than wedging a lane.
"""
import http.server
import json
import os
import subprocess
import threading
import urllib.parse

import pytest

WQ_BIN = os.environ.get("WQ_BIN") or os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "wq")

PAYLOAD = "# wq2943 scratch payload\nkey = value\n"


def make_env(tmp_path, agent="test-wq2943-agent"):
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


def open_ticket(env, title):
    p = wq(env, "add", "--lane", "auto", "--title", title)
    assert p.returncode == 0, out(p)
    return next(tok for tok in p.stdout.split() if tok.isdigit())


def notes_of(env, tid):
    p = wq(env, "show", tid)
    assert p.returncode == 0, out(p)
    return p.stdout


def git(cwd, *args):
    p = subprocess.run(["git", "-C", str(cwd)] + list(args), capture_output=True, text=True)
    assert p.returncode == 0, p.stdout + p.stderr
    return p


@pytest.fixture
def repo(tmp_path):
    """A git checkout holding one ops bundle, plus the live target the bundle installs to.
    The bundle's `--verify` handler is self-contained (no ops/_verify.sh in a scratch repo)
    but answers the same contract: exit 0 live, exit 1 with a MISS line when it is not."""
    r = tmp_path / "repo"
    (r / "ops" / "wq9999-scratch-bundle").mkdir(parents=True)
    bundle = r / "ops" / "wq9999-scratch-bundle"
    (bundle / "payload.conf").write_text(PAYLOAD)
    target = tmp_path / "live" / "payload.conf"
    target.parent.mkdir()
    (bundle / "install.sh").write_text(
        "#!/usr/bin/env bash\nset -euo pipefail\n"
        'SRC="$(cd "$(dirname "$0")" && pwd)"\n'
        f'TARGET={target}\n'
        'if [ "${1:-}" = --verify ]; then\n'
        '  cmp -s "$SRC/payload.conf" "$TARGET" || { echo "MISS absent: $TARGET"; '
        'echo "VERIFY FAIL (1)"; exit 1; }\n'
        '  echo "VERIFY OK"; exit 0\nfi\n'
        'install -m 644 "$SRC/payload.conf" "$TARGET"\n')
    (bundle / "units").mkdir()
    (bundle / "units" / "scratch.service").write_text("[Service]\nExecStart=/bin/true\n")
    (r / "ops" / "wq8888-no-installer").mkdir()
    (r / "ops" / "wq8888-no-installer" / "NOTES.md").write_text("notes only\n")
    (r / "README.md").write_text("scratch\n")
    git(r, "init", "-q", "-b", "main")
    git(r, "config", "user.email", "t@example.invalid")
    git(r, "config", "user.name", "t")
    git(r, "add", "-A")
    git(r, "commit", "-qm", "scratch bundle")
    return r, bundle, target


def _install(bundle, target):
    target.write_text(PAYLOAD)


def _strip_verify_handler(bundle):
    """Same bundle, no wq#1696 marker — the shape the --deployed/--deploy-verify proof is for."""
    text = (bundle / "install.sh").read_text()
    (bundle / "install.sh").write_text(text.split('if [ "${1:-}" = --verify ]')[0] +
                                       'install -m 644 "$SRC/payload.conf" "$TARGET"\n')


class _FakeGitea(http.server.BaseHTTPRequestHandler):
    routes = {}

    def do_GET(self):
        body = self.routes.get(urllib.parse.urlparse(self.path).path)
        if body is None:
            self.send_response(404)
            self.end_headers()
            return
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
    """PR 7 touches the ops bundle; PR 8 touches only a README; PR 9's file list 404s.
    wq#4641: PR 10 touches only the bundle's payload (the wq#4458/wq#4638 selfheal.yaml
    shape), PR 11 two payload files of one bundle, PR 12 a file of an ops dir with no
    install.sh."""
    srv = _serve({
        "/api/v1/repos/o/r/pulls/10": MERGED_PR,
        "/api/v1/repos/o/r/pulls/10/files": [
            {"filename": "ops/wq9999-scratch-bundle/payload.conf"}],
        "/api/v1/repos/o/r/pulls/11": MERGED_PR,
        "/api/v1/repos/o/r/pulls/11/files": [
            {"filename": "ops/wq9999-scratch-bundle/payload.conf"},
            {"filename": "ops/wq9999-scratch-bundle/units/scratch.service"}],
        "/api/v1/repos/o/r/pulls/12": MERGED_PR,
        "/api/v1/repos/o/r/pulls/12/files": [
            {"filename": "ops/wq8888-no-installer/NOTES.md"}],
        "/api/v1/repos/o/r/pulls/7": MERGED_PR,
        "/api/v1/repos/o/r/pulls/7/files": [
            {"filename": "README.md"},
            {"filename": "ops/wq9999-scratch-bundle/install.sh"},
            {"filename": "ops/wq9999-scratch-bundle/payload.conf"}],
        "/api/v1/repos/o/r/pulls/8": MERGED_PR,
        "/api/v1/repos/o/r/pulls/8/files": [{"filename": "README.md"}],
        "/api/v1/repos/o/r/pulls/9": MERGED_PR,
    })
    yield f"127.0.0.1:{srv.server_address[1]}"
    srv.shutdown()


def test_merged_pr_carrying_an_undeployed_bundle_is_refused(tmp_path, repo, gitea):
    """The wq#2910 shape: bundle merged, never installed, ticket cites the PR URL."""
    env = make_env(tmp_path)
    r, _bundle, _target = repo
    tid = open_ticket(env, "wq2943 undeployed bundle via PR URL")
    p = wq(env, "done", tid, "--notes", f"http://{gitea}/o/r/pulls/7", cwd=r)
    assert p.returncode == 9, out(p)
    assert "wq#2943 ops-deploy gate" in p.stderr
    assert "wq9999-scratch-bundle" in p.stderr


def test_a_live_bundle_whose_verify_handler_passes_closes(tmp_path, repo, gitea):
    env = make_env(tmp_path)
    r, _bundle, target = repo
    _install(_bundle, target)
    tid = open_ticket(env, "wq2943 deployed bundle via PR URL")
    p = wq(env, "done", tid, "--notes", f"http://{gitea}/o/r/pulls/7", cwd=r)
    assert p.returncode == 0, out(p)
    assert "--verify OK" in p.stdout
    assert "[OPS-DEPLOYED:" in notes_of(env, tid)


def test_handlerless_bundle_still_closes_on_the_wq1724_deployed_proof(tmp_path, repo, gitea):
    """A bundle with no `--verify` marker is not run (wq#1696: a script that does not know
    the flag would install); the --deployed byte-match + --deploy-verify proof carries it."""
    env = make_env(tmp_path)
    r, bundle, target = repo
    _strip_verify_handler(bundle)
    _install(bundle, target)
    tid = open_ticket(env, "wq2943 handlerless bundle")
    p = wq(env, "done", tid, "--notes", f"http://{gitea}/o/r/pulls/7",
           "--deployed", str(target),
           "--deploy-verify", "premise: re-read the live target before install; "
                              "probe: install.sh --verify dry-run against the real path",
           cwd=r)
    assert p.returncode == 0, out(p)
    assert "[OPS-DEPLOYED:" in notes_of(env, tid)


def test_handlerless_bundle_without_the_proof_is_refused(tmp_path, repo, gitea):
    env = make_env(tmp_path)
    r, bundle, target = repo
    _strip_verify_handler(bundle)
    _install(bundle, target)
    tid = open_ticket(env, "wq2943 handlerless unproven bundle")
    p = wq(env, "done", tid, "--notes", f"http://{gitea}/o/r/pulls/7", cwd=r)
    assert p.returncode == 9, out(p)
    assert "ships no --verify handler" in p.stderr


def test_no_deploy_needed_remains_the_on_the_record_override(tmp_path, repo, gitea):
    env = make_env(tmp_path)
    r, _bundle, _target = repo
    tid = open_ticket(env, "wq2943 override")
    p = wq(env, "done", tid, "--notes", f"http://{gitea}/o/r/pulls/7",
           "--no-deploy-needed", "docs-only change to the bundle's README", cwd=r)
    assert p.returncode == 0, out(p)
    assert "[NO-DEPLOY-NEEDED:" in notes_of(env, tid)


def test_a_pr_touching_no_ops_bundle_is_untouched_by_the_gate(tmp_path, repo, gitea):
    env = make_env(tmp_path)
    r, _bundle, _target = repo
    tid = open_ticket(env, "wq2943 ordinary PR")
    p = wq(env, "done", tid, "--notes", f"http://{gitea}/o/r/pulls/8", cwd=r)
    assert p.returncode == 0, out(p)
    assert "ops-deploy gate" not in out(p)


def test_a_payload_only_pr_implicates_its_bundle(tmp_path, repo, gitea):
    """wq#4641: the wq#4458/wq#4638 shape. The PR edits only a file the bundle ships, never
    its install.sh; the install.sh-only regex matched nothing and the close went through
    with the target never touched."""
    env = make_env(tmp_path)
    r, _bundle, _target = repo
    tid = open_ticket(env, "wq4641 payload-only PR")
    p = wq(env, "done", tid, "--notes", f"http://{gitea}/o/r/pulls/10", cwd=r)
    assert p.returncode == 9, out(p)
    assert "wq#2943 ops-deploy gate" in p.stderr
    assert "wq9999-scratch-bundle" in p.stderr


def test_a_bundle_touched_in_several_files_is_checked_once(tmp_path, repo, gitea):
    env = make_env(tmp_path)
    r, bundle, target = repo
    _install(bundle, target)
    tid = open_ticket(env, "wq4641 two payload files, one bundle")
    p = wq(env, "done", tid, "--notes", f"http://{gitea}/o/r/pulls/11", cwd=r)
    assert p.returncode == 0, out(p)
    assert p.stdout.count("wq9999-scratch-bundle: --verify OK") == 1, out(p)


def test_a_payload_only_pr_on_a_retired_bundle_closes(tmp_path, repo, gitea):
    env = make_env(tmp_path)
    r, bundle, _target = repo
    (bundle / "RETIRED").write_text("superseded by wq9998\n")
    tid = open_ticket(env, "wq4641 payload-only PR, retired bundle")
    p = wq(env, "done", tid, "--notes", f"http://{gitea}/o/r/pulls/10", cwd=r)
    assert p.returncode == 0, out(p)


def test_an_ops_dir_without_an_install_sh_is_untouched_by_the_gate(tmp_path, repo, gitea):
    env = make_env(tmp_path)
    r, _bundle, _target = repo
    tid = open_ticket(env, "wq4641 ops dir with no installer")
    p = wq(env, "done", tid, "--notes", f"http://{gitea}/o/r/pulls/12", cwd=r)
    assert p.returncode == 0, out(p)
    assert "ops-deploy gate" not in out(p)


def test_forge_trouble_fails_open_with_a_warning(tmp_path, repo, gitea):
    """PR 9 has no /files route: the gate cannot tell, so it warns and closes — forge
    trouble is not a ticket's fault (same posture as the wq#899 merge-state gate)."""
    env = make_env(tmp_path)
    r, _bundle, _target = repo
    tid = open_ticket(env, "wq2943 forge trouble")
    p = wq(env, "done", tid, "--notes", f"http://{gitea}/o/r/pulls/9", cwd=r)
    assert p.returncode == 0, out(p)
    assert "could not list the files" in p.stdout
