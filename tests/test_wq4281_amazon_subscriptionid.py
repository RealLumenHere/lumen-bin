"""wq#4281 / ISSUE-LOG row 1437: an Amazon PA-API/affiliate link embeds the
publisher's own public AWS access key id as the SubscriptionId query param (plus
an affiliate tag), and search engines index those URLs verbatim. A fleet-search
web hit on any Amazon listing then false-triggers the AKIA mask tripwire for a
value that was never ours (wq#4267/wq#4268 attempt 1, settled via wq#4279).

fleet-search:searxng_search/brave_api_search/deepseek_search must strip that
noise (via _clean_amazon_url/_clean_amazon_noise, fleet-search:~264-290) from
every result url and snippet before it reaches cache, prior-art, or emit.

Builds the SubscriptionId value at runtime (never a bare AKIA literal) per the
mask-tripwire-probe rule.

Run: python3 -m pytest tests/test_wq4281_amazon_subscriptionid.py -v
"""
import importlib.machinery
import importlib.util
import os

HERE = os.path.dirname(os.path.abspath(__file__))
SCRIPT = os.path.join(HERE, "..", "fleet-search")


def _load_fleet_search():
    loader = importlib.machinery.SourceFileLoader("fleet_search_under_test_wq4281", SCRIPT)
    spec = importlib.util.spec_from_loader(loader.name, loader)
    mod = importlib.util.module_from_spec(spec)
    loader.exec_module(mod)
    return mod


fs = _load_fleet_search()

# Split-built, never a bare literal (wq#2795/wq#2839 class).
_SUBSCRIPTION_ID = "AKI" + "A" + "Q" * 16
_AFFILIATE_TAG = "lumen" + "research-20"


def _fixture_amazon_url():
    """A committed fixture result URL shaped exactly like the one that false-fired
    on wq#4267/wq#4268 attempt 1: a real Amazon product link with SubscriptionId
    and an affiliate tag query param."""
    return ("https://www.amazon.com/Polaris-Freedom-Robotic-Cleaner/dp/B07ABCDEFG"
            "?SubscriptionId=" + _SUBSCRIPTION_ID + "&tag=" + _AFFILIATE_TAG + "&qid=1700000000")


def test_clean_amazon_url_strips_subscriptionid_and_tag():
    cleaned = fs._clean_amazon_url(_fixture_amazon_url())
    assert _SUBSCRIPTION_ID not in cleaned
    assert "SubscriptionId" not in cleaned
    assert "tag=" not in cleaned
    assert "qid=1700000000" in cleaned          # unrelated params survive
    assert cleaned.startswith("https://www.amazon.com/Polaris-Freedom-Robotic-Cleaner/dp/B07ABCDEFG")


def test_clean_amazon_url_leaves_non_amazon_urls_untouched():
    url = "https://example.com/path?SubscriptionId=" + _SUBSCRIPTION_ID
    assert fs._clean_amazon_url(url) == url


def test_clean_amazon_noise_scrubs_url_embedded_in_snippet_text():
    snippet = "Buy it here: " + _fixture_amazon_url() + " (prime eligible)"
    cleaned = fs._clean_amazon_noise(snippet)
    assert _SUBSCRIPTION_ID not in cleaned
    assert "prime eligible" in cleaned


def test_searxng_search_end_to_end_strips_subscriptionid(monkeypatch):
    """Fixture: a full SearXNG json response (the shape _get_json returns) carrying
    one Amazon result with SubscriptionId/tag noise, same as the live wq#4279 hit."""
    fixture_response = {
        "results": [
            {
                "title": "Polaris Freedom Robotic Pool Cleaner",
                "url": _fixture_amazon_url(),
                "content": "See it on Amazon: " + _fixture_amazon_url(),
                "engine": "google",
            }
        ],
        "unresponsive_engines": [],
    }
    monkeypatch.setattr(fs, "_get_json", lambda *a, **k: (200, fixture_response, None))
    results, err, unresponsive = fs.searxng_search("Polaris Freedom pool cleaner")
    assert err is None
    assert unresponsive == []
    assert len(results) == 1
    assert _SUBSCRIPTION_ID not in results[0]["url"]
    assert _SUBSCRIPTION_ID not in results[0]["snippet"]
    assert "SubscriptionId" not in results[0]["url"]
