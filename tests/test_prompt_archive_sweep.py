"""prompt-archive-sweep (wq#5082): done ticket moves, open stays, template skipped, unticketed
never moved, missing ticket kept, name collision in _archive, dry-run moves nothing, bad db
moves nothing. Synthetic fixtures only — never the live queue.db or prompts dir."""
import os
import sqlite3
import subprocess
import sys

import pytest

BIN = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "prompt-archive-sweep")


def make_db(path, rows):
    c = sqlite3.connect(path)
    c.execute("CREATE TABLE tasks(id INTEGER PRIMARY KEY, status TEXT, title TEXT)")
    c.executemany("INSERT INTO tasks(id,status,title) VALUES(?,?,?)", [(i, s, f"t{i}") for i, s in rows])
    c.commit()
    c.close()


@pytest.fixture
def env(tmp_path):
    prompts = tmp_path / "prompts"
    prompts.mkdir()
    db = tmp_path / "queue.db"
    log = tmp_path / "sweep.log"
    make_db(db, [(1, "done"), (2, "open"), (3, "claimed"), (4, "blocked"), (5, "done"), (6, "frank_gated")])
    files = {
        "wq1-done-thing.md": "MODEL: x\nTICKET: wq#1\n",
        "wq2-open-thing.md": "o",
        "wq3-claimed-thing.md": "c",
        "wq4-blocked.md": "b",
        "wq6-gated.md": "g",
        "wq99-not-in-db.md": "m",
        "orchestrator.md": "template",
        "limpet-build.md": "template",
        "wq5-task-template.md": "template even though wq5 is done",
        "ptah-clone-to-nonpro.md": "unticketed",
        "wq1-notes.txt": "not md",
    }
    for n, body in files.items():
        (prompts / n).write_text(body)
    return prompts, db, log


def run(prompts, db, log, *extra):
    e = dict(os.environ, PROMPT_SWEEP_LOCK=str(prompts.parent / "lock"))
    return subprocess.run([sys.executable, BIN, "--prompts", str(prompts), "--db", str(db), "--log", str(log), *extra],
                          capture_output=True, text=True, env=e, timeout=30)


def test_done_moves_open_stays_template_skipped(env):
    prompts, db, log = env
    r = run(prompts, db, log)
    assert r.returncode == 0, r.stderr
    arch = prompts / "_archive"
    assert (arch / "wq1-done-thing.md").read_text().startswith("MODEL: x")
    assert not (prompts / "wq1-done-thing.md").exists()
    for keep in ("wq2-open-thing.md", "wq3-claimed-thing.md", "wq4-blocked.md", "wq6-gated.md",
                 "wq99-not-in-db.md", "orchestrator.md", "limpet-build.md", "wq5-task-template.md",
                 "ptah-clone-to-nonpro.md", "wq1-notes.txt"):
        assert (prompts / keep).exists(), keep
    assert sorted(os.listdir(arch)) == ["wq1-done-thing.md"]
    assert "archived wq1-done-thing.md" in log.read_text()


def test_dry_run_moves_nothing(env):
    prompts, db, log = env
    r = run(prompts, db, log, "--dry-run")
    assert r.returncode == 0
    assert "would-archive    wq1-done-thing.md" in r.stdout
    assert "skip-template    wq5-task-template.md" in r.stdout
    assert "skip-unticketed  ptah-clone-to-nonpro.md" in r.stdout
    assert (prompts / "wq1-done-thing.md").exists()
    assert not (prompts / "_archive").exists()
    assert not log.exists()


def test_idempotent_and_collision(env):
    prompts, db, log = env
    assert run(prompts, db, log).returncode == 0
    (prompts / "wq1-done-thing.md").write_text("second copy")
    assert run(prompts, db, log).returncode == 0
    names = sorted(os.listdir(prompts / "_archive"))
    assert len(names) == 2 and "wq1-done-thing.md" in names
    stamped = [n for n in names if n != "wq1-done-thing.md"][0]
    assert stamped.startswith("wq1-done-thing.") and stamped.endswith("Z.md")
    assert (prompts / "_archive" / stamped).read_text() == "second copy"
    assert run(prompts, db, log).returncode == 0     # third run: nothing to do
    assert len(os.listdir(prompts / "_archive")) == 2


def test_status_change_is_picked_up(env):
    prompts, db, log = env
    c = sqlite3.connect(db)
    c.execute("UPDATE tasks SET status='done' WHERE id=2")
    c.commit()
    c.close()
    assert run(prompts, db, log).returncode == 0
    assert (prompts / "_archive" / "wq2-open-thing.md").exists()
    assert (prompts / "wq3-claimed-thing.md").exists()


def test_bad_db_moves_nothing(env, tmp_path):
    prompts, _db, log = env
    bad = tmp_path / "nope.db"
    bad.write_text("not sqlite")
    r = run(prompts, bad, log)
    assert r.returncode == 1
    assert (prompts / "wq1-done-thing.md").exists()
    assert not (prompts / "_archive").exists()


def test_missing_prompts_dir(tmp_path):
    db = tmp_path / "q.db"
    make_db(db, [])
    r = run(tmp_path / "absent", db, tmp_path / "l")
    assert r.returncode == 2


def test_symlink_never_moved(env):
    prompts, db, log = env
    target = prompts.parent / "outside.md"
    target.write_text("x")
    os.symlink(target, prompts / "wq5-link.md")
    assert run(prompts, db, log).returncode == 0
    assert os.path.islink(prompts / "wq5-link.md")
