"""wq#2514: rotate-github-cred rewrite/revoke logic with the GitHub API mocked."""
import importlib.machinery, importlib.util, os, pathlib, pytest

HERE = pathlib.Path(__file__).resolve().parent.parent
OLD = "ghp_" + "a" * 36
NEW = "ghp_" + "b" * 36
DEAD = "github_pat_" + "c" * 82
OTHER = "ghp_" + "d" * 36   # a different login's live token must be left alone


def load(tmp, monkeypatch, revoke_rc=202, revoked=True):
    monkeypatch.setenv("GH_ROT_CRED", str(tmp / "gc"))
    monkeypatch.setenv("GH_ROT_CANON", str(tmp / "canon"))
    loader = importlib.machinery.SourceFileLoader("rgc", str(HERE / "rotate-github-cred"))
    spec = importlib.util.spec_from_loader("rgc", loader)
    m = importlib.util.module_from_spec(spec); loader.exec_module(m)
    state = {"revoked": set()}
    def gh_user(t):
        if t in state["revoked"] or t == DEAD: return 401, None, set()
        if t in (OLD, NEW): return 200, "RealLumenHere", {"repo", "workflow"}
        if t == OTHER: return 200, "frankstupak", {"repo"}
        return 401, None, set()
    def revoke(toks):
        if revoked: state["revoked"].update(toks)
        return revoke_rc
    monkeypatch.setattr(m, "gh_user", gh_user); monkeypatch.setattr(m, "revoke", revoke)
    monkeypatch.setattr(m.os, "uname", lambda: os.uname_result(("Linux", "lumen", "", "", "")))
    monkeypatch.setattr(m.time, "sleep", lambda s: None)
    return m, state


def store(tmp):
    (tmp / "gc").write_text(
        f"http://lumen:{'e'*40}@100.121.17.5%3a3000\n"
        f"https://RealLumenHere:{OLD}@github.com\n"
        f"https://x-access-token:{OLD}@github.com\n"
        f"https://x:{DEAD}@github.com\n"
        f"https://frankstupak:{OTHER}@github.com\n")
    os.chmod(tmp / "gc", 0o600)
    n = tmp / "new"; n.write_text(NEW + "\n"); os.chmod(n, 0o600)
    return n


def test_happy_path(tmp_path, monkeypatch):
    m, st = load(tmp_path, monkeypatch); n = store(tmp_path)
    assert m.main([str(n)]) == 0
    lines = (tmp_path / "gc").read_text().splitlines()
    assert lines[0].startswith("http://lumen:")                     # gitea untouched
    assert f"https://RealLumenHere:{NEW}@github.com" in lines
    assert f"https://x-access-token:{NEW}@github.com" in lines
    assert not any(DEAD in l for l in lines) and not any(OLD in l for l in lines)
    assert f"https://frankstupak:{OTHER}@github.com" in lines       # other login kept
    assert OLD in st["revoked"] and OTHER not in st["revoked"]
    assert (tmp_path / "canon").read_text().strip() == NEW and not n.exists()
    assert oct((tmp_path / "gc").stat().st_mode)[-3:] == "600"
    assert list(tmp_path.glob("gc.bak.*_pre_github_rotate"))


def test_rerun_is_noop(tmp_path, monkeypatch):
    m, st = load(tmp_path, monkeypatch); n = store(tmp_path)
    assert m.main([str(n)]) == 0
    before = (tmp_path / "gc").read_text()
    n.write_text(NEW + "\n"); os.chmod(n, 0o600)
    assert m.main([str(n)]) == 0
    assert (tmp_path / "gc").read_text() == before


def test_revoke_refused_is_loud(tmp_path, monkeypatch):
    m, _ = load(tmp_path, monkeypatch, revoke_rc=422); n = store(tmp_path)
    with pytest.raises(SystemExit) as e: m.main([str(n)])
    assert e.value.code == 1


def test_revoke_accepted_but_still_live_is_loud(tmp_path, monkeypatch):
    m, _ = load(tmp_path, monkeypatch, revoked=False); n = store(tmp_path)
    t = iter(range(0, 10000, 30)); monkeypatch.setattr(m.time, "time", lambda: next(t))
    with pytest.raises(SystemExit) as e: m.main([str(n)])
    assert e.value.code == 1


def test_wrong_login_changes_nothing(tmp_path, monkeypatch):
    m, _ = load(tmp_path, monkeypatch); n = store(tmp_path)
    n.write_text(OTHER + "\n"); os.chmod(n, 0o600)
    before = (tmp_path / "gc").read_text()
    with pytest.raises(SystemExit): m.main([str(n)])
    assert (tmp_path / "gc").read_text() == before
