#!/usr/bin/env python3
"""minerva_presentation_stability.py — competence-preserving perturbation stability
probe for the Minerva verification gate (wq#2086, Minerva proposal d653cc13c6a3).

Source: [arXiv 2609.16517] "Competence-Preserving Resume Perturbations Expose
Presentation Sensitivity in LLM Screening" — rewrite the surface of an input without
changing the evidence it carries, re-screen it, and count decision flips.

What this measures for Lumen: minerva_verify.verify() is the accept/reject gate on
every proposal. It scores a claim set against a fact-pack context via a LiteLLM judge
rung. If reworded-but-equivalent text flips `passed`, the gate is scoring presentation,
not grounding. Nothing else in the fleet tests that: paired_eval (agent5/dgm_v2.py)
compares variants of an AGENT, pelican_bench local model drift, model_audit external
rung correctness, predicate_audit reasoning grounding, judgearena_bench judge-vs-human
agreement, cross_agreement.py inter-rung agreement. All hold the input fixed.

Method, per item (one already-scored verification_log row = answer + its context):
  1. baseline   — re-score the UNTOUCHED answer on the rung, this run.
  2. control    — re-score the UNTOUCHED answer again. baseline != control is the
                  rung's own nondeterminism (VERIFY_JUDGE_SAMPLES=3 at temperature),
                  not presentation sensitivity. Reported as self_flip_rate.
  3. variants   — K deterministic surface transforms (formatting / structure /
                  punctuation / declared synonyms), each admitted only if it passes
                  the evidence-equality gate below. Re-score each; a flip is
                  variant.passed != baseline.passed.
  headline      — excess_flip_rate = flip_rate - self_flip_rate. Everything above the
                  rung's own noise floor is presentation sensitivity.

Evidence-equality gate (the paper's validation gate, made deterministic — two tiers,
both hard; a variant failing either is discarded, never counted as a flip):
  A. evidence tokens identical as a multiset — urls, CVE/GHSA ids, wq refs, paths,
     filenames, numbers, ALLCAPS identifiers, quoted spans.
  B. content words identical as a multiset after canonicalising the DECLARED
     synonym/contraction table. An undeclared word change (a dropped qualifier, a
     softened claim) fails: that would be a competence change, not a presentation one.

Read-only by contract:
  * items are read over a `mode=ro` sqlite URI;
  * minerva_verify.verify() APPENDS to verification_log on every call, so the probe
    redirects minerva_verify.DB at a temp sqlite that it deletes on exit. Without that
    redirect the probe's own perturbed variants become next week's "already-scored
    items" — the corpus would eat itself. Selection also skips subject LIKE 'pstab:%'
    as a second fence.
  * VERIFY_ALERT=0: the probe is not production traffic, so a judge error here must not
    page as a production judge outage. Judge errors are counted and reported here.

Usage:
  python3 minerva_presentation_stability.py                 # probe + report
  python3 minerva_presentation_stability.py --json          # machine-readable
  python3 minerva_presentation_stability.py --ids 4190,4191 # pin the item set
  python3 minerva_presentation_stability.py --rungs qwen36-local   # force rung(s)
  python3 minerva_presentation_stability.py --dry-run       # gate/transform audit, no scoring
  python3 minerva_presentation_stability.py --selftest      # invariants, no network
Env: PSTAB_ENABLED=0 disables (timer no-op), PSTAB_DB, PSTAB_LIMIT, PSTAB_VARIANTS,
     PSTAB_MAX_SCORES, PSTAB_FLIP_THRESHOLD, PSTAB_MINERVA_DIR, PSTAB_NTFY_BIN,
     PSTAB_LANGFUSE_ENV, PSTAB_FOREMAN_DIR.
"""
import argparse
import collections
import json
import os
import re
import sqlite3
import subprocess
import sys
import tempfile
import textwrap
import uuid
from datetime import datetime, timezone

DB = os.environ.get("PSTAB_DB", "/home/lumen/minerva/minerva.db")
MINERVA_DIR = os.environ.get("PSTAB_MINERVA_DIR", "/home/lumen/minerva")
FOREMAN_DIR = os.environ.get("PSTAB_FOREMAN_DIR", "/home/lumen/foreman")
LANGFUSE_ENV = os.environ.get("PSTAB_LANGFUSE_ENV",
                              "/home/lumen/.secrets/langfuse-foreman.env")
NTFY_BIN = os.environ.get("PSTAB_NTFY_BIN", "/home/lumen/bin/ntfy-send")
# The rung to probe is TODAY'S production judge, not the rung a row was historically
# scored on. verification_log rows from before wq#1501 carry deepseek-v4-flash, which is
# PAID, and the standing constraint is no paid-model spend on the judge (minerva-eval
# drop-in 40-judge-model.conf). --rungs overrides, deliberately and visibly.
PROD_RUNG = os.environ.get("VERIFY_JUDGE_MODEL", "qwen36-local")
# Defaults sized for that rung: qwen36-local is a CPU llama-server on Isis that also
# serves LPP (~10-15s/sample warm, 60-150s cold). 3 items x (2 baselines + 2 variants)
# = 12 scores. MAX_SCORES is the hard stop; each score() is VERIFY_JUDGE_SAMPLES calls.
LIMIT = int(os.environ.get("PSTAB_LIMIT", "3"))
VARIANTS = int(os.environ.get("PSTAB_VARIANTS", "2"))
MAX_SCORES = int(os.environ.get("PSTAB_MAX_SCORES", "20"))
FLIP_THRESHOLD = float(os.environ.get("PSTAB_FLIP_THRESHOLD", "0.25"))
MIN_CONTEXT_CHARS = 200          # shorter context_ref values are refs, not fact packs
MIN_ANSWER_CHARS = 120           # too short to perturb meaningfully

# ---------------------------------------------------------------- evidence gate

EVIDENCE_PATTERNS = (
    r"https?://\S+",                                   # urls
    r"\b(?:CVE|GHSA)-[A-Za-z0-9.-]+\b",                # advisory ids
    r"\bwq#\d+\b",                                     # ticket refs
    r"(?:/[A-Za-z0-9_.-]+){2,}",                       # absolute paths
    r"\b[A-Za-z0-9_-]+\.(?:py|sh|db|json|ya?ml|md|service|timer|conf|log)\b",
    r"\b[A-Z][A-Z0-9_]{2,}\b",                         # ALLCAPS identifiers
    r"(?<![\w.])\d+(?:[.:]\d+)*%?",                    # numbers, versions, percents
    r'"[^"\n]{1,80}"',                                 # quoted spans
    r"`[^`\n]{1,80}`",
)

# Declared equivalences. A transform may only introduce a wording change that appears
# here; the tier-B gate canonicalises both sides through this table, so anything NOT
# declared is caught as a competence change.
# Every pair must be a true substitution in both directions: an alt that is a substring
# of its own canon (e.g. "in order to"/"to") would rewrite itself on canonicalisation
# and fail the gate it exists to satisfy, so no such pair belongs here.
SYNONYMS = (
    ("does not", "doesn't"),
    ("do not", "don't"),
    ("is not", "isn't"),
    ("cannot", "can not"),
    ("currently", "at present"),
    ("approximately", "about"),
    ("utilises", "uses"),
    ("utilizes", "uses"),
)

# Deliberately narrow: articles, conjunctions, prepositions, copulas and discourse glue
# only. Negations, modals and quantifiers ("not", "no", "may", "only", "all", "most")
# stay OUT of this set and therefore COUNT — dropping a negation or softening "does not"
# to "may not" is a competence change, and a gate that ignored those words would wave it
# through as presentation.
STOPWORDS = frozenset("""a an the and or but if then than that this these those of in on at
to for with by from as is are was were be been being it its their there here s t""".split())

# Sentence order is only shuffled when no sentence opens with a back-reference — a
# reordered "This also means ..." would change what the text asserts.
BACKREF_OPENERS = ("this", "that", "these", "those", "it", "they", "such", "hence",
                   "therefore", "however", "also", "then", "thus", "consequently",
                   "instead", "moreover", "additionally", "conversely")

_SENTENCE_SPLIT = re.compile(r"(?<=[.!?])\s+(?=[\"'(\[]?[A-Z0-9])")


def evidence_fingerprint(text):
    """Multiset of evidence-bearing tokens (tier-A gate)."""
    counts = collections.Counter()
    for pat in EVIDENCE_PATTERNS:
        for m in re.finditer(pat, text):
            counts[m.group(0).rstrip(".,;:)")] += 1
    return counts


def canonicalise(text):
    out = text
    for canon, alt in SYNONYMS:
        out = re.sub(r"(?<!\w)%s(?!\w)" % re.escape(alt), canon, out, flags=re.I)
    return out


def content_fingerprint(text):
    """Multiset of content words after canonicalising declared equivalences (tier-B)."""
    words = re.findall(r"[A-Za-z][A-Za-z'-]{2,}", canonicalise(text).lower())
    return collections.Counter(w for w in words if w not in STOPWORDS)


def competence_preserved(original, variant):
    """(ok, reason). Both gate tiers, evaluated deterministically — no model involved."""
    if variant.strip() == original.strip():
        return False, "identical"
    ev_o, ev_v = evidence_fingerprint(original), evidence_fingerprint(variant)
    if ev_o != ev_v:
        lost = sorted((ev_o - ev_v).elements())[:3]
        gained = sorted((ev_v - ev_o).elements())[:3]
        return False, "evidence_differs(lost=%s,gained=%s)" % (lost, gained)
    if content_fingerprint(original) != content_fingerprint(variant):
        co, cv = content_fingerprint(original), content_fingerprint(variant)
        lost = sorted((co - cv).elements())[:3]
        gained = sorted((cv - co).elements())[:3]
        return False, "content_differs(lost=%s,gained=%s)" % (lost, gained)
    return True, "ok"


# ------------------------------------------------------- surface perturbations

def sentences(text):
    return [s for s in (p.strip() for p in _SENTENCE_SPLIT.split(text.strip())) if s]


def t_reflow(text, seed):
    """Formatting: unwrap a hard-wrapped text, or hard-wrap a single-line one. Real eval
    items are one long line (verification_log 4196), so collapsing alone was a no-op."""
    if "\n" in text.strip():
        return re.sub(r"\s+", " ", text).strip()
    return textwrap.fill(text.strip(), width=72, break_long_words=False,
                         break_on_hyphens=False)


def t_bulletize(text, seed):
    """Structure: prose -> one bullet per sentence (or bullets -> prose if already a list)."""
    lines = [ln.strip() for ln in text.strip().splitlines() if ln.strip()]
    if sum(1 for ln in lines if ln.startswith(("-", "*", "•"))) >= 2:
        return " ".join(re.sub(r"^[-*•]\s*", "", ln) for ln in lines)
    sents = sentences(text)
    if len(sents) < 2:
        return text
    return "\n".join("- " + s for s in sents)


def _has_backref(sentence):
    """A demonstrative/pronoun in the opening words binds to the preceding sentence."""
    return any(w in BACKREF_OPENERS
               for w in re.findall(r"[a-z']+", sentence.lower())[:4])


def t_reorder(text, seed):
    """Structure: rotate sentence order. Declined when any sentence opens with a
    back-reference — rotating "None of these currently test ..." (the shape real eval
    items take) would change what the text asserts, not just how it reads."""
    sents = sentences(text)
    if len(sents) < 2 or any(_has_backref(s) for s in sents):
        return text
    k = 1 + (seed % (len(sents) - 1))
    return " ".join(sents[k:] + sents[:k])


def t_punctuate(text, seed):
    """Formatting: punctuation and markdown emphasis style only."""
    out = text.replace(" — ", " - ").replace("—", " - ")
    out = re.sub(r";\s+", ". ", out)
    out = re.sub(r"\*\*(.+?)\*\*", r"\1", out)
    out = re.sub(r"[ \t]{2,}", " ", out)
    if out.strip() == text.strip():        # nothing to restyle: use typewriter spacing
        out = re.sub(r"([.!?]) (?=[\"'(\[]?[A-Z0-9])", r"\1  ", text)
    return out


def t_resplit(text, seed):
    """Structure: break one comma-joined clause out into its own sentence, or unwrap one
    parenthetical into apposition. Punctuation only — no word is added or dropped."""
    out = re.sub(r",\s+and\s+", ". And ", text, count=1)
    if out.strip() == text.strip():
        out = re.sub(r"\s+\(([^()\n]{1,60})\)", r", \1,", text, count=1)
    return out


def t_synonym(text, seed):
    """Wording: declared equivalences only (see SYNONYMS / tier-B gate)."""
    out = text
    for canon, alt in SYNONYMS:
        out = re.sub(r"(?<!\w)%s(?!\w)" % re.escape(canon), alt, out, flags=re.I)
    return out


TRANSFORMS = (("reflow", t_reflow), ("bulletize", t_bulletize),
              ("punctuate", t_punctuate), ("resplit", t_resplit),
              ("reorder", t_reorder), ("synonym", t_synonym))


def build_variants(text, seed, k):
    """Up to k gate-admitted variants, plus the rejects for the audit trail.
    Deterministic in (text, seed, k): the transform order is fixed."""
    admitted, rejected = [], []
    for name, fn in TRANSFORMS:
        try:
            cand = fn(text, seed)
        except Exception as e:                    # a transform must never kill the run
            rejected.append((name, "transform_error(%s)" % str(e)[:60]))
            continue
        ok, why = competence_preserved(text, cand)
        if ok:
            admitted.append((name, cand))
        else:
            rejected.append((name, why))
        if len(admitted) >= k:
            break
    return admitted, rejected


# ------------------------------------------------------------------ item input

def load_items(db_path, limit, ids=None):
    """Already-scored verification_log rows, read-only. context_ref holds the full
    fact-pack text (verified: rows 4192-4196 carry the LUMEN CURRENT STATE pack)."""
    uri = "file:%s?mode=ro" % db_path
    conn = sqlite3.connect(uri, uri=True)
    conn.row_factory = sqlite3.Row
    sql = ("SELECT id, subject, input_text, context_ref, model, mode, passed, "
           "final_score, citation_score, judge_score, ts FROM verification_log "
           "WHERE input_text IS NOT NULL AND context_ref IS NOT NULL "
           "AND (error_kind IS NULL OR error_kind = '') "
           "AND COALESCE(subject,'') NOT LIKE 'pstab:%' ")
    if ids:
        sql += "AND id IN (%s) ORDER BY id DESC" % ",".join("?" * len(ids))
        rows = conn.execute(sql, [int(i) for i in ids]).fetchall()
    else:
        sql += "ORDER BY id DESC LIMIT ?"
        rows = conn.execute(sql, (limit * 4,)).fetchall()
    conn.close()
    items = []
    for r in rows:
        if len(r["context_ref"] or "") < MIN_CONTEXT_CHARS:
            continue                              # a ref string, not a replayable pack
        if len(r["input_text"] or "") < MIN_ANSWER_CHARS:
            continue
        items.append(dict(r))
        if not ids and len(items) >= limit:
            break
    return items


# --------------------------------------------------------------------- scoring

def make_minerva_scorer(minerva_dir, log_db):
    """The real gate, with its verification_log append redirected at log_db."""
    sys.path.insert(0, minerva_dir)
    os.environ["VERIFY_ALERT"] = "0"
    import minerva_verify as mv
    mv.DB = log_db
    with sqlite3.connect(log_db) as conn:         # give the append somewhere harmless
        conn.execute("""CREATE TABLE IF NOT EXISTS verification_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT, subject TEXT, input_text TEXT,
            context_ref TEXT, citation_score REAL, judge_score REAL, final_score REAL,
            passed INTEGER, rejected_reason TEXT, model TEXT, ts TEXT,
            judge_rationale TEXT, error_kind TEXT, mode TEXT, n_claims INTEGER,
            claims_entailed INTEGER, span_confidence TEXT, judge_ms TEXT)""")

    def score(answer, context, mode, rung, subject):
        mv.JUDGE_MODEL = rung
        mv.JUDGE_MODEL_2 = ""                     # pin the rung: no silent fallback
        r = mv.verify(answer, context, subject=subject, model=rung, mode=mode or "semantic")
        return {"passed": bool(r["passed"]), "final": float(r["final_score"]),
                "citation": float(r["citation_score"]), "judge": float(r["judge_score"]),
                "error": r.get("error_kind") or None}
    return score


def probe(items, score, variants=VARIANTS, max_scores=MAX_SCORES, rungs=None,
          dry_run=False):
    """Returns (per_rung_metrics, item_records, budget_note)."""
    records, spent, budget_note = [], 0, None
    for item in items:
        for rung in (rungs or [PROD_RUNG]):
            admitted, rejected = build_variants(item["input_text"], item["id"], variants)
            rec = {"id": item["id"], "subject": (item["subject"] or "")[:70],
                   "rung": rung, "mode": item["mode"], "variants": [],
                   "rejected": [{"transform": n, "reason": w} for n, w in rejected],
                   "recorded": {"passed": bool(item["passed"]),
                                "final": item["final_score"], "rung": item["model"]}}
            if dry_run:
                rec["variants"] = [{"transform": n, "chars": len(v)} for n, v in admitted]
                records.append(rec)
                continue
            if not admitted:
                rec["skipped"] = "no_admitted_variants"
                records.append(rec)
                continue
            need = 2 + len(admitted)              # baseline + control + variants
            if spent + need > max_scores:
                budget_note = ("stopped at item %d: %d scores would exceed "
                               "PSTAB_MAX_SCORES=%d" % (item["id"], spent + need, max_scores))
                return metrics(records), records, budget_note
            ctx, mode = item["context_ref"], item["mode"]
            base = score(item["input_text"], ctx, mode, rung, "pstab:%d:baseline" % item["id"])
            ctrl = score(item["input_text"], ctx, mode, rung, "pstab:%d:control" % item["id"])
            spent += 2
            rec["baseline"], rec["control"] = base, ctrl
            rec["self_flip"] = base["passed"] != ctrl["passed"]
            for name, text in admitted:
                v = score(text, ctx, mode, rung, "pstab:%d:%s" % (item["id"], name))
                spent += 1
                rec["variants"].append({
                    "transform": name, "passed": v["passed"], "final": v["final"],
                    "flip": v["passed"] != base["passed"],
                    "delta_final": round(abs(v["final"] - base["final"]), 4),
                    "error": v["error"]})
            records.append(rec)
    return metrics(records), records, budget_note


def metrics(records):
    """Per-rung flip rates. excess_flip_rate is the headline: flips above the rung's
    own run-to-run noise floor."""
    by_rung = collections.defaultdict(lambda: {
        "items": 0, "items_scored": 0, "variants_admitted": 0, "variants_scored": 0,
        "flips": 0, "self_flips": 0, "gate_rejected": 0, "errors": 0, "deltas": []})
    for rec in records:
        m = by_rung[rec["rung"]]
        m["items"] += 1
        m["gate_rejected"] += len(rec["rejected"])
        m["variants_admitted"] += len(rec["variants"])
        if "baseline" not in rec:
            continue                   # dry run, or an item with nothing admitted
        m["items_scored"] += 1
        m["self_flips"] += int(rec["self_flip"])
        m["errors"] += sum(1 for k in ("baseline", "control") if rec[k]["error"])
        for v in rec["variants"]:
            m["variants_scored"] += 1
            m["flips"] += int(v["flip"])
            m["deltas"].append(v["delta_final"])
            m["errors"] += int(bool(v["error"]))
    out = {}
    for rung, m in by_rung.items():
        nv, ni = m["variants_scored"], m["items_scored"]
        flip = (m["flips"] / nv) if nv else None
        self_flip = (m["self_flips"] / ni) if ni else None
        out[rung] = {
            "items": m["items"], "items_scored": ni,
            "variants_admitted": m["variants_admitted"], "variants_scored": nv,
            "gate_rejected": m["gate_rejected"], "errors": m["errors"],
            "flip_rate": round(flip, 4) if flip is not None else None,
            "self_flip_rate": round(self_flip, 4) if self_flip is not None else None,
            "excess_flip_rate": (round(flip - self_flip, 4)
                                 if flip is not None and self_flip is not None else None),
            "mean_abs_delta_final": (round(sum(m["deltas"]) / len(m["deltas"]), 4)
                                     if m["deltas"] else None),
            "max_abs_delta_final": max(m["deltas"]) if m["deltas"] else None,
        }
    return out


# ------------------------------------------------------------------- reporting

def format_report(met, records, budget_note):
    lines = ["presentation-stability (wq#2086) %s"
             % datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")]
    if not met:
        lines.append("  no items scored")
    for rung in sorted(met):
        m = met[rung]
        lines.append("  %s: flip=%s self=%s excess=%s  dfinal mean=%s max=%s"
                     % (rung, m["flip_rate"], m["self_flip_rate"], m["excess_flip_rate"],
                        m["mean_abs_delta_final"], m["max_abs_delta_final"]))
        lines.append("    items=%d scored=%d admitted=%d variants=%d gate_rejected=%d "
                     "errors=%d"
                     % (m["items"], m["items_scored"], m["variants_admitted"],
                        m["variants_scored"], m["gate_rejected"], m["errors"]))
    for rec in records:
        flips = [v["transform"] for v in rec["variants"] if v.get("flip")]
        if flips:
            lines.append("    FLIP item=%s rung=%s by=%s (%s)"
                         % (rec["id"], rec["rung"], ",".join(flips), rec["subject"]))
    if budget_note:
        lines.append("  budget: %s" % budget_note)
    return "\n".join(lines)


def worst_excess(met):
    vals = [(m["excess_flip_rate"], r) for r, m in met.items()
            if m["excess_flip_rate"] is not None]
    return max(vals) if vals else (None, None)


def notify(met, records, budget_note, threshold=FLIP_THRESHOLD, ntfy_bin=NTFY_BIN):
    """ntfy only when the gate looks presentation-sensitive, or the probe could not
    score anything. Returns the title sent, or None."""
    excess, rung = worst_excess(met)
    all_errored = bool(met) and all(
        m["variants_scored"] == 0 or m["errors"] >= m["variants_scored"]
        for m in met.values())
    if excess is not None and excess >= threshold:
        title = "minerva gate is presentation-sensitive"
        body = ("%s: excess flip rate %.2f >= %.2f on competence-preserving rewrites\n%s"
                % (rung, excess, threshold, format_report(met, records, budget_note)))
    elif all_errored or not met:
        title = "presentation-stability probe scored nothing"
        body = format_report(met, records, budget_note)
    else:
        return None
    try:
        subprocess.run([ntfy_bin, "-p", "default", "-T", title, body[:1200]],
                       capture_output=True, text=True, timeout=10)
    except Exception:
        pass
    return title


def emit_langfuse(met, records, foreman_dir=FOREMAN_DIR, env_path=LANGFUSE_ENV):
    """Best-effort, never load-bearing (harness/langfuse_client contract). One trace
    for the run, one span per rung. Returns events shipped, or None."""
    try:
        sys.path.insert(0, os.path.join(foreman_dir, "harness"))
        from langfuse_client import emit_trace_with_span
    except Exception:
        return None
    trace_id = str(uuid.uuid4())
    shipped = 0
    for rung in sorted(met):
        try:
            n = emit_trace_with_span(
                env_path, trace_id, "presentation-stability",
                "rung:%s" % rung, met[rung], tags=["wq2086", "minerva", "stability"],
                trace_metadata={"items": [r["id"] for r in records],
                                "probe": "minerva_presentation_stability"})
            shipped += n or 0
        except Exception:
            return None
    return shipped


# -------------------------------------------------------------------- selftest

def selftest():
    """Gate + transform invariants. No DB, no network, no model (install.sh health)."""
    text = ("Minerva  evaluates proposals via minerva-eval.timer (minerva_eval.py) and\n"
            "scores them with minerva_verify.py; coverage is approximately 0.5 on wq#2086.\n\n"
            "The judge rung does not test surface stability, and no harness reads\n"
            "/home/lumen/minerva/minerva.db for a second opinion.\n\n"
            "Full details live at https://example.org/a for review.\n")
    changed = 0
    for name, fn in TRANSFORMS:
        cand = fn(text, 7)
        assert fn(text, 7) == cand, "%s must be deterministic" % name
        if cand.strip() == text.strip():
            continue                   # declining is legitimate: nothing to restyle
        changed += 1
        ok, why = competence_preserved(text, cand)
        assert ok, "%s must preserve competence, got %s" % (name, why)
    assert changed == len(TRANSFORMS), "fixture must exercise every transform: %d" % changed

    oneline = re.sub(r"\s+", " ", text).strip()          # the live corpus shape
    wrapped = t_reflow(oneline, 0)
    assert "\n" in wrapped and competence_preserved(oneline, wrapped)[0], wrapped
    dropped = text.replace("does not ", "")
    assert not competence_preserved(text, dropped)[0], "dropped negation must fail gate"
    renumbered = text.replace("0.5", "0.9")
    ok, why = competence_preserved(text, renumbered)
    assert not ok and why.startswith("evidence_differs"), why
    unurl = text.replace("https://example.org/a", "https://example.org/b")
    assert not competence_preserved(text, unurl)[0], "changed url must fail gate"
    softened = text.replace("does not test", "may not always test")
    assert not competence_preserved(text, softened)[0], "undeclared wording must fail gate"
    backref = "It does not hold. This also fails. Numbers are 1 and 2 here."
    assert t_reorder(backref, 3) == backref, "back-reference text must not be reordered"

    admitted, rejected = build_variants(text, 11, 3)
    assert len(admitted) == 3, admitted
    assert build_variants(text, 11, 3)[0] == admitted, "variant set must be reproducible"

    # flip accounting: variant 'reflow' flips, control agrees with baseline
    calls = []

    def fake_score(answer, context, mode, rung, subject):
        calls.append(subject)
        flip = subject.endswith(":reflow")
        return {"passed": not flip, "final": 0.4 if flip else 0.8,
                "citation": 0.6, "judge": 0.7, "error": None}

    items = [{"id": 1, "subject": "s", "input_text": text, "context_ref": "c" * 300,
              "model": "recorded-rung", "mode": "semantic", "passed": 1, "final_score": 0.8}]
    met, recs, note = probe(items, fake_score, variants=3, max_scores=99, rungs=["rung-x"])
    assert recs[0]["recorded"]["rung"] == "recorded-rung", recs[0]
    m = met["rung-x"]
    assert m["variants_scored"] == 3 and m["flip_rate"] == round(1 / 3, 4), m
    assert m["self_flip_rate"] == 0.0 and m["excess_flip_rate"] == round(1 / 3, 4), m
    assert m["max_abs_delta_final"] == 0.4, m
    assert note is None and len(calls) == 5, (note, calls)
    assert list(probe(items, fake_score, 1, 99)[0]) == [PROD_RUNG], "default is prod rung"

    met2, _, note2 = probe(items, fake_score, variants=3, max_scores=4, rungs=["rung-x"])
    assert met2 == {} and "PSTAB_MAX_SCORES" in note2, (met2, note2)
    assert notify({}, [], None, ntfy_bin="/bin/true") is not None, "empty run must alert"
    assert notify(met, recs, None, threshold=0.9, ntfy_bin="/bin/true") is None
    assert notify(met, recs, None, threshold=0.1, ntfy_bin="/bin/true") is not None
    print("selftest OK")
    return 0


# ------------------------------------------------------------------------ main

def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--ids", help="comma-separated verification_log ids (pins the set)")
    ap.add_argument("--limit", type=int, default=LIMIT)
    ap.add_argument("--variants", type=int, default=VARIANTS)
    ap.add_argument("--max-scores", type=int, default=MAX_SCORES)
    ap.add_argument("--rungs", help="comma-separated LiteLLM rungs (default: %s, the "
                                    "production judge rung from VERIFY_JUDGE_MODEL — NOT "
                                    "the row's recorded rung, which may be paid)"
                                    % PROD_RUNG)
    ap.add_argument("--db", default=DB)
    ap.add_argument("--dry-run", action="store_true",
                    help="show gate admissions/rejections, score nothing")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--no-ntfy", action="store_true")
    ap.add_argument("--no-langfuse", action="store_true")
    ap.add_argument("--fail-on-finding", action="store_true",
                    help="exit 1 when excess_flip_rate >= threshold. OFF under the timer: "
                         "a finding is an ntfy, not a failed unit for the healer to retry")
    ap.add_argument("--selftest", action="store_true")
    args = ap.parse_args(argv)

    if args.selftest:
        return selftest()
    if os.environ.get("PSTAB_ENABLED", "1") == "0":
        print("presentation-stability disabled (PSTAB_ENABLED=0)")
        return 0

    ids = [s.strip() for s in args.ids.split(",") if s.strip()] if args.ids else None
    items = load_items(args.db, args.limit, ids)
    if not items:
        print("no replayable verification_log items found in %s" % args.db, file=sys.stderr)
        return 1
    rungs = [s.strip() for s in args.rungs.split(",") if s.strip()] if args.rungs else None

    if args.dry_run:
        met, records, note = probe(items, None, args.variants, args.max_scores, rungs,
                                   dry_run=True)
    else:
        tmp = tempfile.mkdtemp(prefix="pstab-")
        log_db = os.path.join(tmp, "throwaway.db")
        try:
            score = make_minerva_scorer(MINERVA_DIR, log_db)
            met, records, note = probe(items, score, args.variants, args.max_scores, rungs)
        finally:
            for p in (log_db, tmp):               # the probe leaves no state behind
                try:
                    os.remove(p) if os.path.isfile(p) else os.rmdir(p)
                except OSError:
                    pass

    if args.json:
        print(json.dumps({"metrics": met, "items": records, "budget_note": note,
                          "ts": datetime.now(timezone.utc).isoformat()}, indent=2))
    else:
        print(format_report(met, records, note))
    if args.dry_run:
        return 0
    if not args.no_langfuse:
        emit_langfuse(met, records)
    if not args.no_ntfy:
        sent = notify(met, records, note)
        if sent:
            print("ntfy: %s" % sent)
    if not any(m["variants_scored"] for m in met.values()):
        print("scored nothing: no item produced a scorable variant", file=sys.stderr)
        return 1
    excess, _ = worst_excess(met)
    if args.fail_on_finding and excess is not None and excess >= FLIP_THRESHOLD:
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
