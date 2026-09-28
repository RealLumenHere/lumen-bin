"""wq#3054 -- `--gate` accepts the documented 'label:value' marker, still refuses prose.

wq#3047 verified wq#3033's prose-gate guard and CONTRADICTED it: bad_gate_clauses() re-derived
"looks like a path" (leading '/' or '~') independently of gate_met(), so it refused the older,
documented use of the same column as a status label --
`wq edit <id> --gate "deploy_pending:<report>"` (reports/staged_review_ui_design.md:30, live on
wq#319/401/402/405/407). Judgment (wq#3054): mirror gate_met()'s own parse. A clause whose tail
after the last ':' is neither 'exists' nor 'missing' is never tested by gate_met(), so it is
vacuously met and cannot strand a ticket -- only an ENFORCED clause needs a real absolute path
(wq#3068 dropped '~' from the legal enforced heads; see test_wq3068_gate_tilde_refused.py).
Prose stays refused on both branches: bare prose is enforced-and-unsatisfiable, and prose with a
colon in it leaves whitespace in the label, which a marker label (one token) never has.

Falsifiability: every accept-case here FAILS against the pre-wq#3054 binary (WQ_BIN=<old wq>),
and the refuse-cases cover the boundary the narrowing could have opened (colon-bearing prose,
relative paths, bare labels) rather than only the population wq#3033 was built for (wq#2630).
Same zero-quota subprocess pattern as test_wq3033_gate_path_guard.py.
"""
import os
import subprocess

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
WQ_BIN = os.environ.get("WQ_BIN", os.path.join(REPO, "wq"))

# Pinned fixture, not a live read: the convention string verbatim from the design doc that
# defines it -- reports/staged_review_ui_design.md:30 in /home/lumen/agents/work-queue, quoted
# in reports/319_staged-review-ui.md:43-44, and the shape of the 5 live deploy_* gate rows.
DOCUMENTED_MARKER = "deploy_pending:reports/319_staged-review-ui.md"
# The UI's approve/reject/defer rewrites of the same marker (staged_review_ui_design.md:32-34).
UI_REWRITES = ["deploy_approved:reports/319_staged-review-ui.md",
               "deploy_rejected:reports/319_staged-review-ui.md",
               "deploy_deferred:reports/319_staged-review-ui.md"]


def make_env(tmp_path):
    env = dict(os.environ)
    env["WQ_DB"] = str(tmp_path / "scratch_wq.db")
    env["WQ_AGENT"] = "test-gate-label-markers"
    # wq#912: fail the lane guard open, same as the other CLI tests in this dir.
    env["WQ_DISPATCHER_CONFIG"] = str(tmp_path / "no-such-dispatcher-config.json")
    return env


def wq(env, *args):
    return subprocess.run([WQ_BIN] + [str(x) for x in args],
                          capture_output=True, text=True, env=env)


def wq_ok(env, *args):
    p = wq(env, *args)
    assert p.returncode == 0, f"{args} -> {p.returncode}\n{p.stdout}\n{p.stderr}"
    return p


def add(env, title, gate=None, force=False):
    args = ["add", "--lane", "auto", "--pri", "1", "--title", title]
    if gate is not None:
        args += ["--gate", gate]
    if force:
        args += ["--force"]
    return wq(env, *args)


def add_id(env, title):
    return int(wq_ok(env, "add", "--lane", "auto", "--pri", "1",
                     "--title", title).stdout.strip().split()[2])


def gate_of(env, tid):
    p = wq_ok(env, "show", str(tid))
    for line in p.stdout.splitlines():
        if line.startswith("gate "):
            return line.split(":", 1)[1].strip()
    return None


# --- the break wq#3047 found: the documented convention must work again ------------------

def test_add_accepts_the_documented_deploy_pending_marker(tmp_path):
    env = make_env(tmp_path)
    p = add(env, "staged review target", gate=DOCUMENTED_MARKER)
    assert p.returncode == 0, p.stdout + p.stderr


def test_edit_accepts_the_documented_deploy_pending_marker(tmp_path):
    """The exact invocation staged_review_ui_design.md:30 tells a BUILD agent to run on close."""
    env = make_env(tmp_path)
    tid = add_id(env, "staged review target")
    p = wq(env, "edit", str(tid), "--gate", DOCUMENTED_MARKER)
    assert p.returncode == 0, p.stdout + p.stderr
    assert gate_of(env, tid) == DOCUMENTED_MARKER


def test_edit_accepts_every_ui_rewrite_of_the_marker(tmp_path):
    env = make_env(tmp_path)
    tid = add_id(env, "staged review target")
    for marker in UI_REWRITES:
        p = wq(env, "edit", str(tid), "--gate", marker)
        assert p.returncode == 0, f"{marker}: {p.stdout}{p.stderr}"


def test_add_accepts_a_frank_hold_label_in_marker_form(tmp_path):
    """The frank-bench-time/frank-after-reddit holds, written as markers."""
    env = make_env(tmp_path)
    for label in ("frank-bench-time:hold", "frank-after-reddit:hold"):
        p = add(env, f"hw ticket {label}", gate=label)
        assert p.returncode == 0, p.stdout + p.stderr


def test_add_accepts_a_path_and_a_marker_together(tmp_path):
    env = make_env(tmp_path)
    p = add(env, "mixed clauses", gate=f"/home/lumen/kb/ISSUE-LOG.md,{DOCUMENTED_MARKER}")
    assert p.returncode == 0, p.stdout + p.stderr


# --- a marker is vacuous in the reader, so it cannot strand the ticket -------------------

def test_a_marker_gate_leaves_the_ticket_claimable(tmp_path):
    """The whole justification for allowing markers: gate_met() never tests a non
    exists/missing cond, so wq#3033's 'claim refused forever' class does not apply."""
    env = make_env(tmp_path)
    tid = int(wq_ok(env, "add", "--lane", "auto", "--pri", "1", "--title", "claimable with marker",
                    "--gate", DOCUMENTED_MARKER).stdout.strip().split()[2])
    p = wq(env, "claim", "--id", str(tid))
    assert "gate:" not in (p.stdout + p.stderr), p.stdout + p.stderr


def test_a_forced_bare_label_gate_does_strand_the_ticket(tmp_path):
    """Control for the test above, and the reason a BARE label stays refused: written without
    a ':value' tail it is an enforced check for a file named 'frank-bench-time'."""
    env = make_env(tmp_path)
    tid = int(wq_ok(env, "add", "--lane", "auto", "--pri", "1", "--title", "stranded by bare label",
                    "--gate", "frank-bench-time", "--force").stdout.strip().split()[2])
    p = wq(env, "claim", "--id", str(tid))
    assert p.returncode != 0
    assert "gate: frank-bench-time missing" in (p.stdout + p.stderr)


# --- the narrowing must not open a hole: prose, relative paths, bare labels stay refused --

def test_add_still_refuses_bare_prose(tmp_path):
    """wq#3033's own case: no colon => enforced as a path => unsatisfiable."""
    env = make_env(tmp_path)
    p = add(env, "wq2844 repro", gate="netdata bundle confirmed live on both Mac Pros")
    assert p.returncode != 0
    assert "not an absolute path" in p.stdout


def test_add_still_refuses_prose_that_contains_a_colon(tmp_path):
    """The boundary a colon-only mirror of gate_met() would have opened: the tail is neither
    'exists' nor 'missing', so the clause parses as a marker -- but the label carries spaces,
    which a real label never does."""
    env = make_env(tmp_path)
    for prose in ("verify: netdata bundle live on both Mac Pros",
                  "done when: Frank signs off"):
        p = add(env, f"colon prose {prose[:12]}", gate=prose)
        assert p.returncode != 0, f"{prose!r} was accepted: {p.stdout}"
        assert "label:value marker" in p.stdout


def test_add_still_refuses_a_relative_path_gate(tmp_path):
    """Enforced (no cond, or an explicit one) and cwd-dependent: gate_met() resolves it against
    whatever directory the caller happens to be in, so it can never pass reliably."""
    env = make_env(tmp_path)
    for rel in ("reports/319_staged-review-ui.md", "reports/319_staged-review-ui.md:exists",
                "reports/319_staged-review-ui.md:missing"):
        p = add(env, f"relative gate {rel[-8:]}", gate=rel)
        assert p.returncode != 0, f"{rel!r} was accepted: {p.stdout}"


def test_add_still_refuses_a_bare_label_without_a_value(tmp_path):
    env = make_env(tmp_path)
    p = add(env, "bare frank hold", gate="frank-bench-time")
    assert p.returncode != 0
    assert "frank-bench-time:hold" in p.stdout  # the refusal teaches the marker form


def test_refusal_names_only_the_bad_clause_in_a_mixed_gate(tmp_path):
    env = make_env(tmp_path)
    p = add(env, "marker plus prose", gate=f"{DOCUMENTED_MARKER},acceptance prose")
    assert p.returncode != 0
    assert "acceptance prose" in p.stdout
    assert DOCUMENTED_MARKER not in p.stdout.split("(wq#3033")[0]


def test_force_still_overrides_the_refusal(tmp_path):
    env = make_env(tmp_path)
    p = add(env, "forced through anyway", gate="still just prose", force=True)
    assert p.returncode == 0, p.stdout + p.stderr
