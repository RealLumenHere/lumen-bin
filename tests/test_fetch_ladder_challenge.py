"""Unit tests for _detect_challenge() / _fetch_ladder() in fleet-search (wq#2494).

wq#2481 shipped a 4-rung fetch ladder that only escalates on RETRY_STATUSES
(403/429/503). That assumed a WAF announces itself in the HTTP status, which
is false for a rendered challenge/block page: rung 3 (_fetch_chromium) has no
status of its own and synthesises 200 from exit-0 + non-empty DOM, and rungs
1-2 can serve a 200-status block page too. wq#2487/wq#2492 reproduced this as
a silent ok:true false positive (crunchbase.com) and a misleading generic
"extracted no content" failure (g2.com). These tests cover the fix: body-text
signature matching that synthesises 403 so a challenge page keeps escalating,
and names the challenge + tier when the ladder exhausts on one.

Run: python3 -m pytest tests/test_fetch_ladder_challenge.py -v
     (or: python3 tests/test_fetch_ladder_challenge.py)
"""
import importlib.machinery
import importlib.util
import os

HERE = os.path.dirname(os.path.abspath(__file__))
SCRIPT = os.path.join(HERE, "..", "fleet-search")


def _load_fleet_search():
    loader = importlib.machinery.SourceFileLoader("fleet_search_under_test_challenge", SCRIPT)
    spec = importlib.util.spec_from_loader(loader.name, loader)
    mod = importlib.util.module_from_spec(spec)
    loader.exec_module(mod)
    return mod


fs = _load_fleet_search()
detect = fs._detect_challenge


# ---------- _detect_challenge signature matching ----------

def test_cloudflare_block_page_detected():
    body = (b"<html><body>Sorry, you have been blocked. This website is using a "
            b"security service to protect itself. Ray ID: 8a1b2c3d</body></html>")
    assert detect(body) == "cloudflare"


def test_cloudflare_js_challenge_detected():
    body = b"<html><body>Checking your browser before accessing example.com.</body></html>"
    assert detect(body) == "cloudflare"


def test_turnstile_detected():
    body = b'<div class="cf-turnstile" data-sitekey="x"></div>'
    assert detect(body) == "turnstile"


def test_datadome_detected():
    body = b"window.DataDome = {cid: 'abc'};"
    assert detect(body) == "datadome"


def test_akamai_detected():
    body = b"document.cookie = 'ak_bmsc=xyz; path=/';"
    assert detect(body) == "akamai"


def test_perimeterx_detected():
    body = b"<script src='https://client.perimeterx.net/abc'></script>"
    assert detect(body) == "perimeterx"


def test_real_content_not_flagged():
    body = b"<html><body><h1>Anthropic</h1><p>Real article content about AI safety.</p></body></html>"
    assert detect(body) is None


def test_case_insensitive_match():
    assert detect(b"RAY ID: DEADBEEF") == "cloudflare"


# ---------- _fetch_ladder: challenge page synthesises 403 ----------

def _mock_rungs(mod, results):
    """results: dict tier -> return value (None means unavailable)."""
    mod._get = lambda url, headers=None, timeout=20: results.get(1)
    mod._fetch_cffi_chrome = lambda url, timeout=20: results.get(2)
    mod._fetch_chromium = lambda url, timeout=20: results.get(3)
    mod._fetch_flaresolverr = lambda url, timeout=20: results.get(4)


def test_200_challenge_page_synthesised_to_403_and_exhausts():
    challenge_body = b"using a security service to protect itself. Ray ID: 123"
    _mock_rungs(fs, {1: (200, challenge_body, "http://x")})
    status, body, final, tier, challenge = fs._fetch_ladder("http://x")
    assert status == 403
    assert tier == 1
    assert challenge == "cloudflare"


def test_200_challenge_at_rung3_escalates_to_rung4():
    challenge_body = b"DataDome protection active"
    real_body = b"<html><body>real page</body></html>"
    _mock_rungs(fs, {1: (403, b"forbidden", "http://x"),
                     2: (403, b"forbidden", "http://x"),
                     3: (200, challenge_body, "http://x"),
                     4: (200, real_body, "http://x")})
    status, body, final, tier, challenge = fs._fetch_ladder("http://x")
    assert status == 200
    assert tier == 4
    assert challenge is None
    assert body == real_body


def test_genuine_200_not_flagged_stops_ladder():
    _mock_rungs(fs, {1: (200, b"<html>real content here</html>", "http://x")})
    status, body, final, tier, challenge = fs._fetch_ladder("http://x")
    assert status == 200
    assert tier == 1
    assert challenge is None


def test_genuine_403_without_challenge_body_stays_generic():
    _mock_rungs(fs, {1: (403, b"Forbidden", "http://x")})
    status, body, final, tier, challenge = fs._fetch_ladder("http://x")
    assert status == 403
    assert challenge is None


# ---------- _fetch_and_extract: envelope naming the challenge ----------

def test_fetch_and_extract_names_challenge_and_tier_on_exhaustion():
    challenge_body = b"using a security service to protect itself. Ray ID: 123"
    fs._fetch_ladder = lambda url: (403, challenge_body, url, 3, "cloudflare")
    payload, err = fs._fetch_and_extract("http://crunchbase.com/organization/anthropic")
    assert payload is None
    assert err["tier"] == 3
    assert err["challenge"] == "cloudflare"
    assert "cloudflare" in err["error"]
    assert "tier 3" in err["error"]
    assert "extracted no content" not in err["error"]


def test_fetch_and_extract_generic_403_keeps_old_message():
    fs._fetch_ladder = lambda url: (403, b"forbidden", url, 1, None)
    payload, err = fs._fetch_and_extract("http://example.com")
    assert payload is None
    assert "challenge" not in err
    assert err["error"].startswith("http 403 blocked")


def test_fetch_and_extract_empty_real_page_keeps_old_message():
    fs._fetch_ladder = lambda url: (200, b"<html></html>", url, 3, None)
    payload, err = fs._fetch_and_extract("http://example.com/empty")
    assert payload is None
    assert err["error"] == "fetched OK but extracted no content/comments"


if __name__ == "__main__":
    import sys
    failures = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
                print(f"PASS {name}")
            except AssertionError as e:
                failures += 1
                print(f"FAIL {name}: {e}")
    sys.exit(1 if failures else 0)
