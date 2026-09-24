"""wq#1401: the pre-reboot boot-safety gate must fail closed.

maintenance-window.md permits reboots in the Mon 02:00-05:00 PT window only when a
triple-check passes, and is emphatic about the third leg: "NEVER reboot a node whose
auto-return is unknown (see thoth/apis lessons)". Frank has no console, so a gate that
says REBOOT-SAFE on a node that will not come back is worse than no gate.

These drive check_auto_return directly with the state lookup stubbed, so the assertions
are about the RULE and not about whatever facts happen to be recorded today.
"""
import importlib.util
import sys
from pathlib import Path

import pytest

GATE = Path(__file__).resolve().parents[1] / "lumen-reboot-gate"


@pytest.fixture
def gate(monkeypatch):
    spec = importlib.util.spec_from_loader("reboot_gate",
                                           importlib.machinery.SourceFileLoader(
                                               "reboot_gate", str(GATE)))
    mod = importlib.util.module_from_spec(spec)
    sys.modules["reboot_gate"] = mod
    spec.loader.exec_module(mod)
    return mod


def stub_facts(gate, facts):
    gate.state_fact = lambda subject, predicate: facts.get(predicate, "")


def test_validated_auto_boot_passes(gate):
    stub_facts(gate, {"auto_boot_verified": "yes — full AC-restore cycle validated"})
    ok, why = gate.check_auto_return("nephthys")
    assert ok is True
    assert "auto_boot_verified" in why


def test_no_facts_at_all_is_refused(gate):
    """The thoth/apis case: nothing recorded means unknown, and unknown means no."""
    stub_facts(gate, {})
    ok, why = gate.check_auto_return("thoth")
    assert ok is False
    assert "UNKNOWN" in why


def test_controllable_plug_alone_is_not_enough(gate):
    """A plug can cut AC. Without AC-restore that is the unrecoverable move, not a
    recovery path — the distinction the thoth/apis lessons are actually about."""
    stub_facts(gate, {"power_plug": "P10"})
    ok, why = gate.check_auto_return("thoth")
    assert ok is False
    assert "AC-restore" in why


def test_ac_restore_alone_is_not_enough(gate):
    """AC-restore with no controllable plug leaves no way to force the cycle."""
    stub_facts(gate, {"bios": "AC-restore=PowerOn, RC6=Enabled"})
    ok, why = gate.check_auto_return("sobek")
    assert ok is False
    assert "no controllable plug" in why


def test_ac_restore_plus_controllable_plug_passes(gate):
    stub_facts(gate, {"bios": "AC-restore=PowerOn, PL1=12000", "power_plug": "P7"})
    ok, why = gate.check_auto_return("bes")
    assert ok is True
    assert "p7" in why


def test_remediation_hold_refuses_even_with_proof(gate):
    """A hold outranks every other proof — khepri carries one precisely because its
    plug mapping was wrong, and actuating on a wrong mapping cuts the wrong box."""
    stub_facts(gate, {
        "remediation_hold": "on — power_plug mapping UNKNOWN/unconfirmed",
        "auto_boot_verified": "yes",
        "bios": "AC-restore=PowerOn", "power_plug": "P3",
    })
    ok, why = gate.check_auto_return("khepri")
    assert ok is False
    assert "remediation_hold" in why


def test_uncontrollable_plug_values_are_not_mistaken_for_plugs(gate):
    """`none`, `NONE — ...` and prose like gameboy's Tasmota note must not parse as pN."""
    for value in ("none — UNMETERED, on no Tapo plug",
                  "NONE — Khepri does NOT hold P3",
                  "sole in-service Tasmota outlet (host is disposable)"):
        stub_facts(gate, {"bios": "AC-restore=PowerOn", "power_plug": value})
        ok, why = gate.check_auto_return("n")
        assert ok is False, f"{value!r} was treated as a controllable plug"
        assert "no controllable plug" in why


def test_household_plugs_are_never_accepted(gate):
    """p13/p14 are Frank's household outlets (riley-pc / parker-pc), excluded from
    fleet ops by plug-ctl's own comment. A fleet node claiming one is a bad fact,
    and acting on it would cut a family member's PC."""
    for plug in ("p13", "p14", "p12"):
        stub_facts(gate, {"bios": "AC-restore=PowerOn", "power_plug": plug})
        ok, _ = gate.check_auto_return("n")
        assert ok is False, f"{plug} was accepted as a fleet recovery path"


def test_skipped_remote_checks_can_never_report_reboot_safe(gate, monkeypatch, capsys):
    """--skip-remote answers only the state question. A skipped check is not a passed
    check, or the flag becomes a way to talk the gate into a yes."""
    stub_facts(gate, {"auto_boot_verified": "yes"})
    monkeypatch.setattr(sys, "argv", ["lumen-reboot-gate", "nephthys", "--skip-remote"])
    rc = gate.main()
    assert rc != 0
    assert "DO-NOT-REBOOT" in capsys.readouterr().out


ACTUATING = ("reboot", "shutdown", "poweroff", "halt", "plug-ctl", "systemctl start",
             "systemctl stop", "systemctl restart")


def test_gate_never_executes_a_reboot_or_power_verb():
    """Structural: this tool reports, it does not act, so it can never become the thing
    that does the reboot. Checked against what it EXECUTES, not against any string it
    contains — the refusal messages name plug-ctl on purpose, as advice to a human."""
    import ast
    tree = ast.parse(GATE.read_text())
    executed = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        target = ast.unparse(node.func)
        if target not in ("subprocess.run", "ssh", "subprocess.check_output",
                          "subprocess.Popen", "os.system"):
            continue
        for arg in node.args:
            for lit in ast.walk(arg):
                if isinstance(lit, ast.Constant) and isinstance(lit.value, str):
                    executed.append(lit.value)
                elif isinstance(lit, ast.JoinedStr):
                    executed.append(ast.unparse(lit))
    blob = " ".join(executed).lower()
    assert blob, "found no executed commands to check — the walk is broken, not the gate"
    for verb in ACTUATING:
        assert verb not in blob, (
            f"the gate EXECUTES an actuating verb {verb!r}; it must only report. "
            f"executed strings: {executed}")
