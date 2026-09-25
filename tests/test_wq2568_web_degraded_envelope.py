"""wq#2568: fleet-search web must not report ok:true when every general-web
engine SearXNG has is suspended/CAPTCHA'd. wq#2564 audit: a shared egress IP
self-rate-limits brave/google-cse/duckduckgo/startpage at once, SearXNG still
answers 200 with results:[], and the old cmd_web wrapped that as {ok:true,
results:[]} -- read by callers (including the mandatory prior-art search step)
as "nothing exists" instead of "the search never happened".

Run: python3 -m pytest tests/test_wq2568_web_degraded_envelope.py -v
"""
import argparse
import importlib.machinery
import importlib.util
import json
import os

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
SCRIPT = os.path.join(HERE, "..", "fleet-search")

ALL_SUSPENDED = [
    ["brave", "too many requests"],
    ["duckduckgo", "CAPTCHA"],
    ["google cse", "too many requests"],
    ["startpage", "Suspended: CAPTCHA"],
]


def _load_fleet_search():
    loader = importlib.machinery.SourceFileLoader("fleet_search_under_test_wq2568", SCRIPT)
    spec = importlib.util.spec_from_loader(loader.name, loader)
    mod = importlib.util.module_from_spec(spec)
    loader.exec_module(mod)
    return mod


fs = _load_fleet_search()


def _web_args(query="systemd StartLimitBurst", n=10):
    return argparse.Namespace(query=query, n=n, project=None, no_cache=True, ttl=fs.CACHE_TTL_DEFAULT)


def _run_cmd_web(monkeypatch, capsys, get_json_fake, brave_key=None):
    monkeypatch.setattr(fs, "cache_store", lambda *a, **k: None)
    monkeypatch.setattr(fs, "_get_json", get_json_fake)
    if brave_key is None:
        monkeypatch.setattr(fs, "_env_token", lambda key: "")
    else:
        monkeypatch.setattr(fs, "_env_token", lambda key: brave_key if key == "BRAVE_API_KEY" else "")
    with pytest.raises(SystemExit) as exc:
        fs.cmd_web(_web_args())
    out = json.loads(capsys.readouterr().out)
    return out, exc.value.code


def test_all_general_engines_suspended_without_key_returns_ok_false(monkeypatch, capsys):
    """wq#2564's exact symptom: 200 + empty results + all 4 general engines down."""
    def fake_get_json(url, headers=None, timeout=fs.HTTP_TIMEOUT):
        assert "search?" in url
        return 200, {"query": "q", "results": [], "unresponsive_engines": ALL_SUSPENDED}, None

    out, code = _run_cmd_web(monkeypatch, capsys, fake_get_json)

    assert out["ok"] is False
    assert code == 2
    assert out["unresponsive_engines"] == ALL_SUSPENDED
    for name in fs.WEB_SEARCH_ENGINES:
        assert name in out["error"]


def test_all_general_engines_suspended_with_brave_key_falls_back_to_ok_true(monkeypatch, capsys):
    def fake_get_json(url, headers=None, timeout=fs.HTTP_TIMEOUT):
        if url.startswith(fs.SEARXNG):
            return 200, {"query": "q", "results": [], "unresponsive_engines": ALL_SUSPENDED}, None
        assert url.startswith(fs.BRAVE_API_BASE)
        assert headers["X-Subscription-Token"] == "test-brave-key"
        return 200, {"web": {"results": [{"title": "t", "url": "http://x", "description": "d"}]}}, None

    out, code = _run_cmd_web(monkeypatch, capsys, fake_get_json, brave_key="test-brave-key")

    assert out["ok"] is True
    assert code == 0
    assert out["source"] == "brave-api"
    assert out["results"] == [{"title": "t", "url": "http://x", "snippet": "d", "engine": "brave-api"}]
    assert out["unresponsive_engines"] == ALL_SUSPENDED
    assert "fallback" in out["note"]


def test_all_general_engines_suspended_brave_key_itself_rate_limited_still_ok_false(monkeypatch, capsys):
    def fake_get_json(url, headers=None, timeout=fs.HTTP_TIMEOUT):
        if url.startswith(fs.SEARXNG):
            return 200, {"query": "q", "results": [], "unresponsive_engines": ALL_SUSPENDED}, None
        return 429, None, b"rate limited"

    out, code = _run_cmd_web(monkeypatch, capsys, fake_get_json, brave_key="test-brave-key")

    assert out["ok"] is False
    assert code == 2
    assert "429" in out["error"]


def test_partial_unresponsive_engines_still_ok_true(monkeypatch, capsys):
    """Only one of four general engines down: a real search still happened."""
    partial = [["brave", "too many requests"]]

    def fake_get_json(url, headers=None, timeout=fs.HTTP_TIMEOUT):
        return 200, {"query": "q", "results": [{"title": "hit", "url": "http://y", "content": "c",
                                                  "engine": "duckduckgo"}],
                      "unresponsive_engines": partial}, None

    out, code = _run_cmd_web(monkeypatch, capsys, fake_get_json)

    assert out["ok"] is True
    assert code == 0
    assert out["source"] == "searxng"
    assert out["unresponsive_engines"] == partial
    assert len(out["results"]) == 1


def test_clean_success_no_unresponsive_engines(monkeypatch, capsys):
    def fake_get_json(url, headers=None, timeout=fs.HTTP_TIMEOUT):
        return 200, {"query": "q", "results": [{"title": "hit", "url": "http://y", "content": "c",
                                                  "engine": "google cse"}],
                      "unresponsive_engines": []}, None

    out, code = _run_cmd_web(monkeypatch, capsys, fake_get_json)

    assert out["ok"] is True
    assert code == 0
    assert out["unresponsive_engines"] == []
