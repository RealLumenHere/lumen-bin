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
subject on their own. A value that is also an ordinary English word (`UP`,
`DOWN`, `FREE`) is matched **case-exactly**, in every tier: the shouted `UP` of
a status table is not the `up` of "set up the container", and case is the only
thing that says so (wq#2151, COMMON_WORD_VALUES).

Three suppressors run on each candidate hit:

  negation   the line already frames the value as past ("was", "no longer",
             "retired", "superseded by") -> skip.       [pre-existing behaviour]
  changelog  the *current* value appears in the same line/note -> the text is
             documenting the transition, not asserting the dead value -> skip.
  as-of      the line/note carries a date earlier than the fact's valid_to ->
             it is a correct historical record -> downgrade to informational.

Artifact reach and the action tier each one gets (§3.5):

  research_docs (pgvector corpus)      report only, behind --corpus  (wq#1775)
                                        [the 11k-doc corpus no staleness
                                        mechanism has ever reached. Same index,
                                        same scan_text(), three narrowings
                                        research prose needs (see below), and a
                                        doc_contradictions writer that stays off
                                        until measured precision clears zero]
  archival_notes                       note_contradictions row  (machine-fixed)
                                        [this is wq#1610: the 1686-row episodic
                                        layer had zero invalidation before this
                                        scanner generalised past the IP-only
                                        filter — scan_notes()/write_note_contra-
                                        dictions() below are that fix, and the
                                        deployed timer is what makes it real
                                        rather than a script nobody runs]
  LIVE_FLEET_STATE*.md                 regenerate (derived file)
  storage_manifest.yaml, fleet YAML    aq row + wq ticket (owned config)
  FLEET_MAP.md, playbooks              proposal in the report
  claude_memory.md, instincts.yaml     proposal only — standing law, never edited

The corpus tier (`--corpus`, wq#1775) reuses all of the above unchanged and adds
the three narrowings research prose needs, because a matcher tuned on the
fleet's terse artifacts measured 0/5 precision against prose
(research/PGVECTOR_INVALIDATION_CONTRACTS.md §4.2):

  same-line subject  no heading widening outside doc_kind=fleet_internal. A
                     60 KB report whose heading names a node is not asserting
                     that node's state 200 lines further down.
  hypothetical       a line in conditional or definitional mood ("if", "would",
                     "fraction of", "e.g.", a failure-mode table row) is not an
                     assertion -> skip.
  shouted-short      a 2-4 char all-caps value matches case-sensitively: `up`
                     in "spin up a container" is not isis.service_state = UP.

Read-only by default. `--apply` performs only the machine tiers (note rows,
state-regen, aq/wq); claude_memory.md and instincts.yaml are never written by
this tool under any flag. The corpus is read-only under `--apply` too: the only
corpus write that exists anywhere in this tool is the `doc_contradictions`
sidecar insert, and it needs its own `--corpus-apply` flag, which the timer
deliberately does not pass (wq#1775 re-scope: report-only until re-measurement
clears zero true positives). No corpus file, row or embedding is ever touched.

Exit 0 = ran clean, 2 = the scan itself broke, 1 = actionable hits (only with
--strict, and never for corpus hits, so a report-only tier cannot turn a timer
red). pgvector being unreachable WARNs and leaves the exit code alone — the
corpus half must never take the fleet-facing half down (§4.3).

  lumen-contradiction-scan [--json] [--apply] [--strict] [--db PATH]
  lumen-contradiction-scan --corpus [--corpus-apply] [--corpus-env PATH]
  lumen-contradiction-scan --selftest
"""
import argparse
import json
import os
import re
import sqlite3
import subprocess
import sys
import time
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

# Dead values that are also ordinary English words (wq#2151). `UP` is a real
# `isis.service_state` value and also the preposition in "spin up a FastAPI
# orchestrator", and subject-plus-cue is no scope at all for a word that any
# prose *about* the subject contains — it was 8 of 9 corpus hits on 2026-09-20
# and 4 of 32 fleet-tier hits the same day. These match case-exactly everywhere:
# SHOUTED_SHORT_RE below does the same job for the corpus only, and stops at 4
# characters, so `ONLINE` and `RUNNING` in a note or a YAML file need this.
# Membership test is "the lowercase form is an ordinary English word a technical
# document uses without asserting a status", which is why `P3` and `OK` are
# absent (short but not English) and `free`/`live`/`down` are present (longer,
# but English) — length is the wrong axis.
COMMON_WORD_VALUES = frozenset({
    "active", "all", "bad", "busy", "closed", "cold", "dead", "done", "down",
    "empty", "free", "full", "good", "high", "hot", "idle", "in", "inactive",
    "live", "local", "low", "main", "new", "no", "none", "off", "old", "on",
    "open", "out", "ready", "remote", "running", "set", "stopped", "unused",
    "up", "used", "yes",
})

NEGATION_RE = re.compile(
    r"\bwas\b|\bwere\b|\bformerly\b|\bpreviously\b|\bused to\b|\bno longer\b"
    r"|\bretired\b|\bdecommission|\bsupersed|\breplaced by\b|\bdeprecat"
    r"|\bold\b|\bex-|\buntil\b|\bwent\b|\bstopped\b|\bmoved (?:from|off)\b"
    r"|\bstale\b|\bdead\b|\bdo not use\b|\bremoved\b|\bgone\b",
    re.IGNORECASE,
)

DATE_RE = re.compile(r"\b(20\d{2})-(\d{2})-(\d{2})\b")

# The fourth suppressor, corpus-only (§4.2): conditional or definitional mood.
# Research prose hypothesises, defines vocabulary and argues about scenarios
# where a fleet artifact asserts, so this runs for --corpus and nowhere else —
# the fleet tiers keep exactly the three suppressors wq#1609 measured.
HYPOTHETICAL_RE = re.compile(
    r"\bif\b|\bwould\b|\bcould\b|\bshould\b|\bmight\b|\bunless\b"
    r"|\bsuppos(?:e|es|ing)\b|\bassum(?:e|es|ed|ing|ption)"
    r"|\bhypothetic|\bscenario|\bfailure mode|\bwhat if\b|\bin the event\b"
    r"|\bfor example\b|\be\.g\.|\bi\.e\.|\bfraction of\b|\bdefined as\b"
    r"|\bimagine\b|\bsimulat",
    re.IGNORECASE,
)

# A short shouted status token collides with ordinary English under the
# case-insensitive matcher: `UP` is `isis.service_state`, `up` is the word in
# "spin up a container". Measured 2026-09-20 — every one of the four hits that
# survived the first two narrowings was this (README, Measured corpus reach).
# Corpus-only, and a superset of COMMON_WORD_VALUES only below 5 characters:
# it is what still covers `OK`, `EOL`, `RMA` — shouted, short, not English.
SHOUTED_SHORT_RE = re.compile(r"^[A-Z]{2,4}$")

# A markdown table row is an assertion in a fleet artifact and a worked example
# in a report; failure-mode vocabulary is what tells the two apart.
TABLE_ROW_RE = re.compile(r"^\s*\|.*\|\s*$")
FAILURE_MODE_RE = re.compile(
    r"\bfailure\b|\bfails?\b|\brisk\b|\bstalls?\b|\bmitigat|\bsymptom\b"
    r"|\bfallback\b|\brecovery\b",
    re.IGNORECASE,
)

TIER_NOTE = "note_row"
TIER_REGEN = "regenerate"
TIER_TICKET = "ticket"
TIER_PROPOSAL = "proposal"
TIER_PROPOSAL_ONLY = "proposal_only"
TIER_DOC = "doc_row"

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

# TIER_DOC's write action exists (write_doc_contradictions) but is not in this
# set: it is reached only by the explicit --corpus-apply flag, never by --apply,
# until measured corpus precision clears zero (wq#1775 re-scope).
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
    English words into the index and the daily scan went 14 -> 777 hits.

    A bare hyphen has the same problem (wq#1781): `POWERED-DOWN` is two
    dictionary words joined by a hyphen, not a token with a subject baked in,
    so a shouted hyphenated compound is exactly as unscoped as `RETIRED` was.
    A digit or any separator other than `-` still counts on its own
    (`lumen-brain.service`, `100.88.71.47`); a lone hyphen only counts when
    the value isn't a shouted compound (`embeddings-host` does, `POWERED-DOWN`
    doesn't)."""
    if any(rx.fullmatch(value) for rx in SELF_IDENTIFYING):
        return True
    if len(value) < 8:
        return False
    if re.search(r"[0-9._:/@]", value):
        return True
    if "-" not in value:
        return False
    return not (value.isupper() and value.replace("-", "").isalpha())


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


def common_word(value):
    """Whether a value is also an ordinary English word, so its appearance in a
    sentence says nothing on its own (wq#2151). See COMMON_WORD_VALUES."""
    return value.lower() in COMMON_WORD_VALUES


def boundary_re(value, case_exact=False):
    """Word boundaries that respect the value alphabet. `100.88.71.47` must not
    match inside `100.88.71.470` and `active` must not match `inactive`, but a
    sentence-final `100.88.71.47.` and a YAML `hathor:` must still match — so a
    dot only blocks when something alphanumeric follows it.

    Matching is case-insensitive except for common-word values, where the case
    the fact recorded is the only signal left that a status is being quoted:
    the shouted `UP` of a service table is a different token from the `up` of
    "set up the Docker container"."""
    return re.compile(
        r"(?<![A-Za-z0-9_-])(?<![A-Za-z0-9]\.)"
        + re.escape(value)
        + r"(?![A-Za-z0-9_-])(?!\.[A-Za-z0-9])",
        0 if case_exact else re.IGNORECASE,
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
    open_by_subject = {}
    open_subjects_by_value = {}
    for subj, pred, val in conn.execute(
        "select subject, predicate, value from state_facts where valid_to is null"
    ):
        open_now[(subj, pred)] = val
        open_by_subject.setdefault(subj, []).append(val)
        open_subjects_by_value.setdefault(val, set()).add(subj)

    # A value simultaneously OPEN on two or more subjects can't be carrying a
    # subject of its own (wq#2850): a kernel build, OS version, or BIOS/model
    # string is fleet vocabulary, not an identifier, however many digits or
    # separators self_identifying() sees in its shape. nephthys.yaml quoting
    # '7.0.0-30-generic' isn't a claim about sobek just because sobek once
    # closed that same string — every other node still open on -30 makes it
    # shared, so it is matched subject-scoped like any ordinary word.
    shared_values = {v for v, subs in open_subjects_by_value.items() if len(subs) >= 2}

    index = []
    for fid, subj, pred, val, valid_to, fclass in conn.execute(
        "select id, subject, predicate, value, valid_to, fact_class "
        "from state_facts where valid_to is not null"
    ):
        if pred in VOLATILE_PREDICATES or not is_structured(val, fclass):
            continue
        current = open_now.get((subj, pred))
        word_like = common_word(val)
        value_re = boundary_re(val, case_exact=word_like)
        # `hathor.power_plug` closed 'P3' and reopened as 'P3 (relabelled …)' is
        # an annotation, not a supersession: the value is still live.
        if current is not None and value_re.search(current):
            continue
        # The same value re-asserted under another predicate name is a rename,
        # not a supersession (wq#1782): `gameboy.ts_ip` closed 100.118.56.59 in a
        # bulk close while `gameboy.tailscale_ip` still holds it, so every doc
        # quoting the address was flagged although it is the live value. Only
        # self-identifying values carry enough on their own to say "same thing";
        # a bare label like `active` under a different predicate proves nothing.
        if not word_like and self_identifying(val) and any(
            other and value_re.search(other)
            for other in open_by_subject.get(subj, ())
        ):
            continue
        index.append(
            {
                "fact_id": fid,
                "subject": subj,
                "predicate": pred,
                "dead_value": val,
                "current_value": current,
                "valid_to": valid_to,
                "scoped": not self_identifying(val) or val in shared_values,
                "value_re": value_re,
                "subject_res": [boundary_re(t) for t in subject_tokens(subj)],
                "cue_res": [boundary_re(c) for c in predicate_cues(pred)],
                "current_res": [boundary_re(t, case_exact=common_word(t))
                                for t in changelog_tokens(current)],
                # The same boundaries, case-sensitive. Corpus-only (§4.2).
                "case_re": re.compile(value_re.pattern),
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


def hypothetical(text):
    """Whether the line is in conditional or definitional mood, so the value in
    it is vocabulary rather than a claim. `a powered-down dGPU reduces chassis
    temperature` and `| Node failure – Isis or Hathor offline. | Pipeline
    stalls. |` are both about a state, neither asserts one."""
    if HYPOTHETICAL_RE.search(text):
        return True
    return bool(TABLE_ROW_RE.match(text) and FAILURE_MODE_RE.search(text))


def earliest_date(text):
    dates = DATE_RE.findall(text)
    return min(("-".join(d) for d in dates), default=None)


def dated_before(text, valid_to):
    if not valid_to:
        return False
    seen = earliest_date(text)
    return bool(seen and seen < valid_to[:10])


def scan_text(text, index, location, tier, subject_context="", changelog_context="",
              suppress_hypothetical=False):
    """Every hit the dead-value index finds in one line of text, with the
    suppressors already applied.

    `subject_context` widens *only* the subject test — a YAML value sits under
    its block key and a note's subject is often only in its title. Negation
    stays line-scoped; `changelog_context` widens the changelog suppressor to
    the whole note, per §3.5. `suppress_hypothetical` adds the corpus-only
    fourth suppressor (§4.2); every fleet caller leaves it off."""
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
        if suppress_hypothetical and hypothetical(text):
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


# ------------------------------------------------------------- corpus (wq#1775)

CORPUS_ENV = os.environ.get("LUMEN_AGENT_ENV", "/home/lumen/agent/.env")

DOC_KIND_FLEET = "fleet_internal"


def doc_kind(filepath):
    """Provenance from the path, deterministically, never from content (§5.1).
    The `category` column cannot serve: `classify()` puts 96.2% of the corpus
    in `other`.

    Only `fleet_internal` keeps heading-widened subjects. The other three kinds
    differ in how a contradiction *resolves* (§5.1), which is wq#1776's problem,
    not this scanner's — here they behave alike, and saying so is cheaper than
    a taxonomy the corpus has no signal for."""
    p = (filepath or "").lower()
    name = os.path.basename(p)
    if ("/foreman/" in p or "/agents/work-queue/reports/" in p
            or "/kb/" in p or "/docs/fleet/" in p):
        return DOC_KIND_FLEET
    if name.startswith(("intel_", "news_", "arxiv")) or "/papers/" in p:
        return "external_literature"
    if "/vendor/" in p or "/api-docs/" in p:
        return "vendor_doc"
    return "generated"


def corpus_env(path=CORPUS_ENV):
    env = {}
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, val = line.split("=", 1)
            env[key.strip()] = val.strip().strip('"').strip("'")
    return env


def corpus_connect(readonly=True, env_path=CORPUS_ENV):
    """A psycopg2 connection to the pgvector corpus. Read-only by default and
    for every path but `--corpus-apply`; `SET default_transaction_read_only`
    makes "the corpus is not written" a property of the session rather than a
    promise about the SQL below it."""
    import psycopg2

    env = corpus_env(env_path)
    conn = psycopg2.connect(
        host=env["LUMEN_PGVECTOR_HOST"], port=int(env["LUMEN_PGVECTOR_PORT"]),
        dbname=env["LUMEN_PGVECTOR_DB"], user=env["LUMEN_PGVECTOR_USER"],
        password=env["LUMEN_PGVECTOR_PASSWORD"],
    )
    if readonly:
        cur = conn.cursor()
        cur.execute("set default_transaction_read_only = on")
        conn.commit()
    return conn


def corpus_candidates(cur, entry):
    """The §4.2 prefilter: coarse containment in Postgres, exact matching in
    Python on the survivors. A self-identifying value carries its own subject,
    so containment alone makes a candidate; a bare label needs its subject in
    the document too. 64 index entries against 11k docs is ~110s of sequential
    ILIKE — affordable daily, never per-query."""
    like = f"%{entry['dead_value']}%"
    if entry["scoped"]:
        cur.execute(
            "select id, filepath, content from research_docs "
            "where content ilike %s and content ilike %s",
            (like, f"%{entry['subject']}%"),
        )
    else:
        cur.execute(
            "select id, filepath, content from research_docs where content ilike %s",
            (like,),
        )
    return cur.fetchall()


def chunk_at(cur, doc_id, char_start):
    """The chunk containing an offset, or None when the hit falls outside every
    chunk span. A 60 KB report with one superseded IP at character 41,000 has
    one bad chunk and thirty good ones; penalising the document penalises all
    thirty (§4.2)."""
    cur.execute(
        "select chunk_ix from research_chunks "
        "where doc_id = %s and %s between char_start and char_end "
        "order by chunk_ix limit 1",
        (doc_id, char_start),
    )
    row = cur.fetchone()
    return row[0] if row else None


def scan_corpus(cur, index):
    """Every corpus hit, localised to its chunk. Returns (hits, candidate_docs).

    Matching is wq#1609's, unchanged, with the three prose narrowings: the
    subject may not come from an enclosing heading outside `fleet_internal`,
    short shouted values must be shouted in the text, and the hypothetical
    suppressor runs."""
    hits, seen, scanned = [], set(), set()
    for entry in index:
        for doc_id, filepath, content in corpus_candidates(cur, entry):
            scanned.add(doc_id)
            kind = doc_kind(filepath)
            lines = (content or "").splitlines(keepends=True)
            offset = 0
            shouted = bool(SHOUTED_SHORT_RE.fullmatch(entry["dead_value"]))
            for n, line in enumerate(lines, 1):
                line_start, offset = offset, offset + len(line)
                # Prose must shout it back: `up` in "spin up a container" is not
                # isis.service_state, and case is the only thing that says so.
                if shouted and not entry["case_re"].search(line):
                    continue
                found = scan_text(
                    line, [entry], f"research_docs:{doc_id}:{n}", TIER_DOC,
                    subject_context=(enclosing_context(lines, n - 1)
                                     if kind == DOC_KIND_FLEET else ""),
                    changelog_context=content,
                    suppress_hypothetical=True,
                )
                if not found:
                    continue
                # scan_corpus matches one index entry at a time, so the value
                # that hit is entry's and its offset is recoverable here rather
                # than by threading positions through scan_text().
                match = entry["value_re"].search(line)
                char_start = line_start + (match.start() if match else 0)
                for hit in found:
                    key = (doc_id, hit["fact_id"], char_start)
                    if key in seen:
                        continue
                    seen.add(key)
                    hit.update(doc_id=doc_id, filepath=filepath, doc_kind=kind,
                               char_start=char_start, line_no=n)
                    hits.append(hit)
    for hit in hits:
        hit["chunk_ix"] = chunk_at(cur, hit["doc_id"], hit["char_start"])
    return hits, len(scanned)


DOC_CONTRADICTIONS_TABLE = """
create table if not exists doc_contradictions (
  id            bigserial primary key,
  doc_id        bigint not null references research_docs(id) on delete cascade,
  chunk_ix      int,
  fact_id       text not null,
  subject       text not null,
  predicate     text not null,
  dead_value    text not null,
  current_value text,
  valid_to      text,
  severity      text not null,
  char_start    int,
  excerpt       text,
  detected_at   timestamptz not null default now(),
  resolved_at   timestamptz,
  unique (doc_id, fact_id, char_start)
)"""

DOC_CONTRADICTIONS_INDEX = (
    "create index if not exists idx_doc_contra_open "
    "on doc_contradictions (doc_id) where resolved_at is null"
)


def write_doc_contradictions(conn, hits):
    """The only corpus write in this tool, and it is a sidecar: no research_docs
    or research_chunks row is read-modify-written, nothing is deleted, nothing
    is re-embedded. `fact_id` is a cross-database reference held by value
    (state_facts lives in sqlite), and the scan re-derives every row from the
    live index, so a dangling one self-heals on the next pass."""
    cur = conn.cursor()
    cur.execute(DOC_CONTRADICTIONS_TABLE)
    cur.execute(DOC_CONTRADICTIONS_INDEX)
    written = 0
    for hit in hits:
        cur.execute(
            "insert into doc_contradictions (doc_id, chunk_ix, fact_id, subject,"
            " predicate, dead_value, current_value, valid_to, severity,"
            " char_start, excerpt) values (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) "
            "on conflict (doc_id, fact_id, char_start) do nothing",
            (
                hit["doc_id"], hit.get("chunk_ix"), hit["fact_id"], hit["subject"],
                hit["predicate"], hit["dead_value"], hit["current_value"],
                hit["valid_to"], hit["severity"], hit["char_start"], hit["excerpt"],
            ),
        )
        written += max(cur.rowcount, 0)
    conn.commit()
    return written


def corpus_tier(index, apply_writes=False, env_path=CORPUS_ENV):
    """The corpus half of the scan. Returns (hits, meta) and never raises:
    pgvector being unreachable WARNs and leaves the sqlite half alone (§4.3)."""
    started = time.time()
    try:
        conn = corpus_connect(readonly=not apply_writes, env_path=env_path)
    except Exception as exc:
        # Type only: a connection error's text carries the DSN.
        log(f"WARN: corpus tier skipped — pgvector unavailable ({type(exc).__name__})")
        return [], {"status": "skipped", "reason": type(exc).__name__}
    try:
        hits, docs = scan_corpus(conn.cursor(), index)
        meta = {
            "status": "ok",
            "candidate_docs": docs,
            "bad_chunks": len({(h["doc_id"], h["chunk_ix"]) for h in hits}),
            "elapsed_s": round(time.time() - started, 1),
            "written": None,
        }
        if apply_writes:
            meta["written"] = write_doc_contradictions(
                conn, [h for h in hits if h["severity"] == "action"]
            )
        return hits, meta
    except Exception as exc:
        log(f"WARN: corpus tier failed — {type(exc).__name__}")
        return [], {"status": "failed", "reason": type(exc).__name__}
    finally:
        conn.close()


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
        rc, out = run([WQ, "add", "--title", title, "--lane", "build", "--pri", "3",
                       "--detail", detail, "--archetype", "standard-engineering"])
        run([AQ, "add", "lumen-contradiction-scan", path, title, detail])
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
    corpus = result.get("corpus", {"status": "off"})
    if corpus["status"] == "ok":
        lines.append(
            f"corpus: {corpus['candidate_docs']} candidate doc(s) prefiltered, "
            f"{corpus['bad_chunks']} chunk(s) implicated, {corpus['elapsed_s']}s"
            + (f", {corpus['written']} doc_contradictions row(s) written"
               if corpus.get("written") is not None else " (report-only)")
        )
    elif corpus["status"] in ("skipped", "failed"):
        lines.append(f"corpus: {corpus['status']} ({corpus['reason']}) — "
                     f"sqlite tiers above are unaffected")

    for tier in (TIER_NOTE, TIER_REGEN, TIER_TICKET, TIER_PROPOSAL,
                 TIER_PROPOSAL_ONLY, TIER_DOC):
        tier_hits = [h for h in result["hits"] if h["tier"] == tier]
        if not tier_hits:
            continue
        lines.append(f"\n[{tier}] {len(tier_hits)} hit(s)")
        for hit in tier_hits[:25]:
            where = hit["location"]
            if tier == TIER_DOC:
                where = (f"{os.path.basename(hit['filepath'])}"
                         f" chunk {hit['chunk_ix']} @{hit['char_start']}"
                         f" [{hit['doc_kind']}]")
            lines.append(
                f"  {hit['severity']:6} {where} — "
                f"{hit['subject']}.{hit['predicate']} = {hit['dead_value']!r} "
                f"(closed {hit['valid_to']}, current {hit['current_value']!r})"
            )
        if len(tier_hits) > 25:
            lines.append(f"  … {len(tier_hits) - 25} more")
    for name, outcome in result.get("actions", {}).items():
        lines.append(f"[applied] {name}: {outcome}")
    return "\n".join(lines)


def scan(db_path, apply=False, artifacts=FILE_ARTIFACTS, corpus=False,
         corpus_apply=False, corpus_env_path=CORPUS_ENV):
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
    corpus_meta = {"status": "off"}
    if corpus:
        corpus_hits, corpus_meta = corpus_tier(
            index, apply_writes=corpus_apply, env_path=corpus_env_path
        )
        hits += corpus_hits
    result = {
        "closed_facts": closed,
        "index_size": len(index),
        "ip_only_size": ip_only,
        "hits": hits,
        "actionable": sum(1 for h in hits if h["severity"] == "action"),
        "informational": sum(1 for h in hits if h["severity"] == "info"),
        # --strict is the timer's red light, and a report-only tier must not be
        # able to turn it on.
        "strict_actionable": sum(1 for h in hits if h["severity"] == "action"
                                 and h["tier"] != TIER_DOC),
        "corpus": corpus_meta,
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
                    help="exit 1 when actionable non-corpus hits exist")
    ap.add_argument("--corpus", action="store_true",
                    help="also scan the pgvector research corpus (report-only)")
    ap.add_argument("--corpus-apply", action="store_true",
                    help="implies --corpus; insert doc_contradictions rows. OFF "
                         "by default and not passed by the timer: corpus "
                         "precision measured 0/5 (wq#1775 re-scope)")
    ap.add_argument("--corpus-env", default=CORPUS_ENV,
                    help="env file holding the LUMEN_PGVECTOR_* connection")
    ap.add_argument("--selftest", action="store_true")
    args = ap.parse_args(argv)

    if args.selftest:
        return selftest()

    try:
        result = scan(args.db, apply=args.apply,
                      artifacts=() if args.no_files else FILE_ARTIFACTS,
                      corpus=args.corpus or args.corpus_apply,
                      corpus_apply=args.corpus_apply,
                      corpus_env_path=args.corpus_env)
    except Exception as exc:
        print(f"SCAN_FAILED: {exc}", file=sys.stderr)
        return 2

    if args.json:
        print(json.dumps(result, indent=2, default=str))
    else:
        print(render(result))
    return 1 if (args.strict and result["strict_actionable"]) else 0


if __name__ == "__main__":
    sys.exit(main())
