"""re-inventory: unit planning, sync status from LEGS/log/state, symlinked root. Temp trees only."""
import importlib.machinery
import importlib.util
import json
import os
import subprocess
import sys

BIN = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "re-inventory")


def put(p, text="x", mtime=None):
    os.makedirs(os.path.dirname(p), exist_ok=True)
    with open(p, "w") as f:
        f.write(text)
    if mtime:
        os.utime(p, (mtime, mtime))


def make_store(tmp_path):
    real = tmp_path / "archive" / "re"
    link = tmp_path / "re"
    real.mkdir(parents=True)
    os.symlink(real, link)  # mirrors /home/lumen/re -> /data/lumen/archive/re
    put(str(real / "miko/miko2/a.bin"), "aaaa", 1_700_000_000)
    put(str(real / "miko/miko3/b.bin"), "bb", 1_700_000_100)
    put(str(real / "miko/README"), "r")
    put(str(real / "common/scripts/s.sh"), "s")
    put(str(real / "common/apks/a.apk"), "apk")
    put(str(real / "rk3399-scanner/dump/d.img"), "dddddddd", 1_700_000_200)
    put(str(real / "quest/q.txt"), "q")
    sync = tmp_path / "re-sync"
    L = str(link)
    sync.write_text(
        "LEGS=(\n"
        '  "thor|thor@1.1.1.1|/p/miko2/|%s/miko/miko2/|"\n'
        '  "thor|thor@1.1.1.1|/p/miko3/|%s/miko/miko3/|"\n'
        '  "thor|thor@1.1.1.1|/m/*.sh|%s/common/scripts/|"\n'
        '  "maksoor|maksoor@2.2.2.2|/d/|%s/rk3399-scanner/dump/|sudo rsync"\n'
        '  "maksoor|maksoor@2.2.2.2|/x.img|%s/rk3399-scanner/|sudo rsync"\n'
        '  "maksoor|maksoor@2.2.2.2|/a/|%s/common/apks/|sudo rsync"\n'
        ")\n" % (L, L, L, L, L, L))
    log = tmp_path / "re-sync.log"
    log.write_text(
        "2026-09-30T23:30:00Z   OK   rc=0 thor@1.1.1.1:/p/miko2/ -> %s/miko/miko2/\n"
        "2026-09-30T23:31:00Z   FAIL rc=23 thor@1.1.1.1:/p/miko3/ -> %s/miko/miko3/ :: boom\n"
        "2026-09-30T10:00:00Z   OK   rc=0 thor@1.1.1.1:/m/*.sh -> %s/common/scripts/\n"
        "2026-09-29T10:00:00Z   OK   rc=0 maksoor@2.2.2.2:/d/ -> %s/rk3399-scanner/dump/\n"
        "2026-09-28T03:00:00Z   FAIL rc=23 thor@1.1.1.1:/gone/ -> /tmp/re-sync-t2/ :: test leg, ignored\n"
        % (L, L, L, L))
    state = tmp_path / "re-sync.state"
    state.write_text("thor=0\nmaksoor=40\n")
    return real, link, sync, log, state


def run(tmp_path, *args):
    if (tmp_path / "archive").exists():
        real, link, sync, log, state = (tmp_path / "archive" / "re", tmp_path / "re", tmp_path / "re-sync",
                                        tmp_path / "re-sync.log", tmp_path / "re-sync.state")
    else:
        real, link, sync, log, state = make_store(tmp_path)
    env = dict(os.environ, RI_ROOT=str(link), RI_LINK=str(link), RI_SYNC_SCRIPT=str(sync),
               RI_SYNC_LOG=str(log), RI_SYNC_STATE=str(state), RI_NOW="2026-10-01T00:00:00Z")
    r = subprocess.run([sys.executable, BIN, *args], capture_output=True, text=True, env=env, timeout=60)
    return r


def test_manifest_units_sizes_and_status(tmp_path):
    r = run(tmp_path, "--json")
    assert r.returncode == 0, r.stderr
    m = json.loads(r.stdout)
    byname = {p["name"]: p for p in m["projects"]}
    # symlinked root resolved, containers split, loose container files get their own unit
    assert set(byname) == {"common/apks", "common/scripts", "miko/", "miko/miko2", "miko/miko3", "quest", "rk3399-scanner"}
    assert m["real_root"].endswith("archive/re")
    assert byname["miko/miko2"]["bytes"] == 4 and byname["miko/miko2"]["files"] == 1
    assert byname["miko/miko2"]["newest_file"] == "a.bin"
    assert byname["rk3399-scanner"]["bytes"] == 8
    st = {k: v["sync"]["status"] for k, v in byname.items()}
    assert st["miko/miko2"] == "synced"          # OK 30 min ago
    assert st["miko/miko3"] == "fail"            # last event FAIL on a reachable host
    assert st["common/scripts"] == "stale"       # OK 14h ago, threshold 3h
    assert st["rk3399-scanner"] == "host-offline"  # maksoor state=40
    assert st["quest"] == "lumen-only"
    assert m["total_files"] == sum(p["files"] for p in m["projects"])
    off = byname["rk3399-scanner"]["sync"]["legs"][0]
    assert off["host_offline_runs"] == 40


def test_never_synced_when_log_has_no_ok(tmp_path):
    real, link, sync, log, state = make_store(tmp_path)
    log.write_text("")
    env = dict(os.environ, RI_ROOT=str(link), RI_LINK=str(link), RI_SYNC_SCRIPT=str(sync),
               RI_SYNC_LOG=str(log), RI_SYNC_STATE=str(state), RI_NOW="2026-10-01T00:00:00Z")
    r = subprocess.run([sys.executable, BIN, "--json"], capture_output=True, text=True, env=env, timeout=60)
    m = json.loads(r.stdout)
    st = {p["name"]: p["sync"]["status"] for p in m["projects"]}
    assert st["miko/miko2"] == "never-synced"


def test_human_output_and_out_file(tmp_path):
    out = tmp_path / "m.json"
    r = run(tmp_path, "--out", str(out))
    assert r.returncode == 0 and r.stdout == ""  # --out alone is quiet
    assert json.loads(out.read_text())["project_count"] == 7
    r2 = run(tmp_path)
    assert "PROJECT" in r2.stdout and "miko/miko2" in r2.stdout and "host-offline" in r2.stdout
    assert "maksoor offline 40 runs" in r2.stdout


def test_bad_root_exits_2(tmp_path):
    env = dict(os.environ, RI_ROOT=str(tmp_path / "nope"))
    r = subprocess.run([sys.executable, BIN], capture_output=True, text=True, env=env, timeout=30)
    assert r.returncode == 2
