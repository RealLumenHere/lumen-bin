"""wq#5012: agent-attention-notify — ntfy push when an interactive Claude Code session is
blocked waiting for Frank. Every case runs the real script as a subprocess with a fake
ntfy-send that records its argv; nothing is published and no live state is touched."""
import json
import os
import shutil
import subprocess
import sys
import time

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
SCRIPT = os.path.join(os.path.dirname(HERE), "agent-attention-notify")


@pytest.fixture
def env(tmp_path):
    rec = tmp_path / "sent.jsonl"
    fake = tmp_path / "ntfy-send"
    fake.write_text("#!/usr/bin/env python3\nimport json,sys\n"
                    "open(%r,'a').write(json.dumps(sys.argv[1:])+'\\n')\n" % str(rec))
    fake.chmod(0o755)
    mask = tmp_path / "mask-secrets"
    mask.write_text("#!/bin/sh\nsed 's/ghp_[A-Za-z0-9]*/ghp_MASKED/g'\n")
    mask.chmod(0o755)
    e = dict(os.environ)
    e.pop("WQ_AGENT", None)
    e.update(AGENT_ATTENTION_CONF=str(tmp_path / "conf"), AGENT_ATTENTION_STATE=str(tmp_path / "state"),
             AGENT_ATTENTION_NTFY_BIN=str(fake), AGENT_ATTENTION_MASK_BIN=str(mask),
             AGENT_ATTENTION_ASSUME_TTY="1")
    return {"env": e, "rec": rec, "tmp": tmp_path}


def run(env, event, **extra):
    e = dict(env["env"], **extra)
    data = event if isinstance(event, str) else json.dumps(event)
    out = subprocess.run([sys.executable, SCRIPT], input=data, capture_output=True, text=True,
                         env=e, timeout=30)
    assert out.returncode == 0, out.stderr
    return out


def sent(env, wait=3.0):
    """The publish is detached, so poll briefly for the recorder."""
    deadline = time.time() + wait
    while time.time() < deadline:
        if env["rec"].exists():
            return [json.loads(l) for l in env["rec"].read_text().splitlines()]
        time.sleep(0.05)
    return []


def ev(ntype, sid="abcdef1234567890", msg="Claude needs your permission to use Bash"):
    return {"hook_event_name": "Notification", "notification_type": ntype, "session_id": sid,
            "cwd": "/home/lumen/projects/ccc", "message": msg, "title": "Permission needed"}


def test_permission_prompt_sends_high_priority(env):
    run(env, ev("permission_prompt"))
    calls = sent(env)
    assert len(calls) == 1
    argv = calls[0]
    assert argv[argv.index("-p") + 1] == "high"
    assert argv[argv.index("-t") + 1] == "lumen-fleet"
    title = argv[argv.index("-T") + 1]
    assert title.startswith("Agent BLOCKED: ") and title.endswith("/ccc")
    assert "abcdef12" in argv[-1] and "permission_prompt" in argv[-1]
    assert "abcdef1234567890" not in argv[-1], "full session id must not leave the box"


def test_repeat_inside_window_is_throttled_other_session_is_not(env):
    run(env, ev("permission_prompt"))
    run(env, ev("permission_prompt"))
    run(env, ev("elicitation_dialog", sid="ffff000011112222"))
    time.sleep(0.5)
    assert len(sent(env)) == 2
    log = (env["tmp"] / "state" / "log.jsonl").read_text()
    assert '"why": "throttled"' in log


def test_idle_prompt_is_default_priority(env):
    run(env, ev("idle_prompt", msg="Claude is waiting for your input"))
    argv = sent(env)[0]
    assert argv[argv.index("-p") + 1] == "default"
    assert argv[argv.index("-T") + 1].startswith("Agent waiting: ")


@pytest.mark.parametrize("ntype", ["auth_success", "agent_completed", "plugin_notification", ""])
def test_untracked_types_send_nothing(env, ntype):
    run(env, ev(ntype))
    assert sent(env, wait=0.5) == []


def test_non_notification_events_and_bad_json_send_nothing(env):
    run(env, {"hook_event_name": "Stop", "session_id": "x"})
    run(env, "{not json")
    run(env, "")
    assert sent(env, wait=0.5) == []


def test_fworker_noninteractive_and_disabled_are_skipped(env):
    run(env, ev("permission_prompt", sid="s1"), WQ_AGENT="fworker-wq4242")
    run(env, ev("permission_prompt", sid="s2"), AGENT_ATTENTION_ASSUME_TTY="0")
    (env["tmp"] / "conf").mkdir()
    (env["tmp"] / "conf" / "DISABLED").touch()
    run(env, ev("permission_prompt", sid="s3"))
    assert sent(env, wait=0.5) == []
    log = (env["tmp"] / "state" / "log.jsonl").read_text()
    for why in ("fworker", "non-interactive", "DISABLED"):
        assert why in log


def test_message_is_masked_capped_and_label_prefers_wq_agent(env):
    secret = "ghp_" + "A" * 36
    run(env, ev("agent_needs_input", msg="token " + secret + " " + "x" * 400),
        WQ_AGENT="orch-build-p2-f3dfac")
    argv = sent(env)[0]
    body = argv[-1]
    assert secret not in body and "ghp_MASKED" in body
    assert len(body.splitlines()[0]) <= 160
    assert argv[argv.index("-T") + 1].endswith("/orch-build-p2-f3dfac")


def test_mask_failure_withholds_text(env):
    run(env, ev("permission_prompt", msg="ghp_" + "B" * 36),
        AGENT_ATTENTION_MASK_BIN="/nonexistent/mask-secrets")
    body = sent(env)[0][-1]
    assert "ghp_" not in body and "withheld" in body


def test_missing_ntfy_bin_still_exits_zero(env):
    run(env, ev("permission_prompt"), AGENT_ATTENTION_NTFY_BIN="/nonexistent/ntfy-send")
    assert '"decision": "error"' in (env["tmp"] / "state" / "log.jsonl").read_text()


@pytest.mark.skipif(not shutil.which("script"), reason="util-linux script(1) not available")
def test_tty_detection_walks_up_to_a_claude_ancestor(env, tmp_path):
    """No ASSUME_TTY override: a fake `claude` started on a pty runs the hook through a
    shell (as Claude Code may), so the hook must walk past the shell to find claude's tty.
    The same fake started with -p, or without a pty, must be skipped."""
    bindir = tmp_path / "bin"
    bindir.mkdir()
    payload = tmp_path / "event.json"
    payload.write_text(json.dumps(ev("permission_prompt", sid="pty00000")))
    claude = bindir / "claude"
    claude.write_text("#!/bin/bash\nsh -c '%s %s < %s'\n" % (sys.executable, SCRIPT, payload))
    claude.chmod(0o755)
    e = dict(env["env"])
    e.pop("AGENT_ATTENTION_ASSUME_TTY")
    subprocess.run(["script", "-qec", str(claude), "/dev/null"], env=e, timeout=30,
                   stdin=subprocess.DEVNULL, capture_output=True)
    assert len(sent(env)) == 1
    payload.write_text(json.dumps(ev("permission_prompt", sid="pty11111")))
    subprocess.run(["script", "-qec", "%s -p" % claude, "/dev/null"], env=e, timeout=30,
                   stdin=subprocess.DEVNULL, capture_output=True)
    payload.write_text(json.dumps(ev("permission_prompt", sid="pty22222")))
    subprocess.run([str(claude)], env=e, timeout=30, stdin=subprocess.DEVNULL, capture_output=True)
    time.sleep(0.5)
    assert len(sent(env)) == 1
    log = (env["tmp"] / "state" / "log.jsonl").read_text()
    assert log.count("non-interactive") == 2
