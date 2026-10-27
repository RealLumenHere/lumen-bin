"""wq#4711 — watchdog --require-exit: artifact stability alone is not OK.

Reproduces the field bug (build-limpet-iso, 2026-10-27): plain `-a` declares OK as
soon as the artifact's size is stable, giving the producer only a fixed ~30s grace
window to exit before reporting regardless of its eventual exit code. A producer
that keeps running past that window (xorriso finishes the ISO, then stage11b/
stage12 still run) gets a premature OK even if it later fails. `--require-exit`
closes that: once the artifact is stable it waits for the real exit code instead
of guessing, however long the producer keeps running after the artifact appears.

Run: pytest tests/test_wq4711_watchdog_require_exit.py
"""
import json
import subprocess
from pathlib import Path

WATCHDOG = Path(__file__).resolve().parent.parent / "watchdog"


def _run_watchdog(tmp_path, label, producer_sh, *extra_args):
    artifact = tmp_path / "out.iso"
    for suffix in (".result.json", ".log", ".out", ".rc", ".pid", ".tail"):
        Path(f"/tmp/watchdog-{label}{suffix}").unlink(missing_ok=True)
    cmd = [
        "bash", str(WATCHDOG), "--fg", "--label", label, "--no-send",
        "-a", str(artifact), "--poll", "1", "--stable", "2", "--timeout", "1",
        *extra_args, "--", "bash", "-c", producer_sh,
    ]
    subprocess.run(cmd, capture_output=True, text=True, timeout=30)
    return json.loads(Path(f"/tmp/watchdog-{label}.result.json").read_text())


def test_require_exit_waits_past_artifact_stability_and_reports_fail(tmp_path):
    # Artifact appears and stabilizes well inside the default ~30s post-stability
    # grace window, but the producer keeps running past it and then exits nonzero
    # (same shape as the 70s field repro, compressed for test speed).
    producer = f'echo data > "{tmp_path}/out.iso"; sleep 4; exit 1'
    result = _run_watchdog(tmp_path, "wq4711fail", producer, "--require-exit")
    assert result["status"] == "FAIL", result
    assert result["rc"] == 1, result


def test_require_exit_reports_ok_only_after_clean_exit(tmp_path):
    producer = f'echo data > "{tmp_path}/out.iso"; sleep 1; exit 0'
    result = _run_watchdog(tmp_path, "wq4711ok", producer, "--require-exit")
    assert result["status"] == "OK", result
    assert result["rc"] == 0, result


def test_plain_artifact_flag_unchanged_without_require_exit(tmp_path):
    # No regression: a fast, clean producer still reports OK via the pre-existing
    # -a path when --require-exit is not passed.
    producer = f'echo data > "{tmp_path}/out.iso"; sleep 1; exit 0'
    result = _run_watchdog(tmp_path, "wq4711plain", producer)
    assert result["status"] == "OK", result
    assert result["rc"] == 0, result


def test_require_exit_rejected_without_artifact():
    result = subprocess.run(
        ["bash", str(WATCHDOG), "--require-exit", "--no-send", "--", "true"],
        capture_output=True, text=True, timeout=10,
    )
    assert result.returncode == 3
    assert "--require-exit requires" in result.stderr
