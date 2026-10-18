"""wq#4309: central Amazon-noise scrub at the two sinks (cache_store/emit).

wq#4281 wired _clean_amazon_url/_clean_amazon_noise per backend, which covered
only searxng/brave/deepseek and missed hn, so, github, reddit --deep, read,
every title, and cache replay (wq#4305 judgment §2-3). The fix moves the scrub
to _scrub() called once in cache_store() and once in emit(); these tests prove
a split-built SubscriptionId value (never a bare AKIA literal, wq#2795/wq#2839)
is stripped on each previously-uncovered surface, from a result title, and from
a replayed cache record written dirty by a pre-fix binary.

Run: python3 -m pytest tests/test_wq4309_central_amazon_scrub.py -v
"""
import argparse
import importlib.machinery
import importlib.util
import json
import os
import time

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
SCRIPT = os.path.join(HERE, "..", "fleet-search")


def _load_fleet_search():
    loader = importlib.machinery.SourceFileLoader("fleet_search_under_test_wq4309", SCRIPT)
    spec = importlib.util.spec_from_loader(loader.name, loader)
    mod = importlib.util.module_from_spec(spec)
    loader.exec_module(mod)
    return mod


fs = _load_fleet_search()

# Split-built, never a bare literal (wq#2795/wq#2839 class).
_SUBSCRIPTION_ID = "AKI" + "A" + "Q" * 16
_AFFILIATE_TAG = "lumen" + "research-20"


def _dirty_url():
    """Committed fixture: the URL shape that false-fired on wq#4267/wq#4268."""
    return ("https://www.amazon.com/Polaris-Freedom-Robotic-Cleaner/dp/B07ABCDEFG"
            "?SubscriptionId=" + _SUBSCRIPTION_ID + "&tag=" + _AFFILIATE_TAG
            + "&pd_rd_r=abc123&psc=1&ref_=as_li_ss_tl&_encoding=UTF8&qid=1700000000")


_CLEAN_PREFIX = "https://www.amazon.com/Polaris-Freedom-Robotic-Cleaner/dp/B07ABCDEFG"


@pytest.fixture
def tmp_cache(monkeypatch, tmp_path):
    """Point the per-project cache at tmp_path so tests never touch real caches."""
    monkeypatch.setattr(fs, "_cache_paths", lambda project: (
        str(tmp_path / "search_cache.jsonl"), str(tmp_path / "PRIOR_ART.md"), str(tmp_path)))
    return tmp_path


def _emitted(capsys, fn, args):
    with pytest.raises(SystemExit) as exc:
        fn(args)
    assert exc.value.code == 0
    out = capsys.readouterr().out
    assert _SUBSCRIPTION_ID not in out
    assert "SubscriptionId" not in out
    return json.loads(out)


def test_scrub_extended_noise_params_and_prefixes():
    cleaned = fs._clean_amazon_url(_dirty_url())
    for noise in ("SubscriptionId", "tag=", "pd_rd_r", "psc", "ref_", "_encoding"):
        assert noise not in cleaned
    assert "qid=1700000000" in cleaned          # content-relevant params survive
    assert cleaned.startswith(_CLEAN_PREFIX)


def test_hn_emit_scrubs_snippet_and_title(monkeypatch, capsys, tmp_cache):
    fixture = {"hits": [{"title": "Deal: " + _dirty_url(),
                         "url": _dirty_url(),
                         "comment_text": "I bought mine at " + _dirty_url() + " works great",
                         "objectID": "1", "points": 10, "num_comments": 2}],
               "nbHits": 1}
    monkeypatch.setattr(fs, "_get_json", lambda *a, **k: (200, fixture, None))
    data = _emitted(capsys, fs.cmd_hn,
                    argparse.Namespace(query="pool cleaner", n=5, project="t"))
    r = data["results"][0]
    assert r["title"].startswith("Deal: " + _CLEAN_PREFIX)   # titles scrubbed (wq#4305 hole)
    assert "works great" in r["snippet"]


def test_so_emit_scrubs_body(monkeypatch, capsys, tmp_cache):
    fixture = {"items": [{"title": "Where to buy? " + _dirty_url(),
                          "link": "https://stackoverflow.com/q/1",
                          "body": "<p>See " + _dirty_url() + "</p>",
                          "score": 1, "is_answered": True, "answer_count": 1}],
               "quota_remaining": 100}
    monkeypatch.setattr(fs, "_env_token", lambda *a, **k: None)
    monkeypatch.setattr(fs, "_get_json", lambda *a, **k: (200, fixture, None))
    data = _emitted(capsys, fs.cmd_so,
                    argparse.Namespace(query="pool cleaner", n=5, project="t"))
    assert data["results"][0]["snippet"].startswith("See " + _CLEAN_PREFIX)


def test_github_issues_emit_scrubs_body(monkeypatch, capsys, tmp_cache):
    fixture = {"items": [{"title": "link cleaner misses " + _dirty_url(),
                          "html_url": "https://github.com/x/y/issues/1",
                          "state": "open", "comments": 0,
                          "body": "repro: " + _dirty_url(),
                          "repository_url": "https://api.github.com/repos/x/y"}],
               "total_count": 1}
    monkeypatch.setattr(fs, "_resolve_github_token", lambda: ("", ""))
    monkeypatch.setattr(fs, "_get_json_with_headers",
                        lambda *a, **k: (200, fixture, None, {"remaining": "10", "reset": None}))
    data = _emitted(capsys, fs.cmd_github,
                    argparse.Namespace(query="amazon link", n=5, project="t",
                                       issues=True, code=False, discussions=False))
    assert data["results"][0]["snippet"].startswith("repro: " + _CLEAN_PREFIX)


def test_reddit_deep_emit_scrubs_comments(monkeypatch, capsys, tmp_cache):
    thread_url = "https://www.reddit.com/r/pools/comments/1/polaris/"
    monkeypatch.setattr(fs, "searxng_search", lambda *a, **k: (
        [{"title": "Polaris thread", "url": thread_url, "snippet": "", "engine": "g"}], None, []))
    payload = {"content": "OP: compare " + _dirty_url(),
               "comments": [{"author": "u1", "depth": 0, "text": "cheapest: " + _dirty_url()}],
               "comment_count": 1, "url": thread_url, "http": 200, "bytes": 100, "tier": 1}
    monkeypatch.setattr(fs, "_fetch_and_extract", lambda url: (payload, None))
    data = _emitted(capsys, fs.cmd_reddit,
                    argparse.Namespace(query="polaris", n=5, project="t", no_cache=True,
                                       ttl=60, deep=True, threads=1, depth=None))
    r = data["results"][0]
    assert r["comments"][0]["text"].startswith("cheapest: " + _CLEAN_PREFIX)
    assert r["content"].startswith("OP: compare " + _CLEAN_PREFIX)


def test_read_emit_scrubs_extracted_content(monkeypatch, capsys, tmp_cache):
    payload = {"content": "Review roundup. Buy: " + _dirty_url() + " end.",
               "comments": [], "comment_count": 0,
               "url": "https://example.com/review", "http": 200, "bytes": 100, "tier": 1}
    monkeypatch.setattr(fs, "_fetch_and_extract", lambda url: (payload, None))
    data = _emitted(capsys, fs.cmd_read,
                    argparse.Namespace(url="https://example.com/review",
                                       project="t", no_cache=True, ttl=60))
    assert data["content"].startswith("Review roundup. Buy: " + _CLEAN_PREFIX)


def test_cache_replay_of_prefix_dirty_record_is_scrubbed(monkeypatch, capsys, tmp_cache):
    """A record written by a pre-fix binary replays through emit() clean for the
    rest of its 14d TTL — the hole per-backend wiring could never close."""
    query, n = "polaris pool cleaner", 10
    rec = {"key": fs._cache_key("web", query, n), "kind": "web", "query": query, "n": n,
           "ts": time.time(), "iso": "2026-10-18T00:00:00Z",
           "results": [{"title": "Polaris " + _dirty_url(), "url": _dirty_url(),
                        "snippet": "see " + _dirty_url(), "engine": "g"}]}
    (tmp_cache / "search_cache.jsonl").write_text(json.dumps(rec) + "\n")

    def _boom(*a, **k):
        raise AssertionError("cache hit expected; live search must not run")
    monkeypatch.setattr(fs, "searxng_search", _boom)
    data = _emitted(capsys, fs.cmd_web,
                    argparse.Namespace(query=query, n=n, project="t", no_cache=False, ttl=3600))
    assert data["cached"] is True
    assert data["results"][0]["url"].startswith(_CLEAN_PREFIX)
    assert data["results"][0]["title"].startswith("Polaris " + _CLEAN_PREFIX)


def test_cache_store_persists_scrubbed_results_and_prior_art(tmp_cache):
    dirty = [{"title": "Polaris " + _dirty_url(), "url": _dirty_url(),
              "snippet": "see " + _dirty_url(), "engine": "g"}]
    fs.cache_store("t", "web", "polaris", 10, dirty)
    cached = (tmp_cache / "search_cache.jsonl").read_text()
    prior = (tmp_cache / "PRIOR_ART.md").read_text()
    assert _SUBSCRIPTION_ID not in cached and "SubscriptionId" not in cached
    assert _SUBSCRIPTION_ID not in prior and "SubscriptionId" not in prior
    assert _CLEAN_PREFIX in prior
