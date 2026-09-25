"""wq#2603 (wq#2601 judgment): fleet-search's web cache must not serve or store
empty-result records. wq#2599 re-reproduced 'fleet-search web "systemd
StartLimitBurst"' returning {ok:true, cached:true, results:[]} from an
outage-era cache record because cache_lookup (fleet-search:121-138) served any
hit within the 14-day TTL with no validity check, and cache_store
(fleet-search:141-158) had no guard stopping a partial outage from writing a
fresh empty record (the regrowth path at fleet-search:259).

Offline, no network needed. (a) and (c) must fail against c7c0dde.

Run: python3 -m pytest tests/test_wq2603_empty_cache_record.py -v
"""
import importlib.machinery
import importlib.util
import json
import os
import time

HERE = os.path.dirname(os.path.abspath(__file__))
SCRIPT = os.path.join(HERE, "..", "fleet-search")


def _load_fleet_search():
    loader = importlib.machinery.SourceFileLoader("fleet_search_under_test_wq2603", SCRIPT)
    spec = importlib.util.spec_from_loader(loader.name, loader)
    mod = importlib.util.module_from_spec(spec)
    loader.exec_module(mod)
    return mod


fs = _load_fleet_search()


def _write_cache(path, records):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as fh:
        for rec in records:
            fh.write(json.dumps(rec) + "\n")


def _rec(key, results, ts=None, iso="2026-09-25T04:06:07Z"):
    return {"key": key, "kind": "web", "query": "q", "n": 10,
            "ts": ts if ts is not None else time.time(), "iso": iso, "results": results}


def test_outage_era_empty_only_record_is_not_a_hit(tmp_path, monkeypatch):
    """(a) A file with only an outage-era empty web record for key K -> lookup None."""
    project = "proj"
    monkeypatch.setattr(fs, "PROJECTS", str(tmp_path))
    key = fs._cache_key("web", "systemd StartLimitBurst", 10)
    path, _, _ = fs._cache_paths(project)
    _write_cache(path, [_rec(key, [])])

    hit = fs.cache_lookup(project, "web", "systemd StartLimitBurst", 10, fs.CACHE_TTL_DEFAULT)

    assert hit is None


def test_older_nonempty_and_newer_empty_returns_the_nonempty_one(tmp_path, monkeypatch):
    """(b) freshest VALID wins: an older non-empty in-TTL record still serves."""
    project = "proj"
    monkeypatch.setattr(fs, "PROJECTS", str(tmp_path))
    key = fs._cache_key("web", "systemd StartLimitBurst", 10)
    path, _, _ = fs._cache_paths(project)
    good = [{"title": "hit", "url": "http://y", "engine": "duckduckgo"}]
    _write_cache(path, [
        _rec(key, good, ts=time.time() - 100),
        _rec(key, [], ts=time.time() - 10),
    ])

    hit = fs.cache_lookup(project, "web", "systemd StartLimitBurst", 10, fs.CACHE_TTL_DEFAULT)

    assert hit is not None
    assert hit["results"] == good


def test_cache_store_appends_nothing_for_empty_results(tmp_path, monkeypatch):
    """(c) cache_store(..., []) appends nothing."""
    project = "proj"
    monkeypatch.setattr(fs, "PROJECTS", str(tmp_path))
    path, _, _ = fs._cache_paths(project)

    fs.cache_store(project, "web", "systemd StartLimitBurst", 10, [])

    assert not os.path.exists(path)


def test_cache_store_and_lookup_roundtrip_unchanged_for_nonempty(tmp_path, monkeypatch):
    """(d) non-empty round-trip unchanged."""
    project = "proj"
    monkeypatch.setattr(fs, "PROJECTS", str(tmp_path))
    good = [{"title": "hit", "url": "http://y", "engine": "duckduckgo"}]

    fs.cache_store(project, "web", "systemd StartLimitBurst", 10, good)
    hit = fs.cache_lookup(project, "web", "systemd StartLimitBurst", 10, fs.CACHE_TTL_DEFAULT)

    assert hit is not None
    assert hit["results"] == good


def test_read_dict_payload_still_caches_and_serves(tmp_path, monkeypatch):
    """(e) cmd_read's dict payload (fleet-search:677-688) is unaffected by the list-only guard."""
    project = "proj"
    monkeypatch.setattr(fs, "PROJECTS", str(tmp_path))
    payload = {"content": "body text", "comments": [], "comment_count": 0,
               "tier": "raw", "url": "http://y", "http": 200, "bytes": 9}

    fs.cache_store(project, "read", "http://y", 0, payload)
    hit = fs.cache_lookup(project, "read", "http://y", 0, fs.CACHE_TTL_DEFAULT)

    assert hit is not None
    assert hit["results"] == payload
