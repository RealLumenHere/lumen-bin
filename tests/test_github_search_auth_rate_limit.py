"""Unit tests for fleet-search's GitHub token resolution + rate-limit handling (wq#2569).

wq#2564 audit: `fleet-search github <query>` from a fworker sandbox came back
{ok:true, results:[], authenticated:false} — fleet-search read GITHUB_TOKEN from
agent/.env only, which a sandboxed worker cannot reach (instinct 4 denylists reading
credentials), so every worker shared GitHub's 60/hr unauthenticated per-IP budget and
exhaustion read as a silent "no findings" success. Covers the fix:
  - _resolve_github_token() goes through the shared credential helper
    (lumen-github-identity.sh) via a subprocess, never this process's own env/a URL.
  - cmd_github surfaces ok:false with a reset time on 403/429 or
    X-RateLimit-Remaining=0, including the 200-with-empty-items shape the bug report
    described, instead of letting it read as an ordinary empty success.

Run: python3 -m pytest tests/test_github_search_auth_rate_limit.py -v
     (or: python3 tests/test_github_search_auth_rate_limit.py)
"""
import argparse
import importlib.machinery
import importlib.util
import json
import os
import subprocess

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
SCRIPT = os.path.join(HERE, "..", "fleet-search")


def _load_fleet_search():
    loader = importlib.machinery.SourceFileLoader("fleet_search_under_test_gh", SCRIPT)
    spec = importlib.util.spec_from_loader(loader.name, loader)
    mod = importlib.util.module_from_spec(spec)
    loader.exec_module(mod)
    return mod


fs = _load_fleet_search()


def _gh_args(query="litellm spend logs per model request count", n=5,
             code=False, issues=False, discussions=False, project=None):
    return argparse.Namespace(query=query, n=n, code=code, issues=issues,
                               discussions=discussions, project=project)


# ---------- _resolve_github_token: credential helper, never env/URL ----------

def test_resolve_github_token_calls_credential_helper_via_subprocess(monkeypatch):
    calls = []

    def fake_run(argv, capture_output, text, timeout):
        calls.append(argv)
        assert argv[0] == "bash" and argv[1] == "-c"
        return subprocess.CompletedProcess(argv, 0, stdout="tok-abc123\n/home/lumen/.secrets/github-frank.token", stderr="")

    monkeypatch.setattr(fs.subprocess, "run", fake_run)
    monkeypatch.setattr(fs.os.path, "isfile", lambda p: True)

    token, source = fs._resolve_github_token()

    assert token == "tok-abc123"
    assert source == "/home/lumen/.secrets/github-frank.token"
    assert len(calls) == 1
    # the helper script text names the shared lib, not a raw env read
    assert fs.GITHUB_IDENTITY_LIB in calls[0]


def test_resolve_github_token_empty_when_helper_missing(monkeypatch):
    monkeypatch.setattr(fs.os.path, "isfile", lambda p: False)
    token, source = fs._resolve_github_token()
    assert (token, source) == ("", "")


def test_resolve_github_token_empty_on_nonzero_exit(monkeypatch):
    monkeypatch.setattr(fs.os.path, "isfile", lambda p: True)
    monkeypatch.setattr(fs.subprocess, "run",
                         lambda *a, **kw: subprocess.CompletedProcess([], 1, stdout="", stderr="no rung answered"))
    token, source = fs._resolve_github_token()
    assert (token, source) == ("", "")


def test_resolve_github_token_never_reads_process_env_for_the_value(monkeypatch):
    """The bug this ticket fixes was reading a token out of env/agent-.env; guard against
    regressing to that by proving a poisoned os.environ has no effect on the result."""
    monkeypatch.setattr(fs.os.path, "isfile", lambda p: True)
    monkeypatch.setattr(fs.subprocess, "run",
                         lambda *a, **kw: subprocess.CompletedProcess([], 0, stdout="real-token\n/path", stderr=""))
    monkeypatch.setenv("GITHUB_TOKEN", "env-leaked-token-should-be-ignored")
    token, _ = fs._resolve_github_token()
    assert token == "real-token"


# ---------- _rate_limit_note ----------

def test_rate_limit_note_formats_reset_timestamp():
    assert fs._rate_limit_note({"remaining": "0", "reset": "1735689600"}) == "2025-01-01T00:00:00Z"


def test_rate_limit_note_none_when_missing():
    assert fs._rate_limit_note({"remaining": "10", "reset": None}) is None
    assert fs._rate_limit_note({}) is None


# ---------- cmd_github: rate-limit / auth surfacing (AC2) ----------

def test_403_rate_limited_returns_ok_false_with_reset(monkeypatch, capsys):
    monkeypatch.setattr(fs, "cache_store", lambda *a, **kw: None)
    monkeypatch.setattr(fs, "_resolve_github_token", lambda: ("tok", "/home/lumen/.secrets/github-frank.token"))
    monkeypatch.setattr(fs, "_get_json_with_headers",
                         lambda *a, **kw: (403, None, "rate limit exceeded",
                                            {"remaining": "0", "reset": "1735689600"}))

    with pytest.raises(SystemExit) as ei:
        fs.cmd_github(_gh_args())
    assert ei.value.code == 2

    out = json.loads(capsys.readouterr().out)
    assert out["ok"] is False
    assert "rate limited" in out["error"]
    assert out["rate_limit_reset"] == "2025-01-01T00:00:00Z"
    assert out["authenticated"] is True


def test_200_with_remaining_zero_returns_ok_false_not_empty_ok_true(monkeypatch, capsys):
    """The exact symptom from the wq#2564 audit: a 200 with an empty item list on an
    exhausted budget must not read as an ordinary successful empty search."""
    monkeypatch.setattr(fs, "cache_store", lambda *a, **kw: None)
    monkeypatch.setattr(fs, "_resolve_github_token", lambda: ("", ""))
    monkeypatch.setattr(fs, "_get_json_with_headers",
                         lambda *a, **kw: (200, {"items": [], "total_count": 0}, None,
                                            {"remaining": "0", "reset": "1735689600"}))

    with pytest.raises(SystemExit) as ei:
        fs.cmd_github(_gh_args())
    assert ei.value.code == 2

    out = json.loads(capsys.readouterr().out)
    assert out["ok"] is False
    assert "results" not in out
    assert "rate limited" in out["error"]
    assert "unauthenticated" in out["error"]


def test_401_error_names_credential_helper_not_agent_env(monkeypatch, capsys):
    monkeypatch.setattr(fs, "cache_store", lambda *a, **kw: None)
    monkeypatch.setattr(fs, "_resolve_github_token", lambda: ("", ""))
    monkeypatch.setattr(fs, "_get_json_with_headers",
                         lambda *a, **kw: (401, None, "Bad credentials", {"remaining": None, "reset": None}))

    with pytest.raises(SystemExit):
        fs.cmd_github(_gh_args())

    out = json.loads(capsys.readouterr().out)
    assert out["ok"] is False
    assert "credential helper" in out["error"]
    assert out["authenticated"] is False


def test_success_reports_authenticated_and_token_source(monkeypatch, capsys):
    monkeypatch.setattr(fs, "cache_store", lambda *a, **kw: None)
    monkeypatch.setattr(fs, "_resolve_github_token",
                         lambda: ("tok", "/home/lumen/.secrets/github-frank.token"))
    monkeypatch.setattr(fs, "_get_json_with_headers",
                         lambda *a, **kw: (200,
                                            {"items": [{"full_name": "lumen/repo", "html_url": "https://x",
                                                        "stargazers_count": 1, "description": "d",
                                                        "updated_at": "2026-01-01"}],
                                             "total_count": 1},
                                            None, {"remaining": "4999", "reset": "1735689600"}))

    with pytest.raises(SystemExit) as ei:
        fs.cmd_github(_gh_args())
    assert ei.value.code == 0

    out = json.loads(capsys.readouterr().out)
    assert out["ok"] is True
    assert out["authenticated"] is True
    assert out["token_source"] == "/home/lumen/.secrets/github-frank.token"
    assert out["total"] == 1


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
