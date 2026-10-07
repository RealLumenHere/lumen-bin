#!/usr/bin/env python3
"""cf_changelog_watch.py — wq#3335.

We missed the official MCP server stack (workers-oauth-provider, McpAgent, CF
Access SaaS MCP) for months while hand-rolling a broken OAuth implementation.
This polls Cloudflare's own release surfaces and files a wq ticket on
anything new that touches an area we've been burned by before, so the next
"someone already shipped this" is a daily diff, not a months-later surprise.

Sources (verified live 2026-10-01 — see README for how each URL was found):
  * CF blog RSS:        https://blog.cloudflare.com/rss
  * CF changelog RSS:   https://developers.cloudflare.com/changelog/rss/index.xml
                         (the documented /changelog/rss.xml 301s here)
  * watched npm packages, via the registry's own `<pkg>/latest` endpoint
    (no npm CLI dependency, just stdlib urllib): @cloudflare/workers-oauth-provider,
    @cloudflare/agents.

Keyword filter (RSS only — a watched npm package is relevant by the mere fact
it moved, no text to filter): MCP, OAuth, Workers, Agents, Access, KV,
Durable Objects, tunnel, cloudflared. Matched case-insensitively on word
boundaries against title + description.

Dedup: sha256(source + identifying string) — RSS item guid/link, npm
"pkg@version" — written to a seen-hashes state file so a run never refiles
something it already filed. Read-only against Cloudflare/npm: the only write
is the state file and, on a new match, one `wq add`.

Usage:
  cf_changelog_watch.py run [--state PATH] [--wq-bin PATH] [--lane LANE]
                            [--pri PRI] [--dry-run]
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

DEFAULT_STATE = "/home/lumen/logs/.cf-changelog-watch.state.json"
DEFAULT_WQ_BIN = "/home/lumen/bin/wq"
DEFAULT_LANE = "research"
DEFAULT_PRI = "p3"
USER_AGENT = "cf-changelog-watch/1.0 (+wq#3335; https://blog.cloudflare.com, https://developers.cloudflare.com/changelog/)"

RSS_SOURCES = [
    ("cf-blog", "https://blog.cloudflare.com/rss"),
    ("cf-changelog", "https://developers.cloudflare.com/changelog/rss/index.xml"),
]
NPM_PACKAGES = ["@cloudflare/workers-oauth-provider", "@cloudflare/agents"]
NPM_REGISTRY = "https://registry.npmjs.org"

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

# relevance tiers: the areas that actually burned us (MCP/OAuth) outrank a
# generic keyword hit.
_HIGH = {"mcp", "oauth"}
_MEDIUM = {"access", "agents", "durable objects", "workers"}

_TAG_RE = re.compile(r"<[^>]+>")


def match_keywords(text):
    return [kw for kw, rx in _KEYWORD_RE.items() if rx.search(text or "")]


def relevance(keywords):
    ks = {k.lower() for k in keywords}
    if ks & _HIGH:
        return "high", ("matches MCP/OAuth — the exact gap that cost us months last "
                         "time (wq#3335); triage first")
    if ks & _MEDIUM:
        return "medium", "touches Access/Agents/Workers/Durable Objects — worth a read"
    if ks:
        return "low", f"matched watched keyword(s) {', '.join(sorted(keywords))} — skim"
    return "medium", "new release of a watched npm package — check its changelog/diff"


def strip_html(text):
    return html.unescape(_TAG_RE.sub(" ", text or "")).strip()


def truncate(text, n):
    text = " ".join((text or "").split())
    return text if len(text) <= n else text[:n - 1].rstrip() + "…"


def dedup_key(*parts):
    return hashlib.sha256("|".join(parts).encode("utf-8")).hexdigest()


def fetch_url(url, timeout=15):
    """Returns (text, error)."""
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
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


def rss_findings(source_label, url, fetch=fetch_url):
    """Returns (findings, error). A fetch/parse error yields ([], error) — the
    caller treats that as one source outage, never a crash (other sources and
    npm packages still get checked)."""
    text, err = fetch(url)
    if err:
        return [], err
    items, perr = parse_rss(text)
    if perr:
        return [], perr
    findings = []
    for it in items:
        haystack = f"{it['title']} {it['description']}"
        keywords = match_keywords(haystack)
        if not keywords:
            continue
        findings.append({
            "source": source_label,
            "key": dedup_key(source_label, it["guid"]),
            "title": it["title"],
            "link": it["link"],
            "pub_date": it["pub_date"],
            "summary": truncate(strip_html(it["description"]), 500),
            "keywords": keywords,
        })
    return findings, None


def npm_findings(pkg, registry=NPM_REGISTRY, fetch=fetch_url):
    """Returns (findings, error) — a one-element list when the latest version is
    new, [] when the fetch/parse fails (caller counts that as a source error)."""
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
        "source": f"npm:{pkg}",
        "key": dedup_key("npm", pkg, version),
        "title": f"{pkg}@{version} published",
        "link": f"https://www.npmjs.com/package/{pkg}/v/{version}",
        "pub_date": "",
        "summary": f"registry latest for {pkg} is now {version}",
        "keywords": [],
    }], None


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
    """Returns (ok, ticket_id_or_None). Never raises — a wq fault must not
    crash the sweep; it just means this finding stays unmarked seen and gets
    retried next run (file_ticket is the only thing that mutates `seen`)."""
    tier, rationale = relevance(finding["keywords"])
    title = truncate(f"[cf-changelog] {finding['source']}: {finding['title']}", 180)
    lines = [f"Source: {finding['source']}", f"Link: {finding['link']}"]
    if finding.get("pub_date"):
        lines.append(f"Published: {finding['pub_date']}")
    if finding.get("keywords"):
        lines.append(f"Matched keywords: {', '.join(sorted(finding['keywords']))}")
    lines.append(f"Relevance: {tier} — {rationale}")
    lines.append("")
    lines.append(finding.get("summary", ""))
    lines.append("")
    lines.append("Filed automatically by cf-changelog-watch (wq#3335) — triage and "
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
        print(f"cf-changelog-watch: wq add failed to run for {title!r}: {e}",
              file=sys.stderr)
        return False, None
    if r.returncode != 0:
        print(f"cf-changelog-watch: wq add exited {r.returncode} for {title!r}: "
              f"{r.stdout.strip()} {r.stderr.strip()}", file=sys.stderr)
        return False, None
    m = re.search(r"added task (\d+)", r.stdout)
    ticket_id = m.group(1) if m else None
    print(f"cf-changelog-watch: filed {title!r}" + (f" as #{ticket_id}" if ticket_id else ""))
    return True, ticket_id


def run(state_path=DEFAULT_STATE, wq_bin=DEFAULT_WQ_BIN, lane=DEFAULT_LANE,
        pri=DEFAULT_PRI, rss_sources=None, npm_packages=None, dry_run=False,
        rss_fetcher=rss_findings, npm_fetcher=npm_findings, ticket_filer=file_ticket):
    rss_sources = RSS_SOURCES if rss_sources is None else rss_sources
    npm_packages = NPM_PACKAGES if npm_packages is None else npm_packages

    # wq#3335 smoke test: a cold run against the live changelog RSS matched 761
    # historical entries (it carries the full changelog history, not just recent
    # posts) — filing one ticket per match on day one would flood the queue.
    # No state file yet means this install has never run before, so baseline
    # everything it sees right now as "seen" without filing, and only alert on
    # what shows up from the NEXT run on. A missing/corrupt state file always
    # reads as "first run" (load_state already degrades the same way on a
    # corrupt file), so a lost state file re-baselines instead of re-flooding.
    first_run = not os.path.exists(state_path)

    state = load_state(state_path)
    seen = dict(state.get("seen", {}))

    findings, errors = [], []
    for label, url in rss_sources:
        f, err = rss_fetcher(label, url)
        findings.extend(f)
        if err:
            errors.append(f"{label}: {err}")
    for pkg in npm_packages:
        f, err = npm_fetcher(pkg)
        findings.extend(f)
        if err:
            errors.append(f"npm {pkg}: {err}")

    checked = len(rss_sources) + len(npm_packages)
    now_iso = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())

    if first_run:
        new_keys = [f["key"] for f in findings if f["key"] not in seen]
        if dry_run:
            print(f"[dry-run] first run — would seed {len(new_keys)} existing match(es) "
                  f"as baseline, file 0")
        else:
            for finding in findings:
                seen.setdefault(finding["key"], {
                    "title": finding["title"], "source": finding["source"],
                    "filed_at": now_iso, "ticket": None, "seeded": True})
            state["seen"] = seen
            save_state(state_path, state)
            print(f"cf-changelog-watch: first run — seeded {len(new_keys)} existing match(es) "
                  f"as baseline, filed 0")
        for e in errors:
            print(f"cf-changelog-watch: source error: {e}", file=sys.stderr)
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
                "source": finding["source"],
                "filed_at": now_iso,
                "ticket": ticket_id,
            }
        elif ok:
            new_count += 1

    for e in errors:
        print(f"cf-changelog-watch: source error: {e}", file=sys.stderr)
    if not dry_run:
        state["seen"] = seen
        save_state(state_path, state)

    print(f"cf-changelog-watch: {len(findings)} keyword match(es), {new_count} new, "
          f"{len(errors)}/{checked} source(s) errored")
    # A source outage is a warning (next run retries it), never a crash of the
    # whole sweep — but if literally nothing could be checked, say so with a
    # nonzero exit rather than a silent, falsely-clean "0 new".
    return 1 if errors and len(errors) == checked else 0


def main():
    parser = argparse.ArgumentParser(description=__doc__,
                                      formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="cmd", required=True)
    p_run = sub.add_parser("run", help="poll every source once, file wq tickets on new matches")
    p_run.add_argument("--state", default=DEFAULT_STATE)
    p_run.add_argument("--wq-bin", default=DEFAULT_WQ_BIN)
    p_run.add_argument("--lane", default=DEFAULT_LANE)
    p_run.add_argument("--pri", default=DEFAULT_PRI)
    p_run.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    if args.cmd == "run":
        return run(state_path=args.state, wq_bin=args.wq_bin, lane=args.lane,
                   pri=args.pri, dry_run=args.dry_run)


if __name__ == "__main__":
    sys.exit(main())
