"""wq#1331 — tasks.needs column (migration + --needs + note append).

Same subprocess-against-a-scratch-DB pattern as test_wq_show_json.py. The column is
owned by migrations/wq1331_needs.py (wq#1158 contract): wq feature-detects it, and
`--needs` is REFUSED pre-migration rather than silently dropped.
"""
import json
import os
import subprocess
import sys

BIN_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
WQ_BIN = os.environ.get("WQ_BIN", os.path.join(BIN_DIR, "wq"))
MIGRATION = os.path.join(BIN_DIR, "migrations", "wq1331_needs.py")


def make_env(tmp_path):
    env = dict(os.environ)
    env["WQ_DB"] = str(tmp_path / "scratch_wq.db")
    env["WQ_AGENT"] = "test-needs-agent"
    return env


def wq(env, *args):
    return subprocess.run([WQ_BIN] + list(args), capture_output=True, text=True, env=env)


def migrate(env, *extra):
    return subprocess.run([sys.executable, MIGRATION, "--db", env["WQ_DB"], *extra],
                          capture_output=True, text=True)


def wq_add(env, title, *extra):
    p = wq(env, "add", "--title", title, *extra)
    return p, (int(p.stdout.strip().split()[2]) if p.returncode == 0 else None)


def show(env, tid):
    return json.loads(wq(env, "show", str(tid), "--json").stdout)


def test_needs_refused_before_migration(tmp_path):
    env = make_env(tmp_path)
    wq(env, "list")  # init the DB
    p, _ = wq_add(env, "pre-migration ticket", "--needs", "repo(/home/lumen/questfree)")
    assert p.returncode == 1 and "wq1331" in p.stdout
    p2, tid = wq_add(env, "plain ticket still fine")
    assert p2.returncode == 0
    p3 = wq(env, "edit", str(tid), "--needs", "dir(/home/lumen/kb)")
    assert p3.returncode == 1 and "wq1331" in p3.stdout


def test_migration_report_apply_idempotent_rollback(tmp_path):
    env = make_env(tmp_path)
    wq(env, "list")
    r = migrate(env)  # bare run: report only, applies nothing
    assert r.returncode == 0 and "MISSING" in r.stdout and "report only" in r.stdout
    a = migrate(env, "--yes")
    assert a.returncode == 0 and "APPLIED." in a.stdout and "backup:" in a.stdout
    again = migrate(env, "--yes")
    assert again.returncode == 0 and "ALREADY APPLIED" in again.stdout
    rb = migrate(env, "--rollback", "--yes")
    assert rb.returncode == 0 and "ROLLED BACK." in rb.stdout
    r2 = migrate(env)
    assert "MISSING" in r2.stdout


def test_needs_roundtrip_add_edit_clear_after_migration(tmp_path):
    env = make_env(tmp_path)
    wq(env, "list")
    assert migrate(env, "--yes").returncode == 0
    p, tid = wq_add(env, "needs ticket", "--needs",
                    "repo(/home/lumen/questfree), dir(/home/lumen/kb)")
    assert p.returncode == 0, p.stdout + p.stderr
    assert show(env, tid)["needs"] == "repo(/home/lumen/questfree), dir(/home/lumen/kb)"

    e = wq(env, "edit", str(tid), "--needs", "dir(/home/lumen/nle-flasher)")
    assert e.returncode == 0 and "needs" in e.stdout
    assert show(env, tid)["needs"] == "dir(/home/lumen/nle-flasher)"

    cl = wq(env, "edit", str(tid), "--needs", "")
    assert cl.returncode == 0
    assert show(env, tid)["needs"] == ""


def test_needs_vocabulary_enforced(tmp_path):
    env = make_env(tmp_path)
    wq(env, "list")
    assert migrate(env, "--yes").returncode == 0
    for bad, frag in (("frobnicate(isis)", "unknown needs tag"),
                      ("repo(relative)", "absolute path"),
                      ("dir()", "empty argument"),
                      ("prose no tags", "unparseable")):
        p, _ = wq_add(env, f"bad needs {bad}", "--needs", bad)
        assert p.returncode == 1 and frag in p.stdout, (bad, p.stdout)
    ok, tid = wq_add(env, "full vocabulary", "--needs",
                     "ssh(isis), restart(ollama@isis), bench(thor,adb)")
    assert ok.returncode == 0


def test_note_appends_and_never_overwrites(tmp_path):
    env = make_env(tmp_path)
    wq(env, "list")
    _, tid = wq_add(env, "note target")
    n1 = wq(env, "note", str(tid), "first line")
    assert n1.returncode == 0, n1.stdout + n1.stderr
    n2 = wq(env, "note", str(tid), "second line")
    assert n2.returncode == 0
    notes = show(env, tid)["notes"]
    assert "first line" in notes and "second line" in notes
    assert notes.index("first line") < notes.index("second line")
    bad = wq(env, "note", str(tid), "   ")
    assert bad.returncode == 1
    missing = wq(env, "note", "99999", "x")
    assert missing.returncode == 1
