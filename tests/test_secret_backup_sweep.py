"""wq#4599 secret-backup-sweep. Each hazard found while building it has a regression test here."""
import argparse, importlib.machinery, importlib.util, os, subprocess, sys, time
import pytest

SRC = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "secret-backup-sweep")
CUR, OLD, OTHER = "tok_current_aaaaaaaaaaaa", "tok_old_bbbbbbbbbbbbbbb", "tok_unique_cccccccccccc"


@pytest.fixture
def m(tmp_path, monkeypatch):
    monkeypatch.setenv("SBS_NO_DOCKER", "1")
    loader = importlib.machinery.SourceFileLoader("sbs_under_test", SRC)
    mod = importlib.util.module_from_spec(importlib.util.spec_from_loader("sbs_under_test", loader))
    loader.exec_module(mod)
    h = tmp_path / "home"
    for d in ("agent", ".secrets/gate-snapshots", "logs"):
        (h / d).mkdir(parents=True)
    mod.HOME = str(h)
    mod.REGISTRY = str(h / ".secrets" / ".backup-registry.jsonl")
    mod.LATCH = str(h / ".maint-ops" / "auto")
    mod.LOG = str(h / "logs" / "sweep.log")
    mod.VAULT = str(h / ".secrets" / "gate-snapshots")
    mod._LF.clear()
    monkeypatch.setattr(mod, "langfuse_dead", lambda env: True)
    monkeypatch.setattr(mod, "port_refused", lambda addr: False)
    monkeypatch.setattr(mod, "aq_add", lambda *a, **k: None)
    mod.h = h
    return mod


def put(path, text):
    path.write_text(text)
    os.chmod(path, 0o600)
    return str(path)


def gitea(m, monkeypatch, live=(CUR,), old_live=()):
    monkeypatch.setitem(m.PROBES, "gitea", lambda ctx, v: "live" if v in tuple(live) + tuple(old_live) else "revoked")


def run(m, path, ttl=-1):
    live, creds = m.live_index()
    return m.classify(path, m.Prober(), ttl, m.load_registry(), live, creds)


def kinds(r):
    return [v.split(":")[0] for _, _, v in r["values"]]


def mk(m, bak_text, name=".env.bak.20200101_000000_t", live_text="GITEA_TOKEN=%s\n" % CUR):
    put(m.h / "agent" / ".env", live_text)
    return put(m.h / "agent" / name, bak_text)


def test_identical_to_live_is_redundant_and_eligible(m, monkeypatch):
    r = run(m, mk(m, "GITEA_TOKEN=%s\n" % CUR))
    assert kinds(r) == ["REDUNDANT"] and r["verdict"] == "ELIGIBLE" and r["expired"]


def test_cp_a_old_mtime_does_not_make_a_fresh_backup_expire(m):
    """cp -a keeps the SOURCE mtime. A rollback copy made a minute ago must not look 40 days old."""
    p = mk(m, "GITEA_TOKEN=%s\n" % CUR)
    old = time.time() - 40 * 86400
    os.utime(p, (old, old))
    assert run(m, p, ttl=7)["expired"] is False


def test_future_filename_stamp_never_shortens_life(m):
    p = mk(m, "GITEA_TOKEN=%s\n" % CUR, name=".env.bak.20990101_000000")
    assert run(m, p, ttl=-1)["expired"] is False


def test_revoked_value_is_eligible(m, monkeypatch):
    gitea(m, monkeypatch)
    r = run(m, mk(m, "GITEA_TOKEN=%s\n" % OLD))
    assert kinds(r) == ["REVOKED"] and r["verdict"] == "ELIGIBLE"


def test_always_401_probe_cannot_prove_anything(m, monkeypatch):
    """A probe that rejects EVERYTHING (blocked path, wrong endpoint) must not 'prove' a live key dead."""
    monkeypatch.setitem(m.PROBES, "gitea", lambda ctx, v: "revoked")
    r = run(m, mk(m, "GITEA_TOKEN=%s\n" % OLD))
    assert r["verdict"] == "HOLD" and kinds(r) == ["UNPROVEN"]


def test_accept_everything_probe_cannot_prove_anything(m, monkeypatch):
    monkeypatch.setitem(m.PROBES, "gitea", lambda ctx, v: "live")
    assert run(m, mk(m, "GITEA_TOKEN=%s\n" % OLD))["verdict"] == "HOLD"


def test_orphan_live_is_kept_even_when_apply_confirmed(m, monkeypatch):
    gitea(m, monkeypatch, old_live=(OLD,))
    p = mk(m, "GITEA_TOKEN=%s\n" % OLD)
    assert run(m, p)["verdict"] == "ORPHAN_LIVE"
    m.cmd_apply(argparse.Namespace(ttl=-1, confirm=True, no_probe=False))
    assert os.path.exists(p)


def test_value_live_under_another_key_is_in_use_not_orphan(m, monkeypatch):
    """The 'old' LiteLLM master key that is now the ops-emergency key: renamed, still live, not a leak."""
    gitea(m, monkeypatch, old_live=(OLD,))
    r = run(m, mk(m, "GITEA_TOKEN=%s\n" % OLD, live_text="GITEA_TOKEN=%s\nLEGACY_SERVICE_KEY=%s\n" % (CUR, OLD)))
    assert kinds(r) == ["IN_USE"] and r["verdict"] == "ELIGIBLE"


def test_unknown_credential_family_is_held(m):
    r = run(m, mk(m, "UNKNOWN_API_KEY=%s\n" % OTHER))
    assert r["verdict"] == "HOLD" and kinds(r) == ["UNPROVEN"]


def test_one_unprovable_value_holds_the_whole_file(m, monkeypatch):
    gitea(m, monkeypatch)
    assert run(m, mk(m, "GITEA_TOKEN=%s\nUNKNOWN_API_KEY=%s\n" % (OLD, OTHER)))["verdict"] == "HOLD"


def test_apply_shreds_only_eligible_and_expired(m, monkeypatch):
    gitea(m, monkeypatch)
    ok = mk(m, "GITEA_TOKEN=%s\n" % OLD, name=".env.bak.20200101_000001_a")
    hold = put(m.h / "agent" / ".env.bak.20200101_000002_b", "UNKNOWN_API_KEY=%s\n" % OTHER)
    m.cmd_apply(argparse.Namespace(ttl=7, confirm=True, no_probe=False))      # nothing expired yet
    assert os.path.exists(ok) and os.path.exists(hold)
    m.cmd_apply(argparse.Namespace(ttl=-1, confirm=False, no_probe=False))    # dry-run
    assert os.path.exists(ok)
    m.cmd_apply(argparse.Namespace(ttl=-1, confirm=True, no_probe=False))
    assert not os.path.exists(ok) and os.path.exists(hold)


def test_apply_refuses_while_a_rotation_journal_exists(m, monkeypatch):
    gitea(m, monkeypatch)
    p = mk(m, "GITEA_TOKEN=%s\n" % OLD)
    put(m.h / ".secrets" / ".rotate-provider-keys.inprogress", "x")
    m.cmd_apply(argparse.Namespace(ttl=-1, confirm=True, no_probe=False))
    assert os.path.exists(p)


def test_output_never_contains_a_secret_value(m, monkeypatch, capsys):
    gitea(m, monkeypatch, old_live=(OLD,))
    mk(m, "GITEA_TOKEN=%s\nUNKNOWN_API_KEY=%s\n" % (OLD, OTHER))
    for js in (False, True):
        m.cmd_report(argparse.Namespace(json=js, aq=False, ttl=7, no_probe=False))
    m.cmd_scan(argparse.Namespace(ttl=-1))
    out = capsys.readouterr()
    for v in (CUR, OLD, OTHER):
        assert v not in out.out + out.err


def test_json_stdout_is_pure_json(m, monkeypatch, capsys):
    import json
    mk(m, "GITEA_TOKEN=%s\n" % CUR)
    m.cmd_report(argparse.Namespace(json=True, aq=False, ttl=7, no_probe=False))
    json.loads(capsys.readouterr().out)


def test_register_extends_life_and_is_idempotent_and_never_raises(m):
    p = mk(m, "GITEA_TOKEN=%s\n" % CUR)
    assert m.register(p, 30, "test", "why") and m.register(p, 30, "test", "again")
    assert run(m, p, ttl=-1)["expired"] is False and run(m, p, ttl=-1)["registered"] is True
    assert m.cmd_register(argparse.Namespace(path=str(m.h / "nope"), ttl="7d", owner=None, why=None, strict=False)) == 0
    assert oct(os.stat(m.REGISTRY).st_mode & 0o777) == "0o600"


def test_scan_counts_overdue(m):
    mk(m, "GITEA_TOKEN=%s\n" % CUR)
    assert m.cmd_scan(argparse.Namespace(ttl=-1)) == 1
    assert m.cmd_scan(argparse.Namespace(ttl=7)) == 0


def test_code_backup_assignments_are_not_credentials_but_literals_are(m):
    put(m.h / ".secrets" / "probe.py.bak.20200101_000000", 'key = "abcdefghijklmnop1234"\nSENTINEL_KEY_FILE="/x/y/zzzzzzzz"\n')
    assert run(m, str(m.h / ".secrets" / "probe.py.bak.20200101_000000"))["verdict"] == "NO_SECRETS"
    lit = put(m.h / ".secrets" / "probe2.py.bak.20200101_000000", 'T = "ghp_%s"\n' % ("a" * 36))
    assert run(m, lit)["verdict"] == "HOLD"


def test_plain_url_is_not_a_secret(m):
    assert run(m, mk(m, "NEXTAUTH_URL=http://localhost:3000/api/auth\n"))["verdict"] == "NO_SECRETS"


def test_langfuse_keys_follow_the_retired_service(m, monkeypatch):
    p = mk(m, "LANGFUSE_SECRET_KEY=lfsk_dddddddddddddddd\n")
    assert kinds(run(m, p)) == ["DEAD_SERVICE"]
    m._LF.clear(); monkeypatch.setattr(m, "langfuse_dead", lambda env: None)
    assert run(m, p)["verdict"] == "HOLD"


def test_dead_verifier_needs_the_port_to_be_closed(m, monkeypatch):
    monkeypatch.setitem(m.PROBES, "shim", lambda ctx, v: "unreachable")
    p = mk(m, "CLAUDE_SHIM_API_KEY=%s\n" % OLD, live_text="CLAUDE_SHIM_API_KEY=%s\n" % CUR)
    assert run(m, p)["verdict"] == "HOLD"
    monkeypatch.setattr(m, "port_refused", lambda addr: True)
    assert kinds(run(m, p)) == ["DEAD_VERIFIER"]


def test_litellm_sk_family_can_raise_live_but_never_revoked(m, monkeypatch):
    sk = "sk-" + "e" * 30
    monkeypatch.setattr(m, "litellm_keyinfo_known", lambda v: False)   # unidentified origin, see wq#4653 tests below
    monkeypatch.setitem(m.PROBES, "litellm-sk", lambda ctx, v: "live" if v in (CUR, sk) else "revoked")
    put(m.h / "agent" / ".env", "LITELLM_MASTER_KEY=%s\n" % CUR)
    p = put(m.h / ".secrets" / "x.py.bak.20200101_000000", 'K = "%s"\n' % sk)
    assert run(m, p)["verdict"] == "ORPHAN_LIVE"
    monkeypatch.setitem(m.PROBES, "litellm-sk", lambda ctx, v: "live" if v == CUR else "revoked")
    assert run(m, p)["verdict"] == "HOLD"      # rejected here proves nothing: it may belong to another provider


def test_cfuser_probe_reads_json_success_field_not_status_code(m, monkeypatch):
    """wq#4653: Cloudflare's /user/tokens/verify answers a non-401 status for a garbage token too (seen live:
    HTTP 400 with success:false, code 1000) -- only the body's `success` field may decide live vs revoked."""
    def fake_http_full(url, headers, timeout=6):
        token = headers["Authorization"].split()[1]
        if token == CUR:
            return 200, b'{"success":true,"result":{"id":"x"}}'
        return 400, b'{"success":false,"errors":[{"code":1000,"message":"Invalid API Token"}]}'
    monkeypatch.setattr(m, "_http_full", fake_http_full)
    ctx = {"base": "https://api.cloudflare.com/client/v4/user/tokens/verify"}
    assert m.PROBES["cfuser"](ctx, CUR) == "live"
    assert m.PROBES["cfuser"](ctx, OLD) == "revoked"


def test_cfat_prefix_routes_to_cfuser_with_mailbot_token_as_control(m, monkeypatch):
    """wq#4653: cfat_ (API token) must route to the same family as cfut_ (user token); the mailbot deploy
    token (no cfut_/cfat_ prefix) is the only live CF credential on record and must serve as its control."""
    monkeypatch.setitem(m.PROBES, "cfuser", lambda ctx, v: "live" if v == CUR else "revoked")
    (m.h / "mailbot").mkdir()
    put(m.h / "mailbot" / ".cf_deploy_token", CUR + "\n")
    p = put(m.h / ".secrets" / "cf-worker.token.bak.20200101_000000", "cfat_" + "x" * 25)
    r = run(m, p)
    assert kinds(r) == ["REVOKED"] and r["verdict"] == "ELIGIBLE"


def test_gemini_api_key_family_live_and_revoked(m, monkeypatch):
    monkeypatch.setitem(m.PROBES, "gemini", lambda ctx, v: "live" if v == CUR else "revoked")
    p = mk(m, "GEMINI_API_KEY=%s\n" % OLD, live_text="GEMINI_API_KEY=%s\n" % CUR)
    assert kinds(run(m, p)) == ["REVOKED"] and run(m, p)["verdict"] == "ELIGIBLE"


def test_gemini_accept_everything_probe_cannot_prove_anything(m, monkeypatch):
    """A probe that accepts EVERYTHING fails its own negative control (a random value must be rejected to
    be trusted), so it must not be able to call anything LIVE or REVOKED -- stays HOLD."""
    monkeypatch.setitem(m.PROBES, "gemini", lambda ctx, v: "live")
    p = mk(m, "GEMINI_API_KEY=%s\n" % OLD, live_text="GEMINI_API_KEY=%s\n" % CUR)
    assert run(m, p)["verdict"] == "HOLD"


def test_mcpw_pool_health_family(m, monkeypatch):
    monkeypatch.setitem(m.PROBES, "mcpw", lambda ctx, v: "live" if v == CUR else "revoked")
    put(m.h / ".secrets" / "mcpw-pool-health.env", "MCPW_POOL_HEALTH_SECRET=%s\n" % CUR)
    p = mk(m, "MCPW_POOL_HEALTH_SECRET=%s\n" % OLD)
    assert kinds(run(m, p)) == ["REVOKED"] and run(m, p)["verdict"] == "ELIGIBLE"


def test_mongo_backup_password_follows_retired_container(m, monkeypatch):
    monkeypatch.setattr(m, "mongo_dead", lambda: True)
    p = mk(m, "MONGO_BACKUP_PASSWORD=%s\n" % OLD)
    assert kinds(run(m, p)) == ["DEAD_SERVICE"]
    m._MONGO.clear(); monkeypatch.setattr(m, "mongo_dead", lambda: False)
    assert run(m, p)["verdict"] == "HOLD"


def test_mongo_json_url_credential_follows_retired_container(m, monkeypatch):
    """wq#4653: a JSON config backup holding a mongodb:// connection string must route through the same
    retired-container check as MONGO_BACKUP_PASSWORD, by connection scheme rather than key name."""
    monkeypatch.setattr(m, "mongo_dead", lambda: True)
    p = put(m.h / ".secrets" / "app-config.json.bak.20200101_000000",
            '{"uri": "mongodb://dbuser:%s@10.0.0.5:27017/admin"}' % OLD)
    assert kinds(run(m, p)) == ["DEAD_SERVICE"]


def test_litellm_sk_keyinfo_upgrades_a_dead_litellm_key_to_revoked(m, monkeypatch):
    """wq#4653: LiteLLM's /v1/models rejecting an sk- literal cannot alone prove it dead (litellm-sk,
    LIVE_ONLY) -- but if /key/info says the key IS known to litellm's VerificationTokenTable, rejection
    means a dead litellm key, not an unidentified foreign one."""
    sk = "sk-" + "e" * 30
    monkeypatch.setitem(m.PROBES, "litellm-sk", lambda ctx, v: "live" if v == CUR else "revoked")
    monkeypatch.setattr(m, "litellm_keyinfo_known", lambda v: True)
    put(m.h / "agent" / ".env", "LITELLM_MASTER_KEY=%s\n" % CUR)
    p = put(m.h / ".secrets" / "x.py.bak.20200101_000000", 'K = "%s"\n' % sk)
    assert kinds(run(m, p)) == ["REVOKED"]


def test_litellm_sk_keyinfo_leaves_a_foreign_key_unproven(m, monkeypatch):
    sk = "sk-" + "e" * 30
    monkeypatch.setitem(m.PROBES, "litellm-sk", lambda ctx, v: "live" if v == CUR else "revoked")
    monkeypatch.setattr(m, "litellm_keyinfo_known", lambda v: False)
    put(m.h / "agent" / ".env", "LITELLM_MASTER_KEY=%s\n" % CUR)
    p = put(m.h / ".secrets" / "y.py.bak.20200101_000000", 'K = "%s"\n' % sk)
    assert run(m, p)["verdict"] == "HOLD"


def test_cli_smoke_on_empty_home(tmp_path):
    h = tmp_path / "h"; (h / ".secrets").mkdir(parents=True)
    r = subprocess.run([sys.executable, SRC, "scan"], env={**os.environ, "SBS_HOME": str(h)}, capture_output=True, text=True)
    assert r.returncode == 0 and "secret_bearing=0 overdue=0" in r.stdout


def test_gitea_rotate_finish_registers_every_backup_copy():
    """wq#4599 fix 1: a future edit must not add a pre-rotate backup that nobody registers."""
    import re
    src = open(os.path.join(os.path.dirname(SRC), "gitea-rotate-finish")).read().splitlines()
    cps = [i for i, l in enumerate(src) if re.search(r"cp -a .*\.bak\.\$\{STAMP\}_pre_rotate", l)]
    assert len(cps) == 4, "expected the 4 known pre-rotate backups, found %d" % len(cps)
    for i in cps:
        assert "reg_bak" in src[i + 1], "backup written at line %d is never registered" % (i + 1)


def test_lumen_secret_check_pages_only_for_a_real_pat_not_for_backup_debt():
    text = open(os.path.join(os.path.dirname(SRC), "lumen-secret-check")).read()
    assert 'if [ -n "$FOUND" ]; then PRI=high' in text and "PRI=default" in text
