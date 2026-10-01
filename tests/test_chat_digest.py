"""chat-digest: block parsing, extraction, ref selection, credential masking. Synthetic fixtures only."""
import json
import os
import subprocess
import sys

BIN = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "chat-digest")
U1 = "11111111-2222-3333-4444-555555555555"
U2 = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
FAKE_CF = "cf" + "at_" + "A1b2C3d4" * 5          # built at runtime: never a literal token in the repo
FAKE_PAT = "github_pat_" + "Q9" * 22

SUMMARY = (
    '<chat url="https://claude.ai/chat/%s" updated_at="2026-09-28T09:34:38+00:00" kind="summary" page_token="x">'
    "Title: Demo fleet session\nDemo fleet session\n&lt;chat_summary&gt;**Conversation Overview**\n\n"
    "Claude worked wq#42 and aq#7 for the demo. Claude decided to install the tool on the build node rather than "
    "the edge box because the edge box is offline. The tool was installed at `/opt/demo/1.0` and verified with "
    "`python3 /home/lumen/bin/demo --check`. Episode `0123456789ab` was committed and the ticket was closed. "
    "One step remains for Frank: approve the timer, which is gated on his review. State was written to "
    "`notes.md` and `systemctl status demo.timer` was run.&lt;/chat_summary&gt;</chat>" % U1)

CONVO = (
    "<chat url='https://claude.ai/chat/%s' updated_at='2026-09-29T20:00:00+00:00' kind='conversation'>\n"
    "Human: Please rotate the key and use the mint tool, don't touch prod.\n"
    "Assistant: Plan is to run this next.\n```bash\n$ token-mint --name demo --dry-run\n# a comment\nls -la /tmp\ncurl -H \"x-key: %s\" https://example.test/api\n```\n"
    "The run passed and the new file landed at /home/lumen/.secrets/demo.token which Claude verified. "
    "Pending: Frank must approve the cutover and rotate %s which leaked.\n</chat>" % (U2, FAKE_CF, FAKE_PAT))


def run(text, *args):
    return subprocess.run([sys.executable, BIN, *args], input=text, capture_output=True, text=True, timeout=30)


def test_summary_chat_extraction():
    r = run(SUMMARY, "--json")
    assert r.returncode == 0, r.stderr
    c = json.loads(r.stdout)["chats"][0]
    assert c["id"] == U1 and c["kind"] == "summary" and c["title"] == "Demo fleet session"
    assert c["updated_pt"].startswith("2026-09-28 02:34")  # PDT
    assert c["tickets"] == ["wq#42", "aq#7"]
    assert c["episodes"] == ["0123456789ab"]
    assert "python3 /home/lumen/bin/demo --check" in c["commands"]
    assert "systemctl status demo.timer" in c["commands"]
    assert "/opt/demo/1.0" in c["files"] and "notes.md" in c["files"]
    assert any("decided to install" in s for s in c["decisions"])
    assert any("verified" in s for s in c["outcomes"])
    assert any("remains for Frank" in s for s in c["open_items"])


def test_conversation_chat_extraction_and_masking():
    r = run(CONVO, "--json")
    assert r.returncode == 0, r.stderr
    raw = r.stdout
    assert FAKE_CF not in raw and FAKE_PAT not in raw and "<redacted>" in raw
    c = json.loads(raw)["chats"][0]
    assert c["id"] == U2 and c["kind"] == "conversation"       # single-quoted attrs accepted
    assert "token-mint --name demo --dry-run" in c["commands"]  # $ prefix stripped
    assert "ls -la /tmp" in c["commands"]
    assert not any(x.startswith("#") for x in c["commands"])
    assert "/home/lumen/.secrets/demo.token" in c["files"]
    assert any("use the mint tool" in s for s in c["decisions"])  # human directive
    assert any("Pending" in s for s in c["open_items"])


def test_ref_selects_one_chat_by_url_or_id():
    both = SUMMARY + "\n" + CONVO
    for ref in (U2, "https://claude.ai/chat/" + U2):
        r = run(both, ref, "--json")
        chats = json.loads(r.stdout)["chats"]
        assert [c["id"] for c in chats] == [U2]
    assert len(json.loads(run(both, "--json").stdout)["chats"]) == 2


def test_ref_not_in_input_exit_3():
    r = run(SUMMARY, "99999999-0000-0000-0000-000000000000")
    assert r.returncode == 3 and "not in the input" in r.stderr


def test_markdown_output_and_plain_text_fallback():
    r = run(SUMMARY)
    assert r.returncode == 0 and "## Demo fleet session" in r.stdout and "### Commands" in r.stdout
    r2 = run("Assistant: We decided to keep the old path. Then wq#9 was closed and verified.\n")
    assert r2.returncode == 0 and "wq#9" in r2.stdout


def test_empty_input_is_usage_error():
    assert run("   \n").returncode == 2


def test_long_paths_not_redacted():
    p = "/home/lumen/scratch/worktrees/bin-feat-fleet-tools-batch/some-file-name.py"
    r = run('<chat url="https://claude.ai/chat/%s" kind="summary">Title: t\nWe wrote `%s` after a long verified run today.</chat>' % (U1, p), "--json")
    assert p in json.loads(r.stdout)["chats"][0]["files"]


def test_ticket_hash_survives_sentence_cleanup_and_loose_episode_phrasing():
    txt = ('<chat url="https://claude.ai/chat/%s" kind="summary">Title: t\n# Header\n'
           "For wq#77, Claude verified the install and the ticket was closed. "
           "The episode was committed and verified in the DB as `abcdef012345`.</chat>" % U1)
    c = json.loads(run(txt, "--json").stdout)["chats"][0]
    assert any("wq#77" in s for s in c["outcomes"])
    assert c["episodes"] == ["abcdef012345"]
