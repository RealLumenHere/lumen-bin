"""fleet-exec-restore (BROKER_WIRING.md §2.4.2): the healer's restore path for a
`<unit>@<node>` holddown. subprocess is monkeypatched — no real ssh.

Run: python3 -m pytest tests/test_fleet_exec_restore.py -q
"""
import importlib.util
import os
import sys
from importlib.machinery import SourceFileLoader

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
BIN_DIR = os.path.join(HERE, "..")
sys.path.insert(0, BIN_DIR)

_loader = SourceFileLoader("fleet_exec_restore", os.path.join(BIN_DIR, "fleet-exec-restore"))
_spec = importlib.util.spec_from_loader(_loader.name, _loader)
restore = importlib.util.module_from_spec(_spec)
_loader.exec_module(restore)


class Result:
    def __init__(self, returncode=0, stdout="", stderr=""):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


@pytest.fixture
def calls(monkeypatch):
    seen = {"argv": [], "rc": 0}

    def fake_run(argv, **kw):
        seen["argv"].append(argv)
        return Result(returncode=seen["rc"])

    monkeypatch.setattr(restore.subprocess, "run", fake_run)
    return seen


def test_remote_unit_is_started_over_ssh_with_sudo_n(calls):
    restore.main(["isis", "ollama"])
    argv = calls["argv"][0]
    assert argv[0] == "ssh" and "isis" in argv
    assert argv[-5:] == ["sudo", "-n", "systemctl", "start", "ollama"]
    assert "BatchMode=yes" in argv


def test_lumen_unit_is_started_locally(calls):
    restore.main(["lumen", "ollama"])
    assert calls["argv"] == [["sudo", "-n", "systemctl", "start", "ollama"]]


def test_thor_is_never_touched(calls):
    with pytest.raises(SystemExit) as e:
        restore.main(["thor", "sshd"])
    assert e.value.code == 2
    assert calls["argv"] == []


@pytest.mark.parametrize("argv", [
    ["isis; rm -rf /", "ollama"],
    ["isis", "ollama && curl evil"],
    ["isis", "$(whoami)"],
    ["isis"],
    ["isis", "ollama", "extra"],
])
def test_malformed_input_refuses_before_any_call(calls, argv):
    with pytest.raises(SystemExit) as e:
        restore.main(argv)
    assert e.value.code == 2
    assert calls["argv"] == []


def test_failed_start_exits_one(calls):
    calls["rc"] = 5
    with pytest.raises(SystemExit) as e:
        restore.main(["isis", "ollama"])
    assert e.value.code == 1
