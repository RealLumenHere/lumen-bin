"""wq#3099: unit-drift must survive a root-only drop-in dir and must not report a crash as a finding."""
import importlib.machinery, importlib.util, os, subprocess
import pytest

PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "unit-drift")

def load(sysdir):
    os.environ["UNIT_DRIFT_SYS"] = str(sysdir)
    loader = importlib.machinery.SourceFileLoader("unit_drift_under_test", PATH)
    spec = importlib.util.spec_from_loader(loader.name, loader); m = importlib.util.module_from_spec(spec); loader.exec_module(m)
    return m

@pytest.fixture
def sysdir(tmp_path):
    (tmp_path / "a.service").write_text("[Unit]\n")
    (tmp_path / "a.service.d").mkdir(); (tmp_path / "a.service.d" / "10-x.conf").write_text("[Service]\n")
    (tmp_path / "secret.service.d").mkdir(); (tmp_path / "secret.service.d" / "10-tok.conf").write_text("[Service]\n")
    return tmp_path

def deny(m, monkeypatch, sysdir, sudo_ok, listing="10-tok.conf\n"):
    real = os.listdir
    monkeypatch.setattr(m.os, "listdir", lambda p: (_ for _ in ()).throw(PermissionError(13, "denied", str(p))) if str(p).endswith("secret.service.d") else real(p))
    monkeypatch.setattr(m.os, "access", lambda p, mode: False if str(p).endswith("secret.service.d") else True)
    real_run = subprocess.run
    def fake(args, **kw):
        if args[:3] == ["sudo", "-n", "ls"]:
            return subprocess.CompletedProcess(args, 0 if sudo_ok else 1, stdout=listing if sudo_ok else "", stderr="")
        if args[:2] == ["sudo", "cat"]:
            return subprocess.CompletedProcess(args, 0, stdout=b"[Service]\nEnvironment=TOKEN=x\n", stderr=b"")
        return real_run(args, **kw)
    monkeypatch.setattr(m.subprocess, "run", fake)

def test_unreadable_dir_is_read_through_sudo(sysdir, monkeypatch):
    m = load(sysdir); deny(m, monkeypatch, sysdir, sudo_ok=True)
    cur = m.scan()
    assert "secret.service.d/10-tok.conf" in cur["dropins"] and "a.service.d/10-x.conf" in cur["dropins"]
    assert not any("UNREADABLE" in k for k in cur["dropins"])

def test_unreadable_dir_without_sudo_is_reported_not_raised(sysdir, monkeypatch):
    m = load(sysdir); deny(m, monkeypatch, sysdir, sudo_ok=False)
    cur = m.scan()                                     # used to raise PermissionError
    assert cur["dropins"]["secret.service.d/<UNREADABLE>"] == "unreadable"
    assert "a.service.d/10-x.conf" in cur["dropins"]   # the rest of the scan still happened

def test_crash_exits_3_not_1(sysdir, monkeypatch, capsys):
    m = load(sysdir)
    monkeypatch.setattr(m, "main", lambda: (_ for _ in ()).throw(PermissionError("boom")))
    assert m.run() == 3
    assert "NOT a drift finding" in capsys.readouterr().err

def test_normal_exit_codes_unchanged(sysdir, monkeypatch):
    m = load(sysdir)
    monkeypatch.setattr(m, "main", lambda: 1); assert m.run() == 1
    monkeypatch.setattr(m, "main", lambda: None); assert m.run() == 0
