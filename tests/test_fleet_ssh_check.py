"""fleet-ssh-check: classification, roster parse, sweep states, ticket dedupe. No network, no real ssh."""
import importlib.machinery
import importlib.util
import json
import os
import socket
import stat
import subprocess
import sys
import threading

import pytest

BIN = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "fleet-ssh-check")


def _load():
    loader = importlib.machinery.SourceFileLoader("fleet_ssh_check", BIN)
    spec = importlib.util.spec_from_loader("fleet_ssh_check", loader)
    mod = importlib.util.module_from_spec(spec)
    loader.exec_module(mod)
    return mod


def _script(path, body):
    path.write_text("#!/bin/bash\n" + body)
    path.chmod(path.stat().st_mode | stat.S_IEXEC)
    return str(path)


@pytest.mark.parametrize("err,want", [
    ("user@h: Permission denied (publickey).", "key-rejected"),
    ("ssh: connect to host h port 22: Connection refused", "ssh-refused"),
    ("ssh: connect to host h port 22: Connection timed out", "offline"),
    ("ssh: connect to host h port 22: No route to host", "offline"),
    ("@@@ WARNING: REMOTE HOST IDENTIFICATION HAS CHANGED! @@@", "hostkey-changed"),
    ("Host key verification failed.", "hostkey-changed"),
    ("# Tailscale SSH requires an additional check. Visit https://login.tailscale.com/a/abc", "ts-check"),
    ("something odd", "error"),
])
def test_classify(err, want):
    assert _load().classify(err) == want


@pytest.fixture
def env(tmp_path, monkeypatch):
    """listener on a free port = the 'ssh port'; fake fleet-inv, ssh, wq, tailscale."""
    srv = socket.socket()
    srv.bind(("127.0.0.1", 0))
    srv.listen(8)
    stop = threading.Event()

    def accept():
        srv.settimeout(0.2)
        while not stop.is_set():
            try:
                c, _ = srv.accept()
                c.close()
            except OSError:
                pass
    threading.Thread(target=accept, daemon=True).start()
    port = srv.getsockname()[1]

    inv = _script(tmp_path / "fleet-inv",
                  "printf '# phone: ssh method n/a — not reachable by design\\n'\n"
                  "printf '# aten: no ssh target (user=VERIFY, tailscale_ip=VERIFY)\\n' >&2\n"
                  "printf 'good\\tgood\\t127.0.0.1\\t\\tactive\\n'\n"
                  "printf 'locked\\tlocked\\t127.0.0.1\\t\\tactive\\n'\n"
                  "printf 'benchbox\\tbenchbox\\t127.0.0.1\\t\\tbench\\n'\n"
                  "printf 'dead\\tdead\\t192.0.2.1\\t\\tactive\\n'\n")
    ssh = _script(tmp_path / "ssh",
                  'for a in "$@"; do last=$a; done\n'
                  'case "$*" in\n'
                  '  *locked@*) echo "locked@127.0.0.1: Permission denied (publickey)." >&2; exit 255;;\n'
                  '  *benchbox@*) echo "Permission denied (publickey)." >&2; exit 255;;\n'
                  'esac\necho fakehost\n')
    wqlog = tmp_path / "wq.log"
    wq = _script(tmp_path / "wq",
                 'if [ "$1" = list ]; then cat ' + str(tmp_path / "wq.list") + ' 2>/dev/null; exit 0; fi\n'
                 'echo "$@" >> ' + str(wqlog) + '\necho "added task 9001 [build]"\n')
    ts = _script(tmp_path / "tailscale", 'echo \'{"Peer":{},"Self":{"TailscaleIPs":["127.0.0.1"],"Online":true}}\'\n')
    monkeypatch.setenv("FSC_FLEET_INV", inv)
    monkeypatch.setenv("FSC_SSH", ssh)
    monkeypatch.setenv("FSC_WQ", wq)
    monkeypatch.setenv("FSC_TAILSCALE", ts)
    yield {"port": port, "wqlog": wqlog, "list": tmp_path / "wq.list"}
    stop.set()
    srv.close()


def run(env_, *args):
    r = subprocess.run([sys.executable, BIN, "--port", str(env_["port"]), "--timeout", "2", *args],
                       capture_output=True, text=True, timeout=60)
    return r


def test_sweep_states_and_exit(env):
    r = run(env, "--json")
    assert r.returncode == 1  # locked + benchbox key-rejected, dead offline
    d = json.loads(r.stdout)
    st = {n["name"]: n["state"] for n in d["nodes"]}
    assert st["good"] == "ok"
    assert st["locked"] == "key-rejected"
    assert st["benchbox"] == "key-rejected"
    assert st["dead"] == "offline"
    assert st["phone"] == "n/a" and st["aten"] == "n/a"


def test_clean_exit_zero(env):
    r = run(env, "--nodes", "good", "--quiet")
    assert r.returncode == 0 and r.stdout == ""


def test_unknown_node_is_tool_error(env):
    assert run(env, "--nodes", "nope").returncode == 2


def test_tickets_only_active_and_deduped(env):
    r = run(env, "--file-tickets")
    assert r.returncode == 1
    filed = env["wqlog"].read_text()
    assert "[fleet-ssh] locked:" in filed and "[fleet-ssh] dead:" in filed
    assert "benchbox" not in filed  # bench status never ticketed
    assert "--liveness" in filed
    # now pretend both exist open (one done): locked open, dead done -> only dead refiled
    env["wqlog"].write_text("")
    env["list"].write_text("[11] build p2 open   [fleet-ssh] locked: key-rejected (x)\n"
                           "[12] build p3 done   [fleet-ssh] dead: offline (y)\n")
    run(env, "--file-tickets")
    again = env["wqlog"].read_text()
    assert "[fleet-ssh] locked:" not in again
    assert "[fleet-ssh] dead:" in again


def test_dry_run_files_nothing(env):
    r = run(env, "--file-tickets", "--dry-run")
    assert "ticket (dry)" in r.stdout
    assert not env["wqlog"].exists()


def test_tailnet_last_seen_is_pacific():
    m = _load()
    assert m.pt_stamp("2026-09-13T07:36:12.123456789Z") == "2026-09-13 00:36"   # PDT = UTC-7
    assert m.pt_stamp("0001-01-01T00:00:00Z") == "" and m.pt_stamp("garbage") == ""
    assert m.fmt_tailnet({"online": False, "last_seen": "2026-12-01T08:00:00Z"}) == "down (seen 2026-12-01 00:00 PT)"
    assert m.fmt_tailnet({"online": True}) == "up" and m.fmt_tailnet(None) == "-"
