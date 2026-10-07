#!/usr/bin/env python3
"""provider_watch.py — wq#3336.

Expands wq#3335's cf-changelog-watch (Cloudflare-only) into a general provider-watch
system: one script, one state file, one config table of sources, N providers. The
problem it solves is the same one wq#3335 named — we missed the official MCP server
stack for months while hand-rolling a broken OAuth implementation — generalized past
Cloudflare: every provider we build against ships things we'd want to adopt and don't
hear about until weeks later (wq#3336).

Providers/sources configured (see SOURCES below; verified live 2026-10-01):
  * cloudflare — blog RSS, changelog RSS, 2 watched npm packages (wq#3335's originals,
    kept here for completeness/testability but EXCLUDED from DEFAULT_PROVIDERS —
    cf-changelog-watch.timer already covers Cloudflare under its own state file;
    including it here too would file duplicate tickets. See DESIGN.md.)
  * tailscale  — blog RSS (tailscale.com/blog/index.xml) + GitHub releases
  * ollama     — GitHub releases
  * litellm    — GitHub releases + PyPI
  * anthropic  — GitHub releases + npm, both on anthropics/claude-code /
    @anthropic-ai/claude-code (the harness this fleet itself runs on). The API/platform
    changelog (platform.claude.com/docs/en/release-notes/overview) was considered and
    rejected: it's a single evolving URL with no per-entry pages, so a sitemap <loc> diff
    (which only notices new URLs, not edits to an existing one) would never see a new
    entry land — verified 2026-10-01 via `curl -s platform.claude.com/sitemap.xml`.
  * cerebras   — sitemap.xml filtered to /blog/ (cerebras.ai has no RSS feed — verified
    via fleet-search, confirmed by a live 404 on every common RSS path)
  * deepseek   — sitemap.xml filtered to /news/ (api-docs.deepseek.com has no RSS feed
    either; its "/news/" index page is client-rendered, so the sitemap is the only
    static list of announcement URLs)
  * sambanova  — blog RSS (sambanova.ai/blog/rss.xml)
  * mistral    — blog RSS (mistral.ai/rss.xml — note: NOT /news/rss.xml, which 404s)
  * openrouter — changelog RSS (openrouter.ai/docs/changelog/rss.xml, per their own
    versioning docs — the guessed /changelog/rss.xml and /rss.xml paths both 404
    behind an SPA shell that returns 200 text/html for any path)
  * langfuse   — GitHub releases
  * pikvm      — GitHub releases on pikvm/kvmd. The ticket says "PiKVM/kvmd-armbian";
    verified live that no such repo exists under the pikvm org (github.com/pikvm has
    13 repos, none named kvmd-armbian — the only GitHub hit for that name is an
    unrelated third-party fork). PiKVM's own armbian/OS build repos (pikvm/os,
    pikvm/pi-builder) don't use GitHub Releases (404 on /releases/latest). pikvm/kvmd,
    the core daemon that actually ships features, does.

Source types (see `findings_for_source` dispatch):
  rss             — RSS 2.0 feed. Optional `keywords` list filters+tiers matches
                    (used for Cloudflare only, where wq#3335 found the raw changelog
                    carries 761+ historical entries and needs filtering); omit keywords
                    and every item is a finding.
  npm / pypi      — registry "latest version" lookup, one finding iff the version
                    differs from what's been seen before.
  github_releases — GitHub REST API `/repos/{repo}/releases/latest`, one finding iff
                    the latest release tag is new. A repo with no GitHub Releases
                    (404) is a source error, not a crash.
  sitemap         — sitemap.xml, `<loc>` entries matching a required path_filter
                    substring. For providers with no RSS/Releases surface at all.

Dedup / first-run baseline / ticket filing / error handling all follow wq#3335's
design unchanged (see cf-changelog-watch, kept running standalone for Cloudflare):
sha256 dedup keys in a seen-state file; a first run baselines everything it finds
without filing (the CF changelog's 761-entry backlog is exactly why); a fetch/parse
error on one source is a warning, not a crash, and only returns nonzero if literally
every configured source errored.

Usage:
  provider_watch.py run [--providers p1,p2,...] [--state PATH] [--wq-bin PATH]
                         [--lane LANE] [--pri PRI] [--dry-run]
  provider_watch.py list-sources
"""
import argparse
import hashlib
import html
import json
import os
import re
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET

DEFAULT_STATE = "/home/lumen/logs/.provider-watch.state.json"
DEFAULT_WQ_BIN = "/home/lumen/bin/wq"
DEFAULT_LANE = "research"
DEFAULT_PRI = "p3"
USER_AGENT = "provider-watch/1.0 (+wq#3336)"

NPM_REGISTRY = "https://registry.npmjs.org"
PYPI_REGISTRY = "https://pypi.org/pypi"
GITHUB_API = "https://api.github.com"

# Cloudflare-only keyword filter, carried over from wq#3335 — the areas that burned us.
KEYWORDS = ["MCP", "OAuth", "Workers", "Agents", "Access", "KV",
            "Durable Objects", "tunnel", "cloudflared"]
# "Access" alone also matches unrelated "Early/Limited/Beta Access" program
# announcements (wq#3423 false-positived on "now available in Early Access" —
# WAF Attack Signature Detection, nothing to do with Cloudflare Access).
# Excluded via negative lookbehind rather than dropping the keyword, so a real
# Cloudflare Access post (e.g. wq#3421: "Access for Infrastructure") still matches.
_ACCESS_EXCLUDE_PREFIXES = ("Early ", "Limited ", "Beta ")


def _keyword_pattern(kw):
    base = r"\b" + re.escape(kw) + r"\b"
    if kw == "Access":
        lookbehinds = "".join(f"(?<!{re.escape(p)})" for p in _ACCESS_EXCLUDE_PREFIXES)
        return lookbehinds + base
    return base


_KEYWORD_RE = {kw: re.compile(_keyword_pattern(kw), re.IGNORECASE)
               for kw in KEYWORDS}
_HIGH = {"mcp", "oauth"}
_MEDIUM = {"access", "agents", "durable objects", "workers"}

_TAG_RE = re.compile(r"<[^>]+>")
_LOC_RE = re.compile(r"<loc>(.*?)</loc>", re.IGNORECASE | re.DOTALL)

SOURCES = [
    {"provider": "cloudflare", "type": "rss", "label": "cf-blog",
     "url": "https://blog.cloudflare.com/rss", "keywords": KEYWORDS},
    {"provider": "cloudflare", "type": "rss", "label": "cf-changelog",
     "url": "https://developers.cloudflare.com/changelog/rss/index.xml", "keywords": KEYWORDS},
    {"provider": "cloudflare", "type": "npm", "pkg": "@cloudflare/workers-oauth-provider"},
    {"provider": "cloudflare", "type": "npm", "pkg": "@cloudflare/agents"},

    {"provider": "tailscale", "type": "rss", "label": "tailscale-blog",
     "url": "https://tailscale.com/blog/index.xml"},
    {"provider": "tailscale", "type": "github_releases", "repo": "tailscale/tailscale"},

    {"provider": "ollama", "type": "github_releases", "repo": "ollama/ollama"},

    {"provider": "litellm", "type": "github_releases", "repo": "BerriAI/litellm"},
    {"provider": "litellm", "type": "pypi", "pkg": "litellm"},

    {"provider": "anthropic", "type": "github_releases", "repo": "anthropics/claude-code"},
    {"provider": "anthropic", "type": "npm", "pkg": "@anthropic-ai/claude-code"},

    {"provider": "cerebras", "type": "sitemap", "label": "cerebras-blog",
     "url": "https://www.cerebras.ai/sitemap.xml", "path_filter": "/blog/"},

    {"provider": "deepseek", "type": "sitemap", "label": "deepseek-news",
     "url": "https://api-docs.deepseek.com/sitemap.xml", "path_filter": "/news/"},

    {"provider": "sambanova", "type": "rss", "label": "sambanova-blog",
     "url": "https://sambanova.ai/blog/rss.xml"},

    {"provider": "mistral", "type": "rss", "label": "mistral-blog",
     "url": "https://mistral.ai/rss.xml"},

    {"provider": "openrouter", "type": "rss", "label": "openrouter-changelog",
     "url": "https://openrouter.ai/docs/changelog/rss.xml"},

    {"provider": "langfuse", "type": "github_releases", "repo": "langfuse/langfuse"},

    {"provider": "pikvm", "type": "github_releases", "repo": "pikvm/kvmd"},
]

DEFAULT_PROVIDERS = sorted({s["provider"] for s in SOURCES} - {"cloudflare"})


def match_keywords(text):
    return [kw for kw, rx in _KEYWORD_RE.items() if rx.search(text or "")]


def relevance(finding):
    keywords = finding.get("keywords") or []
    if keywords:
        ks = {k.lower() for k in keywords}
        if ks & _HIGH:
            return "high", ("matches MCP/OAuth — the exact gap that cost us months "
                             "(wq#3335); triage first")
        if ks & _MEDIUM:
            return "medium", "touches Access/Agents/Workers/Durable Objects — worth a read"
        return "low", f"matched watched keyword(s) {', '.join(sorted(keywords))} — skim"
    label = {
        "npm": "new release of a watched npm package",
        "pypi": "new release of a watched PyPI package",
        "github_releases": "new GitHub release of a watched repo",
        "sitemap": "new page matching a watched path",
        "rss": "new post on a watched blog/changelog",
    }.get(finding.get("source_type", ""), "new item from a watched provider source")
    return "medium", f"{label} — check for anything we should adopt"


def strip_html(text):
    return html.unescape(_TAG_RE.sub(" ", text or "")).strip()


def truncate(text, n):
    text = " ".join((text or "").split())
    return text if len(text) <= n else text[:n - 1].rstrip() + "…"


def dedup_key(*parts):
    return hashlib.sha256("|".join(parts).encode("utf-8")).hexdigest()


def fetch_url(url, timeout=15, headers=None):
    """Returns (text, error)."""
    req_headers = {"User-Agent": USER_AGENT}
    if headers:
        req_headers.update(headers)
    req = urllib.request.Request(url, headers=req_headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.read().decode("utf-8", errors="replace"), None
    except (urllib.error.URLError, OSError, ValueError) as e:
        return None, f"{type(e).__name__}: {e}"


def parse_rss(xml_text):
    """Returns (items, error). Each item: title/link/guid/description/pub_date."""
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError as e:
        return [], f"ParseError: {e}"
    items = []
    for item in root.iter("item"):
        def field(tag):
            el = item.find(tag)
            return (el.text or "").strip() if el is not None and el.text else ""
        link = field("link")
        items.append({
            "title": field("title"),
            "link": link,
            "guid": field("guid") or link,
            "description": field("description"),
            "pub_date": field("pubDate"),
        })
    return items, None


def rss_findings(provider, label, url, keywords=None, fetch=fetch_url):
    """Returns (findings, error). A fetch/parse error yields ([], error) — one source
    outage never crashes the sweep. `keywords=None` means every item is a finding
    (the default for every provider except Cloudflare, see module docstring)."""
    text, err = fetch(url)
    if err:
        return [], err
    items, perr = parse_rss(text)
    if perr:
        return [], perr
    findings = []
    for it in items:
        haystack = f"{it['title']} {it['description']}"
        matched = match_keywords(haystack) if keywords else []
        if keywords and not matched:
            continue
        findings.append({
            "provider": provider, "source": label, "source_type": "rss",
            "key": dedup_key(label, it["guid"]),
            "title": it["title"],
            "link": it["link"],
            "pub_date": it["pub_date"],
            "summary": truncate(strip_html(it["description"]), 500),
            "keywords": matched,
        })
    return findings, None


def npm_findings(provider, pkg, registry=NPM_REGISTRY, fetch=fetch_url):
    """Returns (findings, error) — a one-element list when the latest version is new."""
    url = f"{registry}/{urllib.parse.quote(pkg, safe='@/')}/latest"
    text, err = fetch(url)
    if err:
        return [], err
    try:
        doc = json.loads(text)
        version = doc["version"]
    except (ValueError, KeyError, TypeError) as e:
        return [], f"unparseable npm response for {pkg}: {type(e).__name__}: {e}"
    return [{
        "provider": provider, "source": f"npm:{pkg}", "source_type": "npm",
        "key": dedup_key("npm", pkg, version),
        "title": f"{pkg}@{version} published",
        "link": f"https://www.npmjs.com/package/{pkg}/v/{version}",
        "pub_date": "",
        "summary": f"registry latest for {pkg} is now {version}",
        "keywords": [],
    }], None


def pypi_findings(provider, pkg, registry=PYPI_REGISTRY, fetch=fetch_url):
    """Returns (findings, error) — mirrors npm_findings against PyPI's JSON API."""
    url = f"{registry}/{urllib.parse.quote(pkg)}/json"
    text, err = fetch(url)
    if err:
        return [], err
    try:
        doc = json.loads(text)
        version = doc["info"]["version"]
    except (ValueError, KeyError, TypeError) as e:
        return [], f"unparseable pypi response for {pkg}: {type(e).__name__}: {e}"
    return [{
        "provider": provider, "source": f"pypi:{pkg}", "source_type": "pypi",
        "key": dedup_key("pypi", pkg, version),
        "title": f"{pkg}=={version} published",
        "link": f"https://pypi.org/project/{pkg}/{version}/",
        "pub_date": "",
        "summary": f"PyPI latest for {pkg} is now {version}",
        "keywords": [],
    }], None


def github_releases_findings(provider, repo, api=GITHUB_API, fetch=fetch_url):
    """Returns (findings, error). A repo with zero GitHub Releases 404s at the fetch
    layer, surfacing as a normal source error — never a crash."""
    url = f"{api}/repos/{repo}/releases/latest"
    text, err = fetch(url, headers={"Accept": "application/vnd.github+json"})
    if err:
        return [], err
    try:
        doc = json.loads(text)
        tag = doc["tag_name"]
    except (ValueError, KeyError, TypeError) as e:
        return [], f"unparseable GitHub releases response for {repo}: {type(e).__name__}: {e}"
    name = doc.get("name") or tag
    title = f"{repo} {tag} released" + (f" — {name}" if name and name != tag else "")
    return [{
        "provider": provider, "source": f"github:{repo}", "source_type": "github_releases",
        "key": dedup_key("gh-release", repo, tag),
        "title": title,
        "link": doc.get("html_url") or f"https://github.com/{repo}/releases/tag/{tag}",
        "pub_date": doc.get("published_at", ""),
        "summary": truncate(doc.get("body") or "", 500),
        "keywords": [],
    }], None


def sitemap_findings(provider, label, url, path_filter, fetch=fetch_url):
    """Returns (findings, error). Extracts <loc> entries via a tolerant regex instead
    of namespace-aware XML parsing — sitemap.xml's default xmlns makes ElementTree's
    plain tag lookups miss every element, and <loc> text is always a bare URL (no
    nested markup), so the regex is simpler and just as correct here."""
    text, err = fetch(url)
    if err:
        return [], err
    locs = [html.unescape(m.strip()) for m in _LOC_RE.findall(text)]
    matched = [loc for loc in locs if path_filter in loc]
    findings = []
    for loc in matched:
        findings.append({
            "provider": provider, "source": label, "source_type": "sitemap",
            "key": dedup_key("sitemap", label, loc),
            "title": loc.rstrip("/").rsplit("/", 1)[-1] or loc,
            "link": loc,
            "pub_date": "",
            "summary": f"new page matching {path_filter!r} on {provider}'s site: {loc}",
            "keywords": [],
        })
    return findings, None


def findings_for_source(source, fetch=fetch_url):
    """Dispatch a single SOURCES entry to its type-specific fetcher."""
    stype = source["type"]
    if stype == "rss":
        return rss_findings(source["provider"], source["label"], source["url"],
                             keywords=source.get("keywords"), fetch=fetch)
    if stype == "npm":
        return npm_findings(source["provider"], source["pkg"], fetch=fetch)
    if stype == "pypi":
        return pypi_findings(source["provider"], source["pkg"], fetch=fetch)
    if stype == "github_releases":
        return github_releases_findings(source["provider"], source["repo"], fetch=fetch)
    if stype == "sitemap":
        return sitemap_findings(source["provider"], source["label"], source["url"],
                                 source["path_filter"], fetch=fetch)
    raise ValueError(f"unknown source type: {stype!r}")


def source_label(source):
    return source.get("label") or source.get("repo") or source.get("pkg") or source["type"]


def load_state(path):
    try:
        with open(path) as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def save_state(path, state):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(state, f, indent=2, sort_keys=True)
    os.replace(tmp, path)


def file_ticket(finding, wq_bin, lane, pri, dry_run=False, runner=subprocess.run):
    """Returns (ok, ticket_id_or_None). Never raises — a wq fault must not crash the
    sweep; it just means this finding stays unmarked seen and gets retried next run."""
    tier, rationale = relevance(finding)
    title = truncate(f"[provider-watch] {finding['provider']}/{finding['source']}: "
                      f"{finding['title']}", 180)
    lines = [f"Provider: {finding['provider']}", f"Source: {finding['source']}",
             f"Link: {finding['link']}"]
    if finding.get("pub_date"):
        lines.append(f"Published: {finding['pub_date']}")
    if finding.get("keywords"):
        lines.append(f"Matched keywords: {', '.join(sorted(finding['keywords']))}")
    lines.append(f"Relevance: {tier} — {rationale}")
    lines.append("")
    lines.append(finding.get("summary", ""))
    lines.append("")
    lines.append("Filed automatically by provider-watch (wq#3336) — triage and "
                 "close/forward as appropriate.")
    detail = "\n".join(lines)

    if dry_run:
        print(f"[dry-run] would file: {title}")
        return True, None

    try:
        r = runner([wq_bin, "add", "--lane", lane, "--pri", pri,
                    "--title", title, "--detail", detail],
                   capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.TimeoutExpired) as e:
        print(f"provider-watch: wq add failed to run for {title!r}: {e}", file=sys.stderr)
        return False, None
    if r.returncode != 0:
        print(f"provider-watch: wq add exited {r.returncode} for {title!r}: "
              f"{r.stdout.strip()} {r.stderr.strip()}", file=sys.stderr)
        return False, None
    m = re.search(r"added task (\d+)", r.stdout)
    ticket_id = m.group(1) if m else None
    print(f"provider-watch: filed {title!r}" + (f" as #{ticket_id}" if ticket_id else ""))
    return True, ticket_id


def run(sources, state_path=DEFAULT_STATE, wq_bin=DEFAULT_WQ_BIN, lane=DEFAULT_LANE,
        pri=DEFAULT_PRI, dry_run=False, source_fetcher=findings_for_source,
        ticket_filer=file_ticket):
    # wq#3335 smoke test (carried over): a cold run against a live, history-carrying
    # feed can match hundreds of historical entries. first_run is keyed off whether
    # load_state actually produced a "seen" map, not file existence: a *corrupt*
    # state file still exists (os.path.exists is True) but load_state degrades it to
    # {}, same as a missing file — gating on existence alone would skip the baseline
    # step and file every historical match in one run.
    state = load_state(state_path)
    first_run = "seen" not in state
    seen = dict(state.get("seen", {}))

    findings, errors = [], []
    for source in sources:
        f, err = source_fetcher(source)
        findings.extend(f)
        if err:
            errors.append(f"{source['provider']}/{source_label(source)}: {err}")

    checked = len(sources)
    now_iso = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())

    if first_run:
        new_keys = [f["key"] for f in findings if f["key"] not in seen]
        if dry_run:
            print(f"provider-watch: [dry-run] first run — would seed {len(new_keys)} "
                  f"existing match(es) as baseline, file 0")
        else:
            for finding in findings:
                seen.setdefault(finding["key"], {
                    "title": finding["title"], "provider": finding["provider"],
                    "source": finding["source"], "filed_at": now_iso,
                    "ticket": None, "seeded": True})
            state["seen"] = seen
            save_state(state_path, state)
            print(f"provider-watch: first run — seeded {len(new_keys)} existing match(es) "
                  f"as baseline, filed 0")
        for e in errors:
            print(f"provider-watch: source error: {e}", file=sys.stderr)
        return 1 if errors and len(errors) == checked else 0

    new_count = 0
    for finding in findings:
        if finding["key"] in seen:
            continue
        ok, ticket_id = ticket_filer(finding, wq_bin, lane, pri, dry_run=dry_run)
        if ok and not dry_run:
            new_count += 1
            seen[finding["key"]] = {
                "title": finding["title"],
                "provider": finding["provider"],
                "source": finding["source"],
                "filed_at": now_iso,
                "ticket": ticket_id,
            }
        elif ok:
            new_count += 1

    for e in errors:
        print(f"provider-watch: source error: {e}", file=sys.stderr)
    if not dry_run:
        state["seen"] = seen
        save_state(state_path, state)

    print(f"provider-watch: {len(findings)} match(es), {new_count} new, "
          f"{len(errors)}/{checked} source(s) errored")
    return 1 if errors and len(errors) == checked else 0


def main():
    parser = argparse.ArgumentParser(description=__doc__,
                                      formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_run = sub.add_parser("run", help="poll every configured source once, "
                                        "file wq tickets on new matches")
    p_run.add_argument("--providers", default=None,
                        help="comma-separated provider names to check "
                             f"(default: all except cloudflare — {','.join(DEFAULT_PROVIDERS)})")
    p_run.add_argument("--state", default=DEFAULT_STATE)
    p_run.add_argument("--wq-bin", default=DEFAULT_WQ_BIN)
    p_run.add_argument("--lane", default=DEFAULT_LANE)
    p_run.add_argument("--pri", default=DEFAULT_PRI)
    p_run.add_argument("--dry-run", action="store_true")

    sub.add_parser("list-sources", help="print configured providers/sources and exit")

    args = parser.parse_args()

    if args.cmd == "list-sources":
        for s in SOURCES:
            print(f"{s['provider']:12s} {s['type']:16s} {source_label(s)}")
        return 0

    if args.cmd == "run":
        providers = (args.providers.split(",") if args.providers else DEFAULT_PROVIDERS)
        known = {s["provider"] for s in SOURCES}
        unknown = sorted(set(providers) - known)
        if unknown:
            print(f"provider-watch: unknown provider(s): {', '.join(unknown)} "
                  f"(known: {', '.join(sorted(known))})", file=sys.stderr)
            return 2
        sources = [s for s in SOURCES if s["provider"] in providers]
        return run(sources, state_path=args.state, wq_bin=args.wq_bin, lane=args.lane,
                   pri=args.pri, dry_run=args.dry_run)


if __name__ == "__main__":
    sys.exit(main())
