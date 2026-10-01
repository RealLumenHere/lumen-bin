"""dc-handles-scrub: redaction, idempotence, --check, and (the point) RAW-byte cleanliness after scrub."""
import hashlib
import importlib.machinery
import importlib.util
import os
import sqlite3
import subprocess
import sys

BIN = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "dc-handles-scrub")
CF = "cf" + "at_" + "A1b2C3d4" * 5 + "abcd1234"         # fake, built at runtime
GH = "github_pat_" + "Q9" * 25
GHP = "gh" + "p_" + "z" * 36
ANT = "sk-" + "ant-" + "api03-" + "x" * 30


def mk(tmp_path, rows):
    db = str(tmp_path / "h.db")
    con = sqlite3.connect(db)
    con.execute("PRAGMA journal_mode=WAL")
    con.execute("create table proc(id integer primary key autoincrement, pid integer not null, host text not null, "
                "cmd text, started_at text not null, output text not null default '', exit_code integer, done_at text, out_file text)")
    for cmd, out in rows:
        con.execute("insert into proc(pid,host,cmd,started_at,output) values(1,'lumen',?,'t',?)", (cmd, out))
    con.commit()
    con.close()
    return db


def run(*args):
    return subprocess.run([sys.executable, BIN, *args], capture_output=True, text=True, timeout=120)


def rows(db):
    con = sqlite3.connect(db)
    try:
        return con.execute("select cmd, output from proc order by id").fetchall()
    finally:
        con.close()


def blob(path):
    b = b""
    for f in (path, path + "-wal"):
        if os.path.exists(f):
            b += open(f, "rb").read()
    return b


def test_redacts_with_fingerprint_and_leaves_the_rest(tmp_path):
    db = mk(tmp_path, [("curl -H 'x: %s' https://h" % CF, "ok %s and %s and %s\nBearer %s end" % (GH, GHP, ANT, "T" * 30)),
                       ("ls", "highs_solver /home/lumen/scratch/worktrees/bin-feat-fleet-tools-batch ok")])
    r = run("--db", db)
    assert r.returncode == 0, r.stderr
    (c1, o1), (c2, o2) = rows(db)
    fp = hashlib.sha256(CF.split("_", 1)[1].encode()).hexdigest()[:12]
    assert "<redacted fp=%s>" % fp in c1 and CF not in c1 and c1.startswith("curl -H 'x: <redacted")
    for tok in (GH, GHP, ANT, "T" * 30):
        assert tok not in o1
    assert "Bearer <redacted>" in o1 and o1.startswith("ok ")
    assert c2 == "ls" and "highs_solver" in o2 and "bin-feat-fleet-tools-batch" in o2     # no false positives
    assert CF not in r.stdout + r.stderr and GH not in r.stdout


def test_idempotent(tmp_path):
    db = mk(tmp_path, [("c", "x %s" % CF)])
    run("--db", db)
    first = rows(db)
    r = run("--db", db)
    assert rows(db) == first and "rows_changed=0" in r.stdout and r.returncode == 0


def test_check_is_read_only_and_exit_codes(tmp_path):
    db = mk(tmp_path, [("c", "x %s" % CF)])
    before = rows(db)
    r = run("--check", "--db", db)
    assert r.returncode == 1 and CF not in r.stdout and "row_hits=1" in r.stdout
    assert rows(db) == before                                     # nothing written
    run("--db", db)
    assert run("--check", "--db", db).returncode == 0


def test_raw_bytes_clean_after_scrub_even_with_old_freed_pages(tmp_path):
    """The live failure: no row holds the secret any more, but freed overflow pages in the file still do."""
    pad = "x" * 6000                                                  # > page size: the token lands in overflow pages
    db = mk(tmp_path, [("c%d" % i, pad + " tok %s\n" % CF) for i in range(10)])
    con = sqlite3.connect(db)
    con.execute("PRAGMA secure_delete=OFF")                            # Commander's Node sqlite writes this way; Ubuntu's python build defaults to ON
    con.execute("PRAGMA wal_checkpoint(TRUNCATE)")                     # push the pages into the main file
    con.execute("delete from proc")                                    # plain delete: pages freed, bytes NOT zeroed
    con.commit()
    con.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    con.close()
    marker = b"cf" + b"at_" + b"A1b2C3d4"
    assert rows(db) == []                                              # no logical row holds a secret
    assert marker in blob(db)                                          # ...but the file does (precondition)
    r = run("--db", db)
    assert r.returncode == 0, (r.stdout, r.stderr)
    assert "vacuumed=True" in r.stdout
    assert marker not in blob(db)                                      # raw main file + WAL are clean
    assert sqlite3.connect(db).execute("pragma integrity_check").fetchone()[0] == "ok"


def test_plain_update_of_live_rows_is_scrubbed_in_raw_bytes_too(tmp_path):
    db = mk(tmp_path, [("c%d" % i, "pad " * 50 + CF) for i in range(10)])
    r = run("--db", db)
    assert r.returncode == 0 and b"cf" + b"at_" + b"A1b2C3d4" not in blob(db)
    assert len(rows(db)) == 10


def test_token_split_across_two_appends_is_caught_on_next_run(tmp_path):
    db = mk(tmp_path, [("c", "head " + CF[:20])])
    run("--db", db)
    con = sqlite3.connect(db)
    con.execute("update proc set output = output || ? where id=1", (CF[20:] + " tail",))
    con.commit()
    con.close()
    run("--db", db)
    assert CF not in rows(db)[0][1] and "<redacted fp=" in rows(db)[0][1]


def test_missing_db_is_error(tmp_path):
    assert run("--db", str(tmp_path / "nope.db")).returncode == 2
