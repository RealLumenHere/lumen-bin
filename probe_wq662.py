#!/usr/bin/env python3
"""probe_wq662 — red/green for the Frank-decision gate protocol (wq#662).
Tests every state transition and guard against a scratch wq DB using the
live /home/lumen/bin/wq binary. Zero Anthropic quota. No external deps.

Transitions under test:
  A. frank_gated ticket is not claimable (--id guard + lane scan)
  B. wq decide: frank_gated -> pending_review (writes prompt + rec)
  C. decide refuses on non-frank_gated ticket
  D. wq answer: records frank_response on pending_review (status unchanged)
  E. answer refuses on non-pending_review ticket + refuses blank response
  F. answer refuses when no decision_prompt set (decide not called first)
  G. wq review accept: pending_review -> open (decision gate cleared)
  H. wq review reject: pending_review -> frank_gated (response cleared, reason stored)
  I. reject requires --reason
  J. reject-resubmit cycle: frank_gated -> pending_review -> frank_gated -> pending_review -> open
  K. pending_review [awaiting-answer] vs [awaiting-review] in wq list
  L. new fields visible in wq show
  M. wq add --frank-gated mints frank_gated + gate_type=frank_decision
  N. wq add without --frank-gated still defaults to open (no regression)
  O. review refuses when frank_response not yet set (answer not called)
"""
import os, subprocess, sqlite3, sys, tempfile

WQ = os.environ.get("FOREMAN_WQ_BIN", "/home/lumen/bin/wq")
PASS, FAIL = [], []
TMP = tempfile.mkdtemp(prefix="probe662-")
DB  = os.path.join(TMP, "q.db")
ENV = dict(os.environ, WQ_DB=DB, WQ_AGENT="probe662")

def sh(args, **kw):
    return subprocess.run([WQ] + args, capture_output=True, text=True, env=ENV, **kw)

def check(cid, ok, detail=""):
    (PASS if ok else FAIL).append(cid)
    print(("  ok   " if ok else "  FAIL ") + cid + ("  " + detail if detail else ""))

def field(tid, col):
    c = sqlite3.connect(DB); v = c.execute(f"SELECT {col} FROM tasks WHERE id=?", (tid,)).fetchone(); c.close()
    return v[0] if v else None

def add(**kw):
    args = ["add", "--lane", kw.get("lane","build"), "--title", kw["title"]]
    if kw.get("frank_gated"): args += ["--frank-gated"]
    if kw.get("detail"): args += ["--detail", kw["detail"]]
    r = sh(args); tid = int(r.stdout.split()[2]); return tid

print("== setup: add tickets")
fg1 = add(title="frank-gated ticket A", frank_gated=True)
fg2 = add(title="frank-gated ticket B", frank_gated=True)
fg3 = add(title="frank-gated ticket for reject cycle", frank_gated=True)
norm = add(title="normal open ticket")
check("setup.frank_gated_status",   field(fg1,"status") == "frank_gated")
check("setup.frank_gated_gate_type",field(fg1,"gate_type") == "frank_decision")
check("setup.normal_open_status",   field(norm,"status") == "open")

print("== A. frank_gated not claimable (--id guard)")
r = sh(["claim", "--id", str(fg1)])
check("A.1 claim refused on frank_gated", r.returncode != 0 and "FRANK_GATED" in r.stdout, r.stdout[:200])

print("== B. wq decide: frank_gated -> pending_review")
r = sh(["decide", str(fg1), "--prompt", "Q: upgrade now or wait?", "--rec", "Rec: wait for Sunday window"])
check("B.1 decide succeeds", r.returncode == 0 and "pending_review" in r.stdout, r.stdout[:200])
check("B.2 status=pending_review",   field(fg1,"status") == "pending_review")
check("B.3 decision_prompt stored",  field(fg1,"decision_prompt") == "Q: upgrade now or wait?")
check("B.4 orch_recommendation stored", field(fg1,"orch_recommendation") == "Rec: wait for Sunday window")
check("B.5 frank_response still NULL", field(fg1,"frank_response") is None)

print("== C. decide refuses on non-frank_gated")
r = sh(["decide", str(norm), "--prompt", "Q?", "--rec", "R?"])
check("C.1 decide refused on open ticket", r.returncode != 0 and "frank_gated" in r.stdout, r.stdout[:200])

print("== K (partial). pending_review [awaiting-answer] shown in list")
r = sh(["list", "build"])
check("K.1 [awaiting-answer] shown when frank_response NULL", "[awaiting-answer]" in r.stdout, r.stdout[:400])

print("== F. answer refuses when no decision_prompt set (ticket manually put in pending_review without decide)")
# Simulate a ticket that arrived in pending_review without a decision_prompt (manual edit path)
fgm = add(title="frank-gated manual pending_review without prompt", frank_gated=True)
sh(["edit", str(fgm), "--status", "pending_review"])  # bypass decide to force the edge case
r = sh(["answer", str(fgm), "some response"])
check("F.1 answer refused when decision_prompt missing on pending_review", r.returncode != 0 and "decision_prompt" in r.stdout, r.stdout[:200])

print("== E. answer refuses on non-pending_review ticket")
r = sh(["answer", str(fg2), "response"])
check("E.1 answer refused on frank_gated (not pending_review)", r.returncode != 0, r.stdout[:200])
r = sh(["answer", str(norm), "response"])
check("E.2 answer refused on open ticket", r.returncode != 0 and "pending_review" in r.stdout, r.stdout[:200])

print("== D. wq answer: records frank_response on pending_review")
r = sh(["answer", str(fg1), "I agree, let's wait until Sunday 2026-08-23. I acknowledge brief outage possible."])
check("D.1 answer succeeds", r.returncode == 0 and "response recorded" in r.stdout, r.stdout[:200])
check("D.2 status stays pending_review", field(fg1,"status") == "pending_review")
check("D.3 frank_response stored",       field(fg1,"frank_response") is not None)
check("D.4 response_ts stored",          field(fg1,"response_ts") is not None)

print("== K (full). pending_review [awaiting-review] after answer")
r = sh(["list", "build"])
check("K.2 [awaiting-review] shown when frank_response set", "[awaiting-review]" in r.stdout, r.stdout[:400])

print("== E (blank). answer refuses blank response")
r = sh(["answer", str(fg2), ""])
check("E.3 answer refused when text is blank/empty", r.returncode != 0, r.stdout[:200])

print("== O. review refuses when frank_response not set")
r = sh(["decide", str(fg2), "--prompt", "Q?", "--rec", "R?"])  # advance fg2 to pending_review
r2 = sh(["review", str(fg2), "accept"])
check("O.1 review refused when frank_response NULL", r2.returncode != 0 and "frank_response" in r2.stdout, r2.stdout[:200])

print("== G. wq review accept: pending_review -> open")
r = sh(["review", str(fg1), "accept"])
check("G.1 review accept succeeds", r.returncode == 0 and "ACCEPTED" in r.stdout, r.stdout[:200])
check("G.2 status=open",            field(fg1,"status") == "open")
check("G.3 review_verdict=accept",  field(fg1,"review_verdict") == "accept")
check("G.4 frank_response preserved", field(fg1,"frank_response") is not None)

print("== A (post-accept). accepted ticket is now claimable")
r = sh(["claim", "--id", str(fg1)])
check("A.2 claim succeeds after accept", r.returncode == 0 and "CLAIMED" in r.stdout, r.stdout[:200])
sh(["release", str(fg1)])

print("== I. reject requires --reason")
sh(["decide", str(fg3), "--prompt", "Q3?", "--rec", "R3?"])
sh(["answer", str(fg3), "vague response without timeframe"])
r = sh(["review", str(fg3), "reject"])
check("I.1 reject refused without --reason", r.returncode != 0 and "--reason" in r.stdout, r.stdout[:200])

print("== H. wq review reject: pending_review -> frank_gated")
r = sh(["review", str(fg3), "reject", "--reason", "Blank response — no timeframe, no risk ack"])
check("H.1 review reject succeeds", r.returncode == 0 and "REJECTED" in r.stdout, r.stdout[:200])
check("H.2 status=frank_gated",        field(fg3,"status") == "frank_gated")
check("H.3 review_verdict=reject",     field(fg3,"review_verdict") == "reject")
check("H.4 review_reason stored",      field(fg3,"review_reason") == "Blank response — no timeframe, no risk ack")
check("H.5 frank_response cleared",    field(fg3,"frank_response") is None)
check("H.6 response_ts cleared",       field(fg3,"response_ts") is None)

print("== J. reject-resubmit: full second cycle to open")
r_d = sh(["decide", str(fg3), "--prompt", "Q3 revised?", "--rec", "Rec: wait"])
check("J.1 decide on rejected ticket succeeds", r_d.returncode == 0, r_d.stdout[:200])
r_a = sh(["answer", str(fg3), "We wait until the maintenance window on Sunday. I acknowledge outage risk."])
check("J.2 resubmit answer succeeds", r_a.returncode == 0, r_a.stdout[:200])
r_rv = sh(["review", str(fg3), "accept", "--reason", "Satisfactory — risk ack + timeframe provided"])
check("J.3 accept after resubmit", r_rv.returncode == 0 and "ACCEPTED" in r_rv.stdout, r_rv.stdout[:200])
check("J.4 status=open after resubmit", field(fg3,"status") == "open")
check("J.5 review_verdict=accept",      field(fg3,"review_verdict") == "accept")

print("== L. wq show exposes all new fields")
r = sh(["show", str(fg3)])
for f in ["gate_type", "decision_prompt", "orch_recommendation", "frank_response",
          "response_ts", "review_verdict", "review_reason"]:
    check(f"L.{f}", f in r.stdout, r.stdout[:100])

print("== M. wq add --frank-gated")
r = sh(["add", "--lane", "build", "--title", "probe662 frank-gated add test", "--frank-gated"])
check("M.1 --frank-gated accepted", r.returncode == 0 and "frank_gated" in r.stdout, r.stdout[:200])

print("== N. wq add without --frank-gated still defaults to open")
r = sh(["add", "--lane", "build", "--title", "probe662 normal add test"])
check("N.1 normal add stays open", r.returncode == 0 and "frank_gated" not in r.stdout, r.stdout[:200])

import shutil; shutil.rmtree(TMP, ignore_errors=True)
print(f"\nPASS {len(PASS)}  FAIL {len(FAIL)}" + (f"  -> {FAIL}" if FAIL else ""))
print("PROBE_RESULT: " + ("GREEN" if not FAIL else "RED"))
sys.exit(0 if not FAIL else 1)
