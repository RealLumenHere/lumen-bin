#!/usr/bin/env python3
"""lumen_contradiction_scan.py — wq#1609.

Root gap: `lumen_dream.superseded_ips()` keeps a closed fact only when
`IP_RE.fullmatch(value)` succeeds, so of 316 closed facts only 6 are visible to
the one staleness detector the fleet owns. `status` (54), `power_plug` (15),
`role` (10), `powers_node` (6), `lan_ip` (4) and the rest are invisible, and the
weekly dream report prints "Stale prose flagged (0)" while dead values sit live
in prose and in machine-retrieved notes (research/MINERVA_STALENESS_REVIEW.md
§2.4, §3.5).

This replaces the IP filter with a dead-value index over *every* closed fact,
matched by value shape:

  structured value  (<=60 chars, ^[A-Za-z0-9._:/@-]+$, fact_class != narrative)
                    -> exact token match with value-aware word boundaries
  prose value       (>60 chars, or fact_class=narrative)
                    -> never string-matched; that is how a scanner earns a
                       reputation for noise (§3.5)

A structured value is matched globally only when its shape is unique enough to
carry its own subject (IPv4, MAC, dotted host, absolute path, systemd unit,
URL). Every other token — `active`, `retired`, `dead`, `133` — is matched only
on a line/note that also names the fact's subject, because those words carry no
subject on their own.

Three suppressors run on each candidate hit:

  negation   the line already frames the value as past ("was", "no longer",
             "retired", "superseded by") -> skip.       [pre-existing behaviour]
  changelog  the *current* value appears in the same line/note -> the text is
             documenting the transition, not asserting the dead value -> skip.
  as-of      the line/note carries a date earlier than the fact's valid_to ->
             it is a correct historical record -> downgrade to informational.

Artifact reach and the action tier each one gets (§3.5):

  archival_notes                       note_contradictions row  (machine-fixed)
  LIVE_FLEET_STATE*.md                 regenerate (derived file)
  storage_manifest.yaml, fleet YAML    aq row + wq ticket (owned config)
  FLEET_MAP.md, playbooks              proposal in the report
  claude_memory.md, instincts.yaml     proposal only — standing law, never edited

Read-only by default. `--apply` performs only the machine tiers (note rows,
state-regen, aq/wq); claude_memory.md and instincts.yaml are never written by
this tool under any flag.

Exit 0 = ran clean, 2 = the scan itself broke, 1 = actionable hits (only with
--strict, so the daily timer does not flap red on a finding it already reported).

  lumen-contradiction-scan [--json] [--apply] [--strict] [--db PATH]
  lumen-contradiction-scan --selftest
"""
import argparse
import json
import os
import re
import sqlite3
import subprocess
import sys
from datetime import datetime, timezone

MINERVA_DB = os.environ.get("MINERVA_DB", "/home/lumen/minerva/minerva.db")
AQ = "/home/lumen/bin/aq"
WQ = "/home/lumen/bin/wq"
STATE_REGEN = "/home/lumen/bin/state-regen"

MAX_VALUE_LEN = 60
TOKEN_RE = re.compile(r"^[A-Za-z0-9._:/@\-]+$")

# Resale prices are volatile by design (VOLATILE_HALFLIFE); a doc quoting last
# month's eBay price is not fleet staleness.
VOLATILE_PREDICATES = {"price", "actual_load", "pr_count"}

# Shapes that carry their own subject, so they may be matched anywhere.
SELF_IDENTIFYING = (
    re.compile(r"^\d{1,3}(\.\d{1,3}){3}$"),
    re.compile(r"^[0-9a-fA-F]{2}(:[0-9a-fA-F]{2}){5}$"),
    re.compile(r"^/[A-Za-z0-9._/-]{6,}$"),
    re.compile(r"^[A-Za-z0-9._-]+\.(service|timer|socket|target|mount)$"),
    re.compile(r"^[a-z][a-z0-9+.-]*://\S+$"),
    re.compile(r"^[A-Za-z0-9._-]+\.[A-Za-z0-9._-]+\.[A-Za-z]{2,}$"),
)

NEGATION_RE = re.compile(
    r"\bwas\b|\bwere\b|\bformerly\b|\bpreviously\b|\bused to\b|\bno longer\b"
    r"|\bretired\b|\bdecommission|\bsupersed|\breplaced by\b|\bdeprecat"
    r"|\bold\b|\bex-|\buntil\b|\bwent\b|\bstopped\b|\bmoved (?:from|off)\b"
    r"|\bstale\b|\bdead\b|\bdo not use\b|\bremoved\b|\bgone\b",
    re.IGNORECASE,
)

DATE_RE = re.compile(r"\b(20\d{2})-(\d{2})-(\d{2})\b")

TIER_NOTE = "note_row"
TIER_REGEN = "regenerate"
TIER_TICKET = "ticket"
TIER_PROPOSAL = "proposal"
TIER_PROPOSAL_ONLY = "proposal_only"

FILE_ARTIFACTS = (
    ("/home/lumen/minerva/context/LIVE_FLEET_STATE.md", TIER_REGEN),
    ("/home/lumen/minerva/context/LIVE_FLEET_STATE_FULL.md", TIER_REGEN),
    ("/home/lumen/agents/storage_manifest.yaml", TIER_TICKET),
    ("/home/lumen/fleet/inventory/machines/*.yaml", TIER_TICKET),
    ("/home/lumen/docs/fleet/retired_services.yaml", TIER_TICKET),
    ("/home/lumen/FLEET_MAP.md", TIER_PROPOSAL),
    ("/home/lumen/claude_memory.md", TIER_PROPOSAL_ONLY),
    ("/home/lumen/instincts.yaml", TIER_PROPOSAL_ONLY),
)

WRITE_TIERS = {TIER_NOTE, TIER_REGEN, TIER_TICKET}


def log(msg):
    ts = datetime.now(timezone.utc).strftime("%H:%M:%S UTC")
    print(f"[{ts}] {msg}", flush=True)


def connect(path, readonly=True):
    mode = "ro" if readonly else "rw"
    return sqlite3.connect(f"file:{path}?mode={mode}", uri=True)


# ------------------------------------------------------------ dead-value index

def is_structured(value, fact_class):
    if fact_class == "narrative":
        return False
    return len(value) <= MAX_VALUE_LEN and bool(TOKEN_RE.fullmatch(value))


def self_identifying(value):
    """Whether a value carries its own subject. `100.88.71.47`,
    `lumen-brain.service` and `embeddings-host` do; `active`, `dead` and `P3`
    are ordinary words or bare labels that mean nothing on their own.

    Shouting does not make a word self-identifying (wq#1629): `RETIRED` says
    nothing about WHICH thing is retired, so it is matched subject-scoped like
    its lowercase twin. This used to be `value.isupper() and value.isalpha()`,
    which was harmless only while status values were prose the index could not
    read at all — once wq#1629 split them into tokens it put 763 unscoped
    English words into the index and the daily scan went 14 -> 777 hits."""
    if any(rx.fullmatch(value) for rx in SELF_IDENTIFYING):
        return True
    return len(value) >= 8 and bool(re.search(r"[0-9._:/@-]", value))


PREDICATE_STOPWORDS = {"the", "and", "for", "has", "was"}


def predicate_cues(predicate):
    """Words that show the text is talking about *this* predicate. `active` and
    `dead` are ordinary English; `pbox … active` is only a status claim when the
    surrounding text is about status."""
    parts = [p for p in re.split(r"[_\-]", predicate) if len(p) >= 3]
    cues = {predicate, predicate.replace("_", " ")}
    cues.update(p for p in parts if p.lower() not in PREDICATE_STOPWORDS)
    return sorted(cues)


def subject_tokens(subject):
    forms = {subject, subject.replace("_", "-"), subject.replace("-", "_")}
    return sorted(t for t in forms if len(t) >= 3)


def boundary_re(value):
    """Word boundaries that respect the value alphabet. `100.88.71.47` must not
    match inside `100.88.71.470` and `active` must not match `inactive`, but a
    sentence-final `100.88.71.47.` and a YAML `hathor:` must still match — so a
    dot only blocks when something alphanumeric follows it."""
    return re.compile(
        r"(?<![A-Za-z0-9_-])(?<![A-Za-z0-9]\.)"
        + re.escape(value)
        + r"(?![A-Za-z0-9_-])(?!\.[A-Za-z0-9])",
        re.IGNORECASE,
    )


def changelog_tokens(current):
    """The tokens whose presence proves a line is documenting the transition.

    A structured current value is one token. A prose current value ('WORKING
    post EC-reset 2026-07-16 …') is not string-matchable as a whole, but its
    self-identifying tokens and its shouted status words are."""
    if not current:
        return []
    if is_structured(current, None):
        return [current]
    tokens = []
    for raw in current.replace(",", " ").replace(";", " ").split():
        tok = raw.strip("()[]'\"“”‘’.:")
        if len(tok) < 3 or not TOKEN_RE.fullmatch(tok):
            continue
        if self_identifying(tok) or (tok.isupper() and tok.isalpha()):
            tokens.append(tok)
    return tokens


def build_dead_value_index(conn):
    """One entry per closed fact whose value is structured enough to match."""
    open_now = {}
    for subj, pred, val in conn.execute(
        "select subject, predicate, value from state_facts where valid_to is null"
    ):
        open_now[(subj, pred)] = val

    index = []
    for fid, subj, pred, val, valid_to, fclass in conn.execute(
        "select id, subject, predicate, value, valid_to, fact_class "
        "from state_facts where valid_to is not null"
    ):
        if pred in VOLATILE_PREDICATES or not is_structured(val, fclass):
            continue
        current = open_now.get((subj, pred))
        value_re = boundary_re(val)
        # `hathor.power_plug` closed 'P3' and reopened as 'P3 (relabelled …)' is
        # an annotation, not a supersession: the value is still live.
        if current is not None and value_re.search(current):
            continue
        index.append(
            {
                "fact_id": fid,
                "subject": subj,
                "predicate": pred,
                "dead_value": val,
                "current_value": current,
                "valid_to": valid_to,
                "scoped": not self_identifying(val),
                "value_re": value_re,
                "subject_res": [boundary_re(t) for t in subject_tokens(subj)],
                "cue_res": [boundary_re(c) for c in predicate_cues(pred)],
                "current_res": [boundary_re(t) for t in changelog_tokens(current)],
            }
        )
    return index


# ------------------------------------------------------------------- scanning

def negated(text, dead_value):
    """A negation word other than the dead value itself — `dead` is both a
    negation cue and a live `fan_status` value, and a value may not negate its
    own assertion."""
    return any(m.group(0).lower() != dead_value.lower()
               for m in NEGATION_RE.finditer(text))


def earliest_date(text):
    dates = DATE_RE.findall(text)
    return min(("-".join(d) for d in dates), default=None)


def dated_before(text, valid_to):
    if not valid_to:
        return False
    seen = earliest_date(text)
    return bool(seen and seen < valid_to[:10])


def scan_text(text, index, location, tier, subject_context="", changelog_context=""):
    """Every hit the dead-value index finds in one line of text, with the
    suppressors already applied.

    `subject_context` widens *only* the subject test — a YAML value sits under
    its block key and a note's subject is often only in its title. Negation
    stays line-scoped; `changelog_context` widens the changelog suppressor to
    the whole note, per §3.5."""
    hits = []
    for entry in index:
        if not entry["value_re"].search(text):
            continue
        if entry["scoped"]:
            scope = text + "\n" + subject_context
            if not any(rx.search(scope) for rx in entry["subject_res"]):
                continue
            if not any(rx.search(scope) for rx in entry["cue_res"]):
                continue
        if negated(text, entry["dead_value"]):
            continue
        if any(rx.search(text) or (changelog_context and rx.search(changelog_context))
               for rx in entry["current_res"]):
            continue
        hits.append(
            {
                "location": location,
                "tier": tier,
                "subject": entry["subject"],
                "predicate": entry["predicate"],
                "dead_value": entry["dead_value"],
                "current_value": entry["current_value"],
                "valid_to": entry["valid_to"],
                "fact_id": entry["fact_id"],
                "severity": "info" if dated_before(text, entry["valid_to"]) else "action",
                "excerpt": text.strip()[:240],
            }
        )
    return hits


def enclosing_context(lines, idx, lookback=40):
    """The lines a value inherits its subject from: the chain of less-indented
    ancestors above it (YAML block keys) plus the nearest markdown heading."""
    indent = len(lines[idx]) - len(lines[idx].lstrip())
    ancestors = []
    for line in reversed(lines[max(0, idx - lookback):idx]):
        if not line.strip():
            continue
        here = len(line) - len(line.lstrip())
        if line.lstrip().startswith("#") and here == 0:
            ancestors.append(line)
            break
        if here < indent:
            ancestors.append(line)
            indent = here
            if here == 0:
                break
    return "".join(ancestors)


def scan_file(path, index, tier):
    try:
        with open(path, encoding="utf-8", errors="replace") as fh:
            lines = fh.readlines()
    except OSError:
        return []
    hits = []
    for n, line in enumerate(lines, 1):
        hits += scan_text(line, index, f"{path}:{n}", tier,
                          subject_context=enclosing_context(lines, n - 1))
    return hits


def scan_files(index, artifacts=FILE_ARTIFACTS):
    import glob

    hits = []
    for pattern, tier in artifacts:
        for path in sorted(glob.glob(pattern)):
            hits += scan_file(path, index, tier)
    return hits


def scan_notes(conn, index, window=2):
    """Notes are matched line by line, not whole-note: a 60-line note that
    mentions `thor` in its header and the word `dead` forty lines later is not
    asserting `thor.fan_status = dead`. The subject may come from the title or
    from a couple of lines either side; the changelog suppressor sees the whole
    note."""
    hits = []
    rows = conn.execute(
        "select id, title, content from archival_notes "
        "where revoked_at is null and superseded_by is null"
    )
    for note_id, title, content in rows:
        whole = f"{title or ''}\n{content or ''}"
        lines = whole.splitlines()
        seen = set()
        for i, line in enumerate(lines):
            near = "\n".join(lines[max(0, i - window):i + window + 1])
            for hit in scan_text(line, index, f"archival_notes:{note_id}",
                                 TIER_NOTE,
                                 subject_context=f"{title or ''}\n{near}",
                                 changelog_context=whole):
                if hit["fact_id"] in seen:
                    continue
                seen.add(hit["fact_id"])
                hits.append(hit)
    return hits


# -------------------------------------------------------------------- actions

def ensure_contradictions_table(conn):
    conn.execute(
        """create table if not exists note_contradictions (
               id            integer primary key autoincrement,
               note_id       text not null,
               fact_id       text not null,
               subject       text not null,
               predicate     text not null,
               dead_value    text not null,
               current_value text,
               valid_to      text,
               severity      text not null,
               excerpt       text,
               detected_at   text not null,
               resolved_at   text,
               unique(note_id, fact_id) on conflict ignore
           )"""
    )
    conn.commit()


def write_note_contradictions(conn, hits):
    ensure_contradictions_table(conn)
    now = datetime.now(timezone.utc).isoformat()
    written = 0
    for hit in hits:
        note_id = hit["location"].split(":", 1)[1]
        cur = conn.execute(
            "insert or ignore into note_contradictions "
            "(note_id, fact_id, subject, predicate, dead_value, current_value,"
            " valid_to, severity, excerpt, detected_at) "
            "values (?,?,?,?,?,?,?,?,?,?)",
            (
                note_id,
                hit["fact_id"],
                hit["subject"],
                hit["predicate"],
                hit["dead_value"],
                hit["current_value"],
                hit["valid_to"],
                hit["severity"],
                hit["excerpt"],
                now,
            ),
        )
        written += cur.rowcount
    conn.commit()
    return written


def run(cmd, timeout=120):
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return p.returncode, (p.stdout or "") + (p.stderr or "")
    except Exception as exc:
        return 255, f"EXEC_FAILED: {exc}"


def regenerate_state(hits):
    if not hits:
        return "no derived-file hits"
    rc, out = run([STATE_REGEN])
    return f"state-regen rc={rc} {out.strip()[:120]}"


def file_config_ticket(hits):
    """One ticket per owned-config artifact, deduped by wq's own title dedup."""
    filed = []
    by_path = {}
    for hit in hits:
        by_path.setdefault(hit["location"].split(":")[0], []).append(hit)
    for path, path_hits in sorted(by_path.items()):
        title = f"[minerva] stale superseded values in {os.path.basename(path)}"
        detail = (
            f"lumen-contradiction-scan found {len(path_hits)} superseded fact "
            f"value(s) still asserted in {path}. Each line cites a value the "
            f"state store closed:\n"
            + "\n".join(
                f"- {h['location']}: {h['subject']}.{h['predicate']} = "
                f"{h['dead_value']!r} (closed {h['valid_to']}, "
                f"current {h['current_value']!r})"
                for h in path_hits[:20]
            )
            + "\n(auto-filed by lumen-contradiction-scan, wq#1609)"
        )
        rc, out = run([WQ, "add", title, "--lane", "build", "--pri", "3",
                       "--detail", detail, "--archetype", "standard-engineering"])
        run([AQ, "add", "--source", "lumen-contradiction-scan",
             "--severity", "warn", "--message", title])
        filed.append(f"{path}: rc={rc} {out.strip()[:80]}")
    return filed


# --------------------------------------------------------------------- report

def render(result):
    lines = [
        f"dead-value index: {result['index_size']} of {result['closed_facts']} "
        f"closed facts matchable "
        f"(was {result['ip_only_size']} under the IP-only filter)",
        f"hits: {result['actionable']} actionable, {result['informational']} "
        f"informational, across {len(result['hits'])} total",
    ]
    for tier in (TIER_NOTE, TIER_REGEN, TIER_TICKET, TIER_PROPOSAL, TIER_PROPOSAL_ONLY):
        tier_hits = [h for h in result["hits"] if h["tier"] == tier]
        if not tier_hits:
            continue
        lines.append(f"\n[{tier}] {len(tier_hits)} hit(s)")
        for hit in tier_hits[:25]:
            lines.append(
                f"  {hit['severity']:6} {hit['location']} — "
                f"{hit['subject']}.{hit['predicate']} = {hit['dead_value']!r} "
                f"(closed {hit['valid_to']}, current {hit['current_value']!r})"
            )
        if len(tier_hits) > 25:
            lines.append(f"  … {len(tier_hits) - 25} more")
    for name, outcome in result.get("actions", {}).items():
        lines.append(f"[applied] {name}: {outcome}")
    return "\n".join(lines)


def scan(db_path, apply=False, artifacts=FILE_ARTIFACTS):
    conn = connect(db_path, readonly=not apply)
    index = build_dead_value_index(conn)
    closed_values = [
        r[0] for r in conn.execute(
            "select value from state_facts where valid_to is not null"
        )
    ]
    closed = len(closed_values)
    # What lumen_dream.superseded_ips() saw: IP_RE.fullmatch over closed values.
    ip_only = sum(1 for v in closed_values if SELF_IDENTIFYING[0].fullmatch(v))

    hits = scan_notes(conn, index) + scan_files(index, artifacts)
    result = {
        "closed_facts": closed,
        "index_size": len(index),
        "ip_only_size": ip_only,
        "hits": hits,
        "actionable": sum(1 for h in hits if h["severity"] == "action"),
        "informational": sum(1 for h in hits if h["severity"] == "info"),
        "actions": {},
    }

    if apply:
        actionable = [h for h in hits if h["severity"] == "action"]
        note_hits = [h for h in actionable if h["tier"] == TIER_NOTE]
        result["actions"]["note_contradictions"] = write_note_contradictions(
            conn, note_hits
        )
        result["actions"]["state_regen"] = regenerate_state(
            [h for h in actionable if h["tier"] == TIER_REGEN]
        )
        ticket_hits = [h for h in actionable if h["tier"] == TIER_TICKET]
        if ticket_hits:
            result["actions"]["tickets"] = file_config_ticket(ticket_hits)
    conn.close()
    return result


# -------------------------------------------------------------------- selftest

def selftest():
    rc, _ = run([sys.executable, "-m", "pytest", "-q",
                 os.path.join(os.path.dirname(os.path.abspath(__file__)),
                              "test_lumen_contradiction_scan.py")])
    return rc


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--db", default=MINERVA_DB)
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--no-files", action="store_true",
                    help="scan archival_notes only, skip the file artifacts")
    ap.add_argument("--apply", action="store_true",
                    help="perform machine tiers (note rows, state-regen, aq/wq)")
    ap.add_argument("--strict", action="store_true",
                    help="exit 1 when actionable hits exist")
    ap.add_argument("--selftest", action="store_true")
    args = ap.parse_args(argv)

    if args.selftest:
        return selftest()

    try:
        result = scan(args.db, apply=args.apply,
                      artifacts=() if args.no_files else FILE_ARTIFACTS)
    except Exception as exc:
        print(f"SCAN_FAILED: {exc}", file=sys.stderr)
        return 2

    if args.json:
        print(json.dumps(result, indent=2, default=str))
    else:
        print(render(result))
    return 1 if (args.strict and result["actionable"]) else 0


if __name__ == "__main__":
    sys.exit(main())
