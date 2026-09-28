"""wq#3068 -- a '~' gate clause is refused at write time, because gate_met() never expands it.

wq#3066 adversarially verified wq#3054 and CONTRADICTED it. wq#3054's stated guarantee is that
the guard (bad_gate_clauses) and the reader (gate_met) share one parse and "can never disagree"
about a clause. They disagreed for '~': the guard accepted `~/x:exists` as a legal ENFORCED
clause (head.startswith('~'), inherited unchanged from wq#3033), while gate_met() calls
os.path.exists() on the literal head -- so the ticket was accepted and then refused forever at
claim time with "gate: ~/x missing", even when the expanded file exists. That is wq#3033's own
"claim refused forever" class, reachable through a form print_bad_gate() advertised in its own
refusal text.

Judgment (wq#3068): drop '~' from the legal enforced heads rather than teach the reader to
expand it. A stored gate is read back by whatever process calls claim; expanding in the reader
makes guard and reader agree only while the writer's HOME and the claimant's HOME match, which
is the same environment-dependence the guard already refuses relative paths for. An absolute
path names one file in every process. Absolute paths, label:value markers and --force are all
unchanged.

Falsifiability (wq#2630): the refuse-cases here FAIL against the pre-fix binary
(`WQ_BIN=<pre-fix wq> python3 -m pytest tests/test_wq3068_gate_tilde_refused.py`), which
accepts every one of them. The probe input is pinned to HOME itself -- a path that exists by
definition and cannot be edited out from under the test -- not to a live mutable file.
Same zero-quota subprocess pattern as test_wq3054_gate_label_markers.py.
"""
import os
import subprocess

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
WQ_BIN = os.environ.get("WQ_BIN", os.path.join(REPO, "wq"))

# Pinned probe input: the home directory. '~' and its expansion denote the same directory by
# definition, on any account this ever runs as, so the pair isolates exactly one variable --
# whether the stored head is expanded -- with no file to create and none to rot.
TILDE = "~"
ABSOLUTE = os.path.expanduser("~")


def make_env(tmp_path):
    env = dict(os.environ)
    env["WQ_DB"] = str(tmp_path / "scratch_wq.db")
    env["WQ_AGENT"] = "test-gate-tilde"
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


def add_id(env, title, gate=None, force=False):
    p = add(env, title, gate=gate, force=force)
    assert p.returncode == 0, p.stdout + p.stderr
    return int(p.stdout.strip().split()[2])


# --- the premise: the reader really cannot satisfy a '~' clause --------------------------

def test_a_forced_tilde_gate_strands_the_ticket_even_though_the_path_exists(tmp_path):
    """Why the write-time refusal is worth having. --force past the guard and the ticket is
    unclaimable forever, while the very same directory named in full claims fine."""
    assert os.path.isdir(ABSOLUTE), "HOME must exist for this pair to isolate expansion"
    env = make_env(tmp_path)
    tid = add_id(env, "forced tilde gate", gate=f"{TILDE}:exists", force=True)
    p = wq(env, "claim", "--id", str(tid))
    assert p.returncode != 0
    assert f"gate: {TILDE} missing" in (p.stdout + p.stderr)


def test_the_same_path_written_in_full_is_claimable(tmp_path):
    """Control for the case above: absolute is not merely accepted, it is satisfiable."""
    env = make_env(tmp_path)
    tid = add_id(env, "absolute home gate", gate=f"{ABSOLUTE}:exists")
    p = wq_ok(env, "claim", "--id", str(tid))
    assert "gate:" not in (p.stdout + p.stderr)


# --- the fix: the guard refuses what the reader cannot satisfy ---------------------------

def test_add_refuses_a_tilde_enforced_clause(tmp_path):
    env = make_env(tmp_path)
    p = add(env, "tilde gate", gate=f"{TILDE}/.bashrc:exists")
    assert p.returncode != 0
    assert "not an absolute path" in p.stdout


def test_add_refuses_a_bare_tilde_clause(tmp_path):
    """No colon => gate_met() supplies cond='exists' => enforced, and still unexpanded."""
    env = make_env(tmp_path)
    p = add(env, "bare tilde gate", gate=TILDE)
    assert p.returncode != 0
    assert "not an absolute path" in p.stdout


def test_add_refuses_a_tilde_missing_clause(tmp_path):
    """The 'missing' branch inverts the check, so an unexpanded '~' clause is vacuously MET --
    a gate that silently enforces nothing, which is the other half of the same defect."""
    env = make_env(tmp_path)
    p = add(env, "tilde missing gate", gate=f"{TILDE}/.bashrc:missing")
    assert p.returncode != 0
    assert "not an absolute path" in p.stdout


def test_add_refuses_a_tilde_clause_mixed_with_a_good_one(tmp_path):
    """One bad clause in a comma list poisons the whole gate; the refusal names it."""
    env = make_env(tmp_path)
    p = add(env, "mixed gate", gate=f"{ABSOLUTE}:exists,{TILDE}/.bashrc:exists")
    assert p.returncode != 0
    assert f"{TILDE}/.bashrc:exists" in p.stdout


def test_edit_refuses_a_tilde_enforced_clause(tmp_path):
    """Both write paths, not just add (wq#3054 unified them; the narrowing must reach both)."""
    env = make_env(tmp_path)
    tid = add_id(env, "edit target")
    p = wq(env, "edit", str(tid), "--gate", f"{TILDE}/.bashrc:exists")
    assert p.returncode != 0
    assert "not an absolute path" in p.stdout


def test_the_refusal_no_longer_teaches_the_tilde_form(tmp_path):
    """The trap was reachable because print_bad_gate() named '~' as a legal form. An agent
    reading the refusal must not be sent straight back into it."""
    env = make_env(tmp_path)
    p = add(env, "prose gate", gate="netdata bundle confirmed live on both Mac Pros")
    assert p.returncode != 0
    assert "absolute or ~" not in p.stdout
    assert "/abs/path, not ~/path" in p.stdout


# --- the narrowing must not take anything else with it -----------------------------------

def test_force_still_overrides_the_tilde_refusal(tmp_path):
    env = make_env(tmp_path)
    p = add(env, "forced tilde gate", gate=f"{TILDE}/.bashrc:exists", force=True)
    assert p.returncode == 0, p.stdout + p.stderr


def test_absolute_paths_and_markers_are_untouched(tmp_path):
    env = make_env(tmp_path)
    assert add(env, "absolute", gate="/home/lumen/kb/ISSUE-LOG.md").returncode == 0
    assert add(env, "marker",
               gate="deploy_pending:reports/319_staged-review-ui.md").returncode == 0
    assert add(env, "multi",
               gate="/home/lumen/kb/ISSUE-LOG.md,/tmp/nope:missing").returncode == 0


def test_a_path_that_merely_contains_a_tilde_is_still_fine(tmp_path):
    """Only a leading '~' is the shell-expansion form; '~' elsewhere is an ordinary filename
    character and an absolute path carrying one must keep working (editor backup files)."""
    env = make_env(tmp_path)
    p = add(env, "tilde inside", gate=f"{ABSOLUTE}/notes.md{TILDE}:exists")
    assert p.returncode == 0, p.stdout + p.stderr
