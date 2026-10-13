"""wq#3792: a fallback: routing decision is automatic, unattended re-routing --
not a human override -- so it must require identity verification exactly like
pin:, not fall into the env: exemption. Covers the three model_source prefixes
resolve_judge_model() can return and asserts the `required` flag bin/judge
passes to verify_model_identity() for each."""
import importlib.machinery
import importlib.util
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
JUDGE_SRC = os.path.join(os.path.dirname(HERE), "judge")


def _load_judge():
    loader = importlib.machinery.SourceFileLoader("judge_under_test", JUDGE_SRC)
    spec = importlib.util.spec_from_loader(loader.name, loader)
    mod = importlib.util.module_from_spec(spec)
    loader.exec_module(mod)
    return mod


def _run_main_capturing_required(monkeypatch, judge, model, model_source):
    captured = {}

    monkeypatch.setattr(judge, "resolve_model", lambda: (model, model_source))
    monkeypatch.setattr(judge, "litellm_key", lambda: "k")
    monkeypatch.setattr(judge, "db_init", lambda: None)
    monkeypatch.setattr(judge, "log_verdict", lambda *a, **k: None)

    def fake_verify(m, required=False, timeout=5):
        captured["required"] = required
        return "forced fail-closed for test"

    monkeypatch.setattr(judge, "verify_model_identity", fake_verify)
    monkeypatch.setattr(judge, "fail_closed", lambda *a, **k: (_ for _ in ()).throw(SystemExit(3)))
    monkeypatch.setattr(sys, "argv", ["judge", "--actor", "t", "--kind", "bash", "--command", "x"])

    try:
        judge.main()
    except SystemExit:
        pass
    return captured["required"]


def test_pin_source_requires_identity(monkeypatch):
    judge = _load_judge()
    assert _run_main_capturing_required(monkeypatch, judge, "hathor-llama8b", "pin:/x") is True


def test_fallback_source_requires_identity(monkeypatch):
    """The gap this ticket closes: a fallback: source used to be exempt
    (only pin: was required=True), so an unpinned fallback rung's identity
    was never checked -- exactly the silent-retag risk wq#2821 exists for."""
    judge = _load_judge()
    assert _run_main_capturing_required(
        monkeypatch, judge, "nephthys-qwen14b", "fallback:/x") is True


def test_env_source_is_still_exempt(monkeypatch):
    """env: is a deliberate human override (bench/experiments) -- the one
    case that legitimately skips the identity pin requirement."""
    judge = _load_judge()
    assert _run_main_capturing_required(
        monkeypatch, judge, "some-bench-model", "env:JUDGE_MODEL") is False
