"""wq#3579: `accept --ticket <id> <unit>...` blesses the baseline for the NAMED
units only. The core property: an undeclared drifting unit SURVIVES an accept of
a declared one — a bundle deploy can never silently commit unrelated drift the
way a blanket `snapshot` would. The commit message carries the unit list and
ticket id."""
import importlib.machinery, importlib.util, os, shutil, subprocess
import pytest

PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "unit-drift")
REAL_RUN = subprocess.run


def load(sysdir, repo):
    os.environ["UNIT_DRIFT_SYS"] = str(sysdir)
    os.environ["UNIT_DRIFT_REPO"] = str(repo)
    loader = importlib.machinery.SourceFileLoader("unit_drift_under_test_3579", PATH)
    spec = importlib.util.spec_from_loader(loader.name, loader)
    m = importlib.util.module_from_spec(spec); loader.exec_module(m)
    return m


def g(repo, *args):
    return REAL_RUN(["git", "-C", str(repo), *args], capture_output=True, text=True)


@pytest.fixture
def m(tmp_path, monkeypatch):
    sysdir = tmp_path / "sys"; sysdir.mkdir()
    (sysdir / "a.service").write_text("[Unit]\nDescription=a\n")
    (sysdir / "a.service.d").mkdir()
    (sysdir / "a.service.d" / "override.conf").write_text("[Service]\n")
    (sysdir / "b.service").write_text("[Unit]\nDescription=b\n")
    (sysdir / "b.service.d").mkdir()
    (sysdir / "b.service.d" / "override.conf").write_text("[Service]\n")
    wants = sysdir / "multi-user.target.wants"; wants.mkdir()
    (wants / "a.service").symlink_to("/etc/systemd/system/a.service")
    (wants / "b.service").symlink_to("/etc/systemd/system/b.service")
    repo = tmp_path / "repo"; repo.mkdir()
    g(repo, "init", "-q")
    g(repo, "config", "user.email", "test@test"); g(repo, "config", "user.name", "test")
    mod = load(sysdir, repo)
    # cmd_snapshot shells out to lumen-cron-stamp-dropin-gen against the live
    # fleet; stub ONLY that call, every other subprocess (git) stays real
    def guarded(args, **kw):
        if "lumen-cron-stamp-dropin-gen" in args[0]:
            return subprocess.CompletedProcess(args, 0)
        return REAL_RUN(args, **kw)
    monkeypatch.setattr(mod.subprocess, "run", guarded)
    # base_unit_exists consults the REAL /etc search paths; point it at the tmp
    # sys dir so the orphan-drop-in advisory doesn't fire on test unit names
    monkeypatch.setattr(mod, "base_unit_exists", lambda b: (sysdir / b).exists())
    monkeypatch.setattr(mod, "dangling_target_lint", lambda: [])
    monkeypatch.setattr(mod, "guard_lint", lambda: [])
    monkeypatch.setattr(mod, "unexplained_masks", lambda units: [])
    assert mod.cmd_snapshot() == 0
    mod.sysdir, mod.repo = sysdir, repo
    return mod


def check_out(m, capsys):
    capsys.readouterr()                              # drop accept/snapshot output
    rc = m.cmd_check(digest=False)
    return rc, capsys.readouterr().out


def test_undeclared_drifting_unit_survives_accept_of_declared_one(m, capsys):
    (m.sysdir / "a.service").write_text("[Unit]\nDescription=a EDITED\n")
    (m.sysdir / "c.service").write_text("[Unit]\nDescription=c undeclared\n")
    assert m.cmd_accept(["--ticket", "wq#3579", "a.service"]) == 0
    rc, out = check_out(m, capsys)
    assert rc == 1                                   # c.service still drifts
    assert "ADDED unit" in out and "c.service" in out
    assert "a.service" not in out                    # a is blessed, no longer drifts


def test_commit_message_carries_ticket_and_unit_list(m):
    (m.sysdir / "a.service").write_text("[Unit]\nDescription=a EDITED\n")
    assert m.cmd_accept(["--ticket", "wq#3579", "a.service"]) == 0
    subj = g(m.repo, "log", "-1", "--format=%s").stdout.strip()
    assert subj == "accept wq#3579: a.service"


def test_accept_of_removed_unit_scopes_removal(m, capsys):
    shutil.rmtree(m.sysdir / "a.service.d"); shutil.rmtree(m.sysdir / "b.service.d")
    os.remove(m.sysdir / "multi-user.target.wants" / "a.service")
    os.remove(m.sysdir / "multi-user.target.wants" / "b.service")
    os.remove(m.sysdir / "a.service")
    os.remove(m.sysdir / "b.service")
    assert m.cmd_accept(["--ticket", "wq#3579", "a.service"]) == 0
    base = m.load_manifest()
    assert "a.service" not in base["units"]
    assert "b.service" in base["units"]              # undeclared removal still drifts
    rc, out = check_out(m, capsys)
    assert rc == 1 and "REMOVED unit" in out and "b.service" in out
    assert "a.service" not in out


def test_dropins_and_enable_state_scoped_to_named_units(m, capsys):
    (m.sysdir / "a.service.d" / "override.conf").write_text("[Service]\nNice=1\n")
    (m.sysdir / "b.service.d" / "override.conf").write_text("[Service]\nNice=2\n")
    os.remove(m.sysdir / "multi-user.target.wants" / "a.service")
    os.remove(m.sysdir / "multi-user.target.wants" / "b.service")
    assert m.cmd_accept(["--ticket", "wq#3579", "a.service"]) == 0
    base = m.load_manifest()
    assert "multi-user.target.wants/a.service" not in base["enable"]
    assert "multi-user.target.wants/b.service" in base["enable"]
    rc, out = check_out(m, capsys)
    assert rc == 1
    assert "b.service.d/override.conf" in out and "b.service" in out
    assert "a.service" not in out


def test_accept_requires_ticket_and_valid_unit_names(m):
    head = g(m.repo, "rev-parse", "HEAD").stdout
    assert m.cmd_accept(["a.service"]) == 2
    assert m.cmd_accept(["--ticket", "wq#3579"]) == 2
    assert m.cmd_accept(["--ticket", "wq#3579", "not-a-unit"]) == 2
    assert g(m.repo, "rev-parse", "HEAD").stdout == head   # nothing committed


def test_accept_refuses_dirty_baseline_repo(m):
    (m.repo / "stray.txt").write_text("uncommitted\n")
    (m.sysdir / "a.service").write_text("[Unit]\nDescription=a EDITED\n")
    assert m.cmd_accept(["--ticket", "wq#3579", "a.service"]) == 2


def test_accept_noop_when_baseline_already_matches(m):
    head = g(m.repo, "rev-parse", "HEAD").stdout
    assert m.cmd_accept(["--ticket", "wq#3579", "a.service"]) == 0
    assert g(m.repo, "rev-parse", "HEAD").stdout == head   # no empty commit
