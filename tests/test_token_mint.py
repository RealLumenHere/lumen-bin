"""token-mint against a mock Cloudflare API. No network, no real credentials."""
import importlib.machinery
import importlib.util
import json
import os
import shutil
import stat
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

BIN = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "token-mint")
ACCT = "a" * 32
ZONE = "b" * 32
ADMIN = "cf" + "at_" + "ADMIN" + "x" * 30           # fake, built at runtime
NEWVAL = "cf" + "at_" + "NEWVALUE" + "y" * 28

GROUPS = [
    {"id": "g-dns-write", "name": "DNS Write", "scopes": ["com.cloudflare.api.account.zone"]},
    {"id": "g-dns-read", "name": "DNS Read", "scopes": ["com.cloudflare.api.account.zone"]},
    {"id": "g-workers", "name": "Workers Scripts Write", "scopes": ["com.cloudflare.api.account"]},
    {"id": "g-tokens", "name": "API Tokens Write", "scopes": ["com.cloudflare.api.account"]},
    {"id": "g-user", "name": "User Details Read", "scopes": ["com.cloudflare.api.user"]},
]


class State:
    def __init__(self):
        self.calls = []        # (method, path, bearer)
        self.posts = []
        self.deleted = []
        self.counter = 0
        self.verify_ok_for_new = True
        self.values = {}       # value -> token id
        self.old_policies = [{"effect": "allow", "resources": {"com.cloudflare.api.account.zone." + ZONE: "*"},
                              "permission_groups": [{"id": "g-dns-write", "name": "DNS Write"}]}]


def make_server(state):
    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def _send(self, code, payload):
            raw = json.dumps(payload).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

        def _bearer(self):
            return self.headers.get("Authorization", "").replace("Bearer ", "")

        def _route(self, method):
            path = self.path.split("?")[0]
            bearer = self._bearer()
            state.calls.append((method, path, bearer))
            base = "/accounts/%s/tokens" % ACCT
            body = None
            if method == "POST":
                n = int(self.headers.get("Content-Length") or 0)
                body = json.loads(self.rfile.read(n) or b"{}")
            if path == base + "/verify":
                if bearer == ADMIN:
                    return self._send(200, {"success": True, "result": {"id": "tok-admin", "status": "active"}})
                if bearer in state.values and state.verify_ok_for_new:
                    return self._send(200, {"success": True, "result": {"id": state.values[bearer], "status": "active"}})
                return self._send(401, {"success": False, "errors": [{"code": 1000, "message": "Invalid API Token"}]})
            if bearer != ADMIN:
                return self._send(403, {"success": False, "errors": [{"code": 9109, "message": "nope"}]})
            if path == base + "/permission_groups" and method == "GET":
                return self._send(200, {"success": True, "result": GROUPS})
            if path == base and method == "GET":
                return self._send(200, {"success": True, "result_info": {"total_pages": 1}, "result": [
                    {"id": "tok-admin", "name": "token-admin", "status": "active"},
                    {"id": "tok-old", "name": "dns-edit", "status": "active"}]})
            if path == base + "/tok-old" and method == "GET":
                return self._send(200, {"success": True, "result": {"id": "tok-old", "name": "dns-edit",
                                                                     "policies": state.old_policies}})
            if path == base and method == "POST":
                state.posts.append(body)
                state.counter += 1
                tid = "tok-new-%d" % state.counter
                val = NEWVAL + str(state.counter)
                state.values[val] = tid
                return self._send(200, {"success": True, "result": {"id": tid, "name": body["name"], "value": val,
                                                                     "status": "active",
                                                                     "expires_on": body.get("expires_on")}})
            if method == "DELETE" and path.startswith(base + "/"):
                state.deleted.append(path.rsplit("/", 1)[1])
                return self._send(200, {"success": True, "result": {"id": path.rsplit("/", 1)[1]}})
            return self._send(404, {"success": False, "errors": [{"code": 7003, "message": "no route"}]})

        def do_GET(self):
            self._route("GET")

        def do_POST(self):
            self._route("POST")

        def do_DELETE(self):
            self._route("DELETE")

    srv = HTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


@pytest.fixture
def env(tmp_path):
    st = State()
    srv = make_server(st)
    sec = tmp_path / "secrets"
    sec.mkdir()
    (sec / "cf-token-admin.token").write_text(ADMIN + "\n")
    (sec / "dns-edit.token").write_text("OLDVALUE-not-a-real-token\n")
    acct = tmp_path / "acct"
    acct.write_text(ACCT + "\n")
    e = dict(os.environ, TOKEN_MINT_API_BASE="http://127.0.0.1:%d" % srv.server_port, TOKEN_MINT_SECRETS=str(sec),
             TOKEN_MINT_LOG=str(tmp_path / "audit.jsonl"), TOKEN_MINT_ACCOUNT_FILE=str(acct),
             TOKEN_MINT_VERIFY_RETRIES="1")
    e.pop("CF_ACCOUNT_ID", None)
    # wq#3865: rotate is behind the vault-backup gate. Sandbox the gate (never the real /home/lumen/.secrets)
    # and seed a backup that IS the current value so the pre-existing rotate tests keep exercising the happy path.
    bk = tmp_path / "rvb-backups"
    bk.mkdir()
    shutil.copy2(sec / "dns-edit.token", bk / "dns-edit.token.bak.20261014_000000")
    e["RVB_BACKUP_DIRS"] = str(bk)
    e["RVB_LOG"] = str(tmp_path / "rvb.log")
    yield {"st": st, "env": e, "sec": sec, "log": tmp_path / "audit.jsonl", "tmp": tmp_path, "bk": bk}
    srv.shutdown()


def tm(env, *args):
    return subprocess.run([sys.executable, BIN, *args], env=env["env"], capture_output=True, text=True, timeout=60)


def test_groups_lists_scopes(env):
    r = tm(env, "groups", "--filter", "dns")
    assert r.returncode == 0 and "DNS Write" in r.stdout and "zone" in r.stdout and "Workers" not in r.stdout


def test_mint_dry_run_creates_nothing(env):
    r = tm(env, "mint", "--name", "dns-new", "--perm", "DNS Write", "--zone-id", ZONE)
    assert r.returncode == 0 and "dry run" in r.stdout
    assert "com.cloudflare.api.account.zone." + ZONE in r.stdout
    assert env["st"].posts == [] and not (env["sec"] / "dns-new.token").exists()


def test_mint_confirm_stores_0600_and_never_prints_value(env):
    r = tm(env, "mint", "--name", "dns-new", "--perm", "DNS Write", "--perm", "Workers Scripts Write",
           "--zone-id", ZONE, "--ttl-days", "30", "--confirm")
    assert r.returncode == 0, r.stderr
    f = env["sec"] / "dns-new.token"
    val = f.read_text().strip()
    assert val.startswith(NEWVAL)
    assert stat.S_IMODE(f.stat().st_mode) == 0o600
    assert val not in r.stdout and val not in r.stderr and ADMIN not in r.stdout + r.stderr
    assert "verified=active" in r.stdout
    log = env["log"].read_text()
    assert val not in log and ADMIN not in log and '"fp"' in log
    body = env["st"].posts[0]
    res = {tuple(p["resources"]): [g["id"] for g in p["permission_groups"]] for p in body["policies"]}
    assert res[("com.cloudflare.api.account.zone." + ZONE,)] == ["g-dns-write"]
    assert res[("com.cloudflare.api.account." + ACCT,)] == ["g-workers"]
    assert "*" not in "".join(k for p in body["policies"] for k in p["resources"] if k.endswith("*"))
    assert body["expires_on"].endswith("Z")
    post_bearer = [c[2] for c in env["st"].calls if c[0] == "POST"][0]
    assert post_bearer == ADMIN
    verify_bearer = [c[2] for c in env["st"].calls if c[1].endswith("/verify")][-1]
    assert verify_bearer == val                      # the NEW token proved itself, not the admin


def test_guards_refuse_before_any_post(env):
    cases = [
        (["--perm", "API Tokens Write"], "could mint further tokens"),
        (["--perm", "User Details Read"], "cannot be used"),
        (["--perm", "Nonexistent Perm"], "unknown permission group"),
        (["--perm", "DNS Write"], "pass --zone-id"),            # zone perm, no zone given
    ]
    for extra, msg in cases:
        r = tm(env, "mint", "--name", "x1", *extra, "--confirm")
        assert r.returncode == 2 and msg in r.stderr, (extra, r.stderr)
    assert env["st"].posts == []
    assert tm(env, "mint", "--name", "../evil", "--perm", "Workers Scripts Write", "--confirm").returncode == 2


def test_existing_file_needs_overwrite_and_is_backed_up(env):
    r = tm(env, "mint", "--name", "dns-edit", "--perm", "DNS Write", "--zone-id", ZONE, "--confirm")
    assert r.returncode == 2 and "exists" in r.stderr and env["st"].posts == []
    r = tm(env, "mint", "--name", "dns-edit", "--perm", "DNS Write", "--zone-id", ZONE, "--confirm", "--overwrite")
    assert r.returncode == 0, r.stderr
    baks = list((env["sec"] / "bak").glob("dns-edit.token.bak.*"))
    assert len(baks) == 1 and baks[0].read_text().startswith("OLDVALUE")
    assert stat.S_IMODE(baks[0].stat().st_mode) == 0o600


def test_rotate_copies_policies_verifies_then_revokes_old(env):
    r = tm(env, "rotate", "dns-edit", "--revoke-old", "--confirm")
    assert r.returncode == 0, r.stderr
    body = env["st"].posts[0]
    assert body["name"] == "dns-edit"
    assert body["policies"][0]["permission_groups"] == [{"id": "g-dns-write"}]
    assert body["policies"][0]["resources"] == {"com.cloudflare.api.account.zone." + ZONE: "*"}
    new = (env["sec"] / "dns-edit.token").read_text().strip()
    assert new.startswith(NEWVAL)
    assert env["st"].deleted == ["tok-old"]
    seq = [(c[0], c[1].rsplit("/", 1)[-1]) for c in env["st"].calls]
    assert seq.index(("DELETE", "tok-old")) > seq.index(("GET", "verify"), seq.index(("POST", "tokens")))  # verify first
    assert new not in r.stdout + r.stderr
    assert "stale-env-audit" in r.stdout


def test_rotate_without_revoke_leaves_old_alive(env):
    r = tm(env, "rotate", "dns-edit", "--confirm")
    assert r.returncode == 0 and env["st"].deleted == [] and "still live" in r.stdout


def test_rotate_failed_verify_keeps_old_token(env):
    env["st"].verify_ok_for_new = False
    r = tm(env, "rotate", "dns-edit", "--revoke-old", "--confirm")
    assert r.returncode == 1 and "NOT revoked" in r.stderr
    assert env["st"].deleted == []


def test_rotate_dry_run_and_unknown_name(env):
    r = tm(env, "rotate", "dns-edit")
    assert r.returncode == 0 and "dry run" in r.stdout and env["st"].posts == []
    assert tm(env, "rotate", "no-such-token", "--confirm").returncode == 2


def test_revoke_refuses_admin_and_needs_confirm(env):
    assert tm(env, "revoke", "--id", "tok-admin", "--confirm").returncode == 2
    assert tm(env, "revoke", "--id", "tok-old").returncode == 0 and env["st"].deleted == []
    assert tm(env, "revoke", "--id", "tok-old", "--confirm").returncode == 0 and env["st"].deleted == ["tok-old"]


def test_verify_uses_file_token(env):
    (env["sec"] / "x.token").write_text(ADMIN + "\n")
    r = tm(env, "verify", "--file", str(env["sec"] / "x.token"))
    assert r.returncode == 0 and "status=active" in r.stdout


def test_print_value_is_opt_in(env):
    r = tm(env, "mint", "--name", "pv", "--perm", "Workers Scripts Write", "--confirm", "--print-value")
    val = (env["sec"] / "pv.token").read_text().strip()
    assert r.stdout.strip().splitlines()[-1] == val and "burned" in r.stderr


def test_api_error_never_leaks_credentials(env):
    (env["sec"] / "cf-token-admin.token").write_text("wrong-admin-token-0000\n")
    r = tm(env, "list")
    assert r.returncode == 1 and "HTTP 403" in r.stderr and "wrong-admin-token-0000" not in r.stderr


def _load():
    loader = importlib.machinery.SourceFileLoader("token_mint", BIN)
    spec = importlib.util.spec_from_loader("token_mint", loader)
    mod = importlib.util.module_from_spec(spec)
    loader.exec_module(mod)
    return mod


def test_orphan_token_deleted_if_value_cannot_be_stored(env, monkeypatch, capsys):
    for k, v in env["env"].items():
        monkeypatch.setenv(k, v)
    tmm = _load()

    def boom(*a, **k):
        raise OSError(28, "No space left on device")
    monkeypatch.setattr(tmm, "write_secret", boom)
    rc = tmmmain = tmm.main(["mint", "--name", "orph", "--perm", "Workers Scripts Write", "--confirm"])
    err = capsys.readouterr().err
    assert rc == 1 and "was deleted again" in err
    assert env["st"].deleted == ["tok-new-1"]


# ---- wq#3865: vault-backup gate on rotate (dummy credentials only) ----

def _nothing_changed(env):
    return (env["st"].posts == [] and env["st"].deleted == []
            and (env["sec"] / "dns-edit.token").read_text() == "OLDVALUE-not-a-real-token\n")


def test_rotate_refused_without_vault_backup(env):
    for f in env["bk"].iterdir():
        f.unlink()
    r = tm(env, "rotate", "dns-edit", "--revoke-old", "--confirm")
    assert r.returncode == 2 and "ROTATION REFUSED" in r.stderr and "gate refused" in r.stderr
    assert _nothing_changed(env)
    rvb = (env["tmp"] / "rvb.log").read_text()
    assert "REFUSED" in rvb and "OLDVALUE" not in rvb


def test_rotate_refused_when_backup_holds_an_older_value(env):
    (env["bk"] / "dns-edit.token.bak.20261014_000000").write_text("an-older-dummy-value\n")
    r = tm(env, "rotate", "dns-edit", "--confirm")
    assert r.returncode == 2 and _nothing_changed(env)


def test_rotate_dry_run_needs_no_backup(env):
    for f in env["bk"].iterdir():
        f.unlink()
    r = tm(env, "rotate", "dns-edit")
    assert r.returncode == 0 and "dry run" in r.stdout and env["st"].posts == []


def test_rotate_revoke_old_with_new_out_path_still_gated_by_name(env):
    for f in env["bk"].iterdir():
        f.unlink()
    out = env["tmp"] / "elsewhere.token"
    r = tm(env, "rotate", "dns-edit", "--revoke-old", "--out", str(out), "--confirm")
    assert r.returncode == 2 and env["st"].posts == [] and env["st"].deleted == [] and not out.exists()
    (env["bk"] / "elsewhere.token.bak.20261014_000001").write_text("name-only-backup\n")
    r = tm(env, "rotate", "dns-edit", "--revoke-old", "--out", str(out), "--confirm")
    assert r.returncode == 0, r.stderr
    assert env["st"].deleted == ["tok-old"]
