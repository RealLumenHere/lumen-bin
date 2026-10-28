"""wq#4765 (agent-authority DESIGN.md §2.4): `lumen-run start --bind-ro PATH` renders
BindReadOnlyPaths= with no '-' prefix (missing source fails unit start, not silently
skipped) and refuses up front if the path isn't already in its canonical (realpath'd)
form — the check that catches a symlink swapped between approval and this spawn.

`sudo`/`hostname` are stubbed on PATH so this suite never touches the real system
systemd manager (hermetic, same style as test_lumen_run_wait_cap.py); the stub logs
every argv it was called with so assertions can inspect the rendered --property=
list without needing a live unit to actually run. The real OS-level behavior this
unit test can't exercise (an EROFS write inside the bind, a missing source actually
failing systemd's NAMESPACE step) was verified live against the real `sudo -n
systemd-run` on this box — see wq4765_cross_repo_evidence.md.
"""
import os
import stat
import subprocess

import pytest

BIN = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "lumen-run")


@pytest.fixture
def stubs(tmp_path):
    d = tmp_path / "stubs"
    d.mkdir()
    calls = tmp_path / "sudo-calls.log"
    # sudo -n systemd-run --unit=... --collect <runas> <props...> /bin/bash -c <wrapped>
    # logs its full argv (one call per line, space-joined) and exits 0 — the script
    # treats a successful `sudo systemd-run` as "unit registered", the only thing this
    # suite needs; whether the unit then actually STARTS is a live-systemd question
    # this stub can't answer (see the module docstring).
    (d / "sudo").write_text('#!/bin/sh\necho "$@" >> "%s"\nexit 0\n' % calls)
    (d / "hostname").write_text("#!/bin/sh\necho lumen\n")
    for f in d.iterdir():
        f.chmod(f.stat().st_mode | stat.S_IEXEC)
    return d, calls


def run_start(stubs, *args):
    d, calls = stubs
    env = {**os.environ, "PATH": f"{d}:{os.environ['PATH']}"}
    p = subprocess.run(["bash", BIN, "start", *args], capture_output=True, text=True,
                       env=env, timeout=30)
    logged = calls.read_text() if calls.exists() else ""
    return p, logged


def test_bind_ro_renders_as_bindreadonlypaths_with_no_dash_prefix(stubs, tmp_path):
    target = tmp_path / "granted"
    target.mkdir()
    canon = str(target.resolve())
    p, logged = run_start(stubs, "--name", "t", "--bind-ro", canon, "--", "true")
    assert p.returncode == 0 and "UNIT=" in p.stdout
    assert f"--property=BindReadOnlyPaths={canon}" in logged
    # F6's fail-closed half depends on this NOT being the '-' (ignore-if-missing) form.
    assert f"--property=-BindReadOnlyPaths={canon}" not in logged


def test_bind_ro_is_repeatable_as_one_property_per_path(stubs, tmp_path):
    a = tmp_path / "a"; a.mkdir()
    b = tmp_path / "b"; b.mkdir()
    ca, cb = str(a.resolve()), str(b.resolve())
    p, logged = run_start(stubs, "--name", "t", "--bind-ro", ca, "--bind-ro", cb, "--", "true")
    assert p.returncode == 0
    assert f"--property=BindReadOnlyPaths={ca}" in logged
    assert f"--property=BindReadOnlyPaths={cb}" in logged


def test_no_bind_ro_means_no_bindreadonlypaths_property(stubs):
    p, logged = run_start(stubs, "--name", "t", "--", "true")
    assert p.returncode == 0
    assert "BindReadOnlyPaths" not in logged


def test_bind_ro_refuses_a_symlinked_noncanonical_path_before_calling_sudo(stubs, tmp_path):
    real = tmp_path / "real"; real.mkdir()
    link = tmp_path / "link"
    os.symlink(real, link)
    p, logged = run_start(stubs, "--name", "t", "--bind-ro", str(link), "--", "true")
    assert p.returncode == 65
    assert "not canonical" in p.stderr
    assert str(real.resolve()) in p.stderr
    # F6: a rejected grant must never reach systemd-run at all.
    assert logged == ""
    assert "UNIT=" not in p.stdout


def test_bind_ro_passes_canonical_path_through_unchanged(stubs, tmp_path):
    target = tmp_path / "granted"
    target.mkdir()
    canon = str(target.resolve())
    # already canonical (no symlink in it) -> must NOT be refused.
    p, logged = run_start(stubs, "--name", "t", "--bind-ro", canon, "--", "true")
    assert p.returncode == 0
    assert "not canonical" not in p.stderr


def test_bind_ro_lets_a_missing_source_reach_sudo_unrefused(stubs, tmp_path):
    # wq#4765 ticket: "a missing source fails unit start" — that is systemd's job
    # (no '-' prefix -> NAMESPACE failure), not lumen-run's; this script must not
    # pre-empt it by refusing a nonexistent-but-otherwise-canonical path itself.
    missing = str(tmp_path / "does-not-exist-yet")
    p, logged = run_start(stubs, "--name", "t", "--bind-ro", missing, "--", "true")
    assert p.returncode == 0
    assert f"--property=BindReadOnlyPaths={missing}" in logged
