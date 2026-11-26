"""wq#5329: `unit-drift accept` run as root (ops/*/install.sh via `lumen-run --root`)
failed because git refused the lumen-owned /home/lumen/fleet-units repo
("dubious ownership", safe.directory). The fix: every git call carries
-c safe.directory=<REPO>, git runs AS the repo owner when euid is 0, and
snapshot/accept hand any root-owned path they wrote back to the owner.

The root tests drive the real script as root (sudo -n) against a lumen-owned
fixture repo, never the live /home/lumen/fleet-units. They skip only when
passwordless sudo is unavailable.
"""
import importlib.machinery, importlib.util, os, subprocess, sys
import pytest

PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "unit-drift")
REAL_RUN = subprocess.run


def load(sysdir, repo):
    os.environ["UNIT_DRIFT_SYS"] = str(sysdir)
    os.environ["UNIT_DRIFT_REPO"] = str(repo)
    loader = importlib.machinery.SourceFileLoader("unit_drift_under_test_5329", PATH)
    spec = importlib.util.spec_from_loader(loader.name, loader)
    m = importlib.util.module_from_spec(spec); loader.exec_module(m)
    return m


def g(repo, *args):
    return REAL_RUN(["git", "-C", str(repo), *args], capture_output=True, text=True)


def have_sudo():
    return os.geteuid() != 0 and REAL_RUN(["sudo", "-n", "true"], capture_output=True).returncode == 0


needs_root = pytest.mark.skipif(not have_sudo(), reason="needs passwordless sudo and a non-root test user")


def as_root(*cmd, env=None):
    # -u SUDO_UID/SUDO_GID/SUDO_USER: git trusts a repo owned by SUDO_UID, which hides the
    # bug under sudo; the real caller (lumen-run --root = a systemd unit) has no SUDO_*.
    # HOME=/root mirrors that unit too.
    pre = ["sudo", "-n", "env", "-u", "SUDO_UID", "-u", "SUDO_GID", "-u", "SUDO_USER",
           "HOME=/root"] + ["%s=%s" % kv for kv in (env or {}).items()]
    return REAL_RUN(pre + list(cmd), capture_output=True, text=True)


def root_owned(repo):
    out = []
    for root, dirs, files in os.walk(repo):
        for name in dirs + files:
            p = os.path.join(root, name)
            if os.lstat(p).st_uid == 0:
                out.append(os.path.relpath(p, repo))
    return out


@pytest.fixture
def fx(tmp_path, monkeypatch):
    sysdir = tmp_path / "sys"; sysdir.mkdir()
    (sysdir / "a.service").write_text("[Unit]\nDescription=a\n")
    (sysdir / "a.service.d").mkdir()
    (sysdir / "a.service.d" / "override.conf").write_text("[Service]\n")
    wants = sysdir / "multi-user.target.wants"; wants.mkdir()
    (wants / "a.service").symlink_to("/etc/systemd/system/a.service")
    repo = tmp_path / "repo"; repo.mkdir()
    g(repo, "init", "-q")
    g(repo, "config", "user.email", "test@test"); g(repo, "config", "user.name", "test")
    mod = load(sysdir, repo)
    # baseline snapshot as the (non-root) test user; stub ONLY the live-fleet
    # cron-stamp generator, git stays real
    def guarded(args, **kw):
        if "lumen-cron-stamp-dropin-gen" in args[0]:
            return subprocess.CompletedProcess(args, 0)
        return REAL_RUN(args, **kw)
    monkeypatch.setattr(mod.subprocess, "run", guarded)
    assert mod.cmd_snapshot() == 0
    mod.sysdir, mod.repo = sysdir, repo
    mod.env = {"UNIT_DRIFT_SYS": str(sysdir), "UNIT_DRIFT_REPO": str(repo)}
    return mod


# ---- unit level (no root needed) -------------------------------------------

def test_every_git_call_carries_safe_directory(fx, monkeypatch):
    seen = []
    monkeypatch.setattr(fx.subprocess, "run", lambda cmd, **kw: seen.append((cmd, kw)) or
                        subprocess.CompletedProcess(cmd, 0, "", ""))
    fx.git("status", "--porcelain")
    cmd, kw = seen[-1]
    assert cmd[:3] == ["git", "-c", "safe.directory=" + os.path.realpath(str(fx.repo))]
    assert "user" not in kw                      # non-root: no privilege change


def test_as_root_git_runs_as_repo_owner(fx, monkeypatch):
    seen = []
    monkeypatch.setattr(fx.os, "geteuid", lambda: 0)
    monkeypatch.setattr(fx.subprocess, "run", lambda cmd, **kw: seen.append((cmd, kw)) or
                        subprocess.CompletedProcess(cmd, 0, "", ""))
    fx.git("status", "--porcelain")
    cmd, kw = seen[-1]
    st = os.stat(fx.repo)
    assert "safe.directory=" + os.path.realpath(str(fx.repo)) in cmd
    assert kw["user"] == st.st_uid and kw["group"] == st.st_gid and kw["extra_groups"] == []
    assert kw["env"]["HOME"] == os.path.expanduser("~")   # owner's HOME, not root's
    assert kw["cwd"] == str(fx.repo)


def test_restore_is_noop_when_not_root(fx):
    assert fx.restore_repo_ownership() == 0


# ---- real root runs against the fixture repo -------------------------------

@needs_root
def test_fixture_reproduces_dubious_ownership_without_the_fix(fx):
    """Precondition: plain root git really is refused on this lumen-owned repo,
    so the accept test below exercises the wq#5329 failure, not a lucky config."""
    r = as_root("git", "-C", str(fx.repo), "status", "--porcelain")
    assert r.returncode != 0 and "dubious ownership" in r.stderr


@needs_root
def test_root_accept_commits_and_leaves_no_root_owned_files(fx):
    (fx.sysdir / "a.service").write_text("[Unit]\nDescription=a EDITED\n")
    (fx.sysdir / "c.service").write_text("[Unit]\nDescription=c new\n")
    (fx.sysdir / "c.service.d").mkdir()                              # new dir in repo as root
    (fx.sysdir / "c.service.d" / "x.conf").write_text("[Service]\n")
    head = g(fx.repo, "rev-parse", "HEAD").stdout
    r = as_root(sys.executable, PATH, "accept", "--ticket", "wq#5329", "a.service", "c.service",
                env=fx.env)
    assert r.returncode == 0, r.stdout + r.stderr
    assert "accept wq#5329: a.service c.service" in r.stdout
    assert g(fx.repo, "rev-parse", "HEAD").stdout != head
    assert g(fx.repo, "log", "-1", "--format=%s").stdout.strip() == "accept wq#5329: a.service c.service"
    assert root_owned(fx.repo) == []                                   # incl. .git objects/index
    assert (fx.repo / "dropins" / "c.service.d" / "x.conf").exists()
    # the owner can keep using the repo normally afterwards
    st = g(fx.repo, "status", "--porcelain")
    assert st.returncode == 0 and st.stdout.strip() == ""


@needs_root
def test_root_accept_noop_still_clean(fx):
    r = as_root(sys.executable, PATH, "accept", "--ticket", "wq#5329", "a.service", env=fx.env)
    assert r.returncode == 0, r.stdout + r.stderr
    assert "baseline already matches" in r.stdout
    assert root_owned(fx.repo) == []


@needs_root
def test_restore_sweep_hands_root_files_back(fx):
    stray = fx.repo / "units" / "rootfile"
    assert as_root("touch", str(stray)).returncode == 0
    assert os.stat(stray).st_uid == 0
    code = ("import importlib.machinery as M, importlib.util as U;"
            "l=M.SourceFileLoader('ud',%r);m=U.module_from_spec(U.spec_from_loader('ud',l));"
            "l.exec_module(m);print(m.restore_repo_ownership())" % PATH)
    r = as_root(sys.executable, "-c", code, env=fx.env)
    assert r.returncode == 0, r.stderr
    assert r.stdout.strip() == "1"
    assert os.stat(stray).st_uid == os.stat(fx.repo).st_uid
    assert root_owned(fx.repo) == []
