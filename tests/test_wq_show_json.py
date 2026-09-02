"""`wq show --json` — wq#1277.

`wq show` used to print `f"{k:13}: {v}"` per column with no escaping. A `detail` field
containing a literal newline (exactly the shape `aq_escalate.py`'s seed_ticket() writes:
"Probe: X\naq row: #N ...") got truncated at the first newline by every line-oriented
caller (harness/fworker.py, orchestrator/router.py in claude-foreman), and a later line
containing a colon got mis-parsed as a bogus top-level key.

`--json` emits the row as one JSON object so line-oriented parsing is no longer needed.
Same subprocess-against-a-scratch-DB pattern as test_wq_reopened.py.
"""
import json
import os
import subprocess

WQ_BIN = os.environ.get("WQ_BIN", "/home/lumen/bin/wq")

MULTILINE_DETAIL = "Probe: X\naq row: #5 (something: value)"


def make_env(tmp_path):
    env = dict(os.environ)
    env["WQ_DB"] = str(tmp_path / "scratch_wq.db")
    env["WQ_AGENT"] = "test-show-json-agent"
    return env


def wq(env, *args):
    return subprocess.run([WQ_BIN] + list(args), capture_output=True, text=True, env=env)


def wq_add(env, title, detail, lane="auto"):
    p = wq(env, "add", "--lane", lane, "--title", title, "--detail", detail)
    assert p.returncode == 0, p.stdout + p.stderr
    return int(p.stdout.strip().split()[2])


def test_json_preserves_embedded_newline(tmp_path):
    env = make_env(tmp_path)
    tid = wq_add(env, "multiline detail ticket", MULTILINE_DETAIL)

    p = wq(env, "show", str(tid), "--json")
    assert p.returncode == 0, p.stdout + p.stderr
    row = json.loads(p.stdout)

    assert row["id"] == tid
    assert row["detail"] == MULTILINE_DETAIL
    assert "effective_pri" in row


def test_plain_text_escapes_embedded_newline(tmp_path):
    env = make_env(tmp_path)
    tid = wq_add(env, "multiline detail ticket plain", MULTILINE_DETAIL)

    p = wq(env, "show", str(tid))
    assert p.returncode == 0, p.stdout + p.stderr
    lines = p.stdout.splitlines()

    # The detail value must collapse onto a single line (escaped), so a naive
    # line-by-line `key : value` parser can no longer be corrupted by it.
    detail_lines = [l for l in lines if l.startswith("detail")]
    assert len(detail_lines) == 1
    assert detail_lines[0] == "detail       : Probe: X\\naq row: #5 (something: value)"

    # And no line derived from the embedded "aq row: #5 (...)" text should be mistakable
    # for a real top-level key — it must not appear as its own "key : value" line.
    assert not any(l.startswith("aq row") for l in lines)


def test_json_no_such_task(tmp_path):
    env = make_env(tmp_path)
    p = wq(env, "show", "999", "--json")
    assert p.returncode == 0, p.stdout + p.stderr
    assert json.loads(p.stdout) == {"error": "no such task"}
