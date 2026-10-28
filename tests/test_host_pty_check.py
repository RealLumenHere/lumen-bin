"""wq#4771 host-pty-check: Lumen ran 3 days with no devpts at /dev/pts and /dev/ptmx owned
lumen:lumen (a limpet chroot teardown), and nothing noticed. Inputs are committed
fixtures and synthetic stat results, never the live /dev, so every case is re-runnable."""
import importlib.machinery
import importlib.util
import os
import stat
import subprocess
import sys

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
SRC = os.path.join(HERE, "..", "host-pty-check")
FIX = os.path.join(HERE, "fixtures", "host_pty")
HEALTHY = os.path.join(FIX, "mountinfo.healthy")
BROKEN = os.path.join(FIX, "mountinfo.lumen-20261025")


@pytest.fixture
def m():
    loader = importlib.machinery.SourceFileLoader("host_pty_check_under_test", SRC)
    mod = importlib.util.module_from_spec(importlib.util.spec_from_loader("host_pty_check_under_test", loader))
    loader.exec_module(mod)
    return mod


class _Stat:
    def __init__(self, mode, uid, gid, rdev):
        self.st_mode, self.st_uid, self.st_gid, self.st_rdev = mode, uid, gid, rdev


def _st(uid=0, gid=0, mode=0o666, dev=(5, 2)):
    return lambda _path: _Stat(stat.S_IFCHR | mode, uid, gid, os.makedev(*dev))


def test_devpts_present(m):
    assert m.devpts_at(HEALTHY)


def test_devpts_only_under_a_chroot_is_not_the_hosts(m):
    # The 2026-10-25 shape: a devpts survives at .build/chroot/dev/pts, none at /dev/pts.
    assert not m.devpts_at(BROKEN)


def test_healthy_ptmx(m):
    assert m.ptmx_problems("/dev/ptmx", _st()) == []


def test_ptmx_group_tty_is_healthy(m):
    import grp
    try:
        tty = grp.getgrnam("tty").gr_gid
    except KeyError:
        pytest.skip("no tty group on this host")
    assert m.ptmx_problems("/dev/ptmx", _st(gid=tty)) == []


def test_ptmx_chowned_to_lumen_is_flagged(m):
    assert m.ptmx_problems("/dev/ptmx", _st(uid=1000, gid=1000)) == ["ptmx-owner=1000:1000"]


def test_ptmx_wrong_mode_is_flagged(m):
    assert m.ptmx_problems("/dev/ptmx", _st(mode=0o600)) == ["ptmx-mode=0600"]


def test_ptmx_wrong_device_is_flagged(m):
    assert m.ptmx_problems("/dev/ptmx", _st(dev=(1, 3))) == ["ptmx=/dev/ptmx:not-the-5:2-char-device"]


def test_ptmx_missing_is_flagged(m, tmp_path):
    assert m.ptmx_problems(str(tmp_path / "nope")) == [f"ptmx={tmp_path / 'nope'}:No such file or directory"]


def _cli(*args):
    return subprocess.run([sys.executable, SRC, *args], capture_output=True, text=True)


def test_cli_fails_on_the_incident_shape(tmp_path):
    # /dev/null is a real char device but not 5:2, so the ptmx half fails too.
    r = _cli("--mountinfo", BROKEN, "--ptmx", "/dev/null")
    assert r.returncode == 1
    assert r.stdout.startswith("host-pty-check: FAIL devpts-not-mounted-at-/dev/pts ptmx=/dev/null:not-the-5:2-char-device")


def test_cli_aq_files_a_row_on_fail(m, monkeypatch, capsys):
    calls = []
    monkeypatch.setattr(m.subprocess, "run", lambda argv, **kw: calls.append(argv))
    monkeypatch.setattr(m, "ptmx_problems", lambda path: [])
    assert m.main(["--mountinfo", BROKEN, "--aq"]) == 1
    assert len(calls) == 1 and calls[0][:4] == [m.AQ, "add", "host-pty", "devpts"]
    assert "devpts-not-mounted-at-/dev/pts" in calls[0][5]


def test_cli_ok_files_nothing(m, monkeypatch, capsys):
    calls = []
    monkeypatch.setattr(m.subprocess, "run", lambda argv, **kw: calls.append(argv))
    monkeypatch.setattr(m, "ptmx_problems", lambda path: [])
    assert m.main(["--mountinfo", HEALTHY, "--aq"]) == 0
    assert calls == []
    assert capsys.readouterr().out == "host-pty-check: OK\n"
