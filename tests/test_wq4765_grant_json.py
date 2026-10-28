"""wq#4765: `wq grant list/show --json` — fworker's two new reads of grant_requests.

At defer time fworker cross-checks the result frame's `deferred_tool_use.id` against
the row hold_gate.py filed, keyed by (ticket, session, state=pending) — it has no row
id yet, only the ticket+session the CLI gave it, so it needs `list --json` to find the
row and read its tool_use_id as a real field (not the truncated-fingerprint text line
_grant_line prints, which carries no tool_use_id at all). At resume time the ticket row
already names the exact grant_req id, so `show --json` is the direct read of that row's
class/target for the H1 --add-dir/--bind-ro materialization. Same WQ_DB-scratch
subprocess pattern as test_wq4761_grant_requests.py; reuses its fixtures rather than
re-deriving them.
"""
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import test_wq4761_grant_requests as base  # noqa: E402


def test_list_json_is_empty_array_with_no_rows(tmp_path):
    env = base.make_env(tmp_path)
    p = base.wq(env, "grant", "list", "--json")
    assert p.returncode == 0, p.stdout + p.stderr
    assert json.loads(p.stdout) == []


def test_list_json_carries_tool_use_id_and_target_as_real_fields(tmp_path):
    env = base.make_env(tmp_path)
    tid = base.add(env)
    we = base.with_agent(env, f"fworker-wq{tid}")
    p = base.wq(we, "claim", "--id", tid)
    assert p.returncode == 0, p.stdout + p.stderr
    p = base.wq(we, "grant", "request", tid, "--fingerprint", base.fp("a1"),
                "--session", "sess-a1", "--tool-use", "toolu_01abc",
                "--class", "H1", "--target", "/srv/granted/dir")
    assert p.returncode == 0, p.stdout + p.stderr
    rid = int(p.stdout.split("id=")[1].split()[0])

    p = base.wq(env, "grant", "list", "--ticket", tid, "--session", "sess-a1",
                "--state", "pending", "--json")
    assert p.returncode == 0, p.stdout + p.stderr
    rows = json.loads(p.stdout)
    assert len(rows) == 1
    row = rows[0]
    assert row["id"] == rid
    assert row["ticket_id"] == tid
    assert row["tool_use_id"] == "toolu_01abc"
    assert row["class"] == "H1"
    assert row["target"] == "/srv/granted/dir"
    assert row["state"] == "pending"


def test_list_json_session_filter_excludes_other_sessions(tmp_path):
    env = base.make_env(tmp_path)
    tid = base.add(env)
    we = base.with_agent(env, f"fworker-wq{tid}")
    base.wq(we, "claim", "--id", tid)
    base.wq(we, "grant", "request", tid, "--fingerprint", base.fp("s1"),
            "--session", "sess-1", "--tool-use", "toolu_1")
    p = base.wq(env, "grant", "list", "--ticket", tid, "--session", "sess-2", "--json")
    assert json.loads(p.stdout) == []


def test_show_json_matches_list_json_row_shape(tmp_path):
    env = base.make_env(tmp_path)
    tid = base.add(env)
    rid = base.file_request(env, tid, "show-json", session="sess-show")
    p_show = base.wq(env, "grant", "show", rid, "--json")
    p_list = base.wq(env, "grant", "list", "--ticket", tid, "--json")
    assert p_show.returncode == 0, p_show.stdout + p_show.stderr
    show_rows = json.loads(p_show.stdout)
    list_rows = json.loads(p_list.stdout)
    assert len(show_rows) == 1
    assert show_rows[0] == list_rows[0]


def test_json_flag_leaves_plain_output_unchanged(tmp_path):
    env = base.make_env(tmp_path)
    tid = base.add(env)
    base.file_request(env, tid, "plain")
    p = base.wq(env, "grant", "list")
    assert p.returncode == 0
    assert "GRANT_REQ id=" in p.stdout and "grant request(s)" in p.stdout
    # plain mode never emits a bare JSON array.
    assert not p.stdout.strip().startswith("[")
