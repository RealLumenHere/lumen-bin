"""End-to-end test of node-bench against a stub node (a throwaway HOME, stub llama-bench, pre-staged fake models).
Run: python3 -m pytest tests/test_node_bench.py"""
import json
import os
import shutil
import subprocess
import tempfile
import time
import unittest
from unittest import mock
from importlib.machinery import SourceFileLoader
from pathlib import Path

nb = SourceFileLoader("node_bench", str(Path(__file__).resolve().parent.parent / "node-bench")).load_module()

STUB = """#!/usr/bin/env bash
# stub llama-bench: print fixture JSON for the -t list it was given
T="$4"; [ "$3" != "-t" ] && T=14
python3 - "$@" <<'PY'
import json,sys
a=sys.argv[1:]
t=[int(x) for x in a[a.index('-t')+1].split(',')]
out=[]
for n in t:
    out.append({"n_threads":n,"n_prompt":512,"n_gen":0,"avg_ts":50.0+n})
    out.append({"n_threads":n,"n_prompt":0,"n_gen":128,"avg_ts":10.0+n/10})
print(json.dumps(out))
PY
"""


class StubNode(unittest.TestCase):
    def setUp(self):
        self.home = Path(tempfile.mkdtemp())
        (self.home / "llama.cpp/build/bin").mkdir(parents=True)
        b = self.home / "llama.cpp/build/bin/llama-bench"
        b.write_text(STUB); b.chmod(0o755)
        env = dict(os.environ, HOME=str(self.home))
        subprocess.run(["git", "init", "-q", str(self.home / "llama.cpp")], check=True, env=env)
        subprocess.run(["git", "-C", str(self.home / "llama.cpp"), "-c", "user.name=t", "-c", "user.email=t@t",
                        "commit", "-q", "--allow-empty", "-m", "x"], check=True, env=env)
        (self.home / "models").mkdir()
        self.env = env

    def tearDown(self):
        shutil.rmtree(self.home, ignore_errors=True)

    def run_launch(self, items):
        runner = nb.build_runner("t1", items)
        r = subprocess.run(["bash", "-s"], input=nb.launch_script("t1", runner), env=self.env,
                           capture_output=True, text=True, timeout=30)
        self.assertIn("LAUNCHED", r.stdout, r.stderr)
        d = self.home / "bench/t1"
        for _ in range(100):
            if (d / "DONE").exists() or (d / "FAILED").exists():
                break
            time.sleep(0.1)
        return d

    def test_end_to_end(self):
        m = dict(nb.MODELS["qwen3-8b"], key="qwen3-8b", threads=[6, 14])
        (self.home / "models" / m["file"]).write_text("fake")
        d = self.run_launch([m])
        self.assertTrue((d / "DONE").exists(), (d / "run.log").read_text() if (d / "run.log").exists() else "no log")
        self.assertFalse((d / "FAILED").exists())
        facts = nb.collect_facts("stub", "t1", d)
        self.assertEqual(len(facts), 1)
        pred, val = facts[0]
        self.assertEqual(pred, "bench_qwen3_8b_q4km_t1")
        self.assertIn("t=14: pp512=64.0 tok/s, tg128=11.4 tok/s", val)
        self.assertIn("t=6: pp512=56.0 tok/s, tg128=10.6 tok/s", val)

    def test_failure_marks_FAILED(self):
        # model is absent and the stub has no network path: curl fails, the runner must mark FAILED, not DONE
        m = dict(nb.MODELS["qwen3-8b"], key="qwen3-8b", threads=[6], repo="invalid.invalid/none")
        m["file"] = "missing.gguf"
        d = self.run_launch([m])
        self.assertTrue((d / "FAILED").exists())
        self.assertFalse((d / "DONE").exists())


class ProbeAndFallback(unittest.TestCase):
    """wq#3591: PROBE must not exit nonzero just because the last MODELS file is absent, and
    ssh()/resolve() must fall back from a dead tailscale_ip to lan_ip on the connection outcome."""

    def test_probe_tail_exits_0_when_models_absent(self):
        files = " ".join(m["file"] for m in nb.MODELS.values())
        home = tempfile.mkdtemp()
        try:
            (Path(home) / "models").mkdir()  # empty: no model file present
            r = subprocess.run(["bash", "-s"], input=nb.PROBE.replace("@FILES@", files),
                               env=dict(os.environ, HOME=home), capture_output=True, text=True, timeout=30)
            self.assertEqual(r.returncode, 0, f"PROBE must exit 0 with no model files; stderr={r.stderr}")
            self.assertNotIn("HAVE=", r.stdout)
        finally:
            shutil.rmtree(home, ignore_errors=True)

    def test_ssh_timeout_is_connection_failure(self):
        with mock.patch.object(nb.subprocess, "run",
                               side_effect=subprocess.TimeoutExpired(cmd="ssh", timeout=8)):
            r = nb.ssh("u", "1.2.3.4", "echo hi", timeout=8)
        self.assertEqual(r.returncode, 255)

    def test_ssh_fb_retries_lan_on_255(self):
        calls = []
        def fake(user, ip, script, timeout=60):
            calls.append(ip)
            rc = 255 if ip == "100.1.2.3" else 0
            return subprocess.CompletedProcess([], rc, "ok" if rc == 0 else "", "")
        with mock.patch.object(nb, "ssh", side_effect=fake):
            r, used = nb.ssh_fb("u", "100.1.2.3", "192.168.8.125", "x")
        self.assertEqual(calls, ["100.1.2.3", "192.168.8.125"])
        self.assertEqual((used, r.returncode), ("192.168.8.125", 0))

    def test_ssh_fb_no_retry_on_success(self):
        with mock.patch.object(nb, "ssh",
                               return_value=subprocess.CompletedProcess([], 0, "ok", "")) as m:
            r, used = nb.ssh_fb("u", "100.1.2.3", "192.168.8.125", "x")
        self.assertEqual((used, m.call_count), ("100.1.2.3", 1))

    def test_resolve_offers_lan_fallback_for_tailscale_primary(self):
        state = "  ptah.user = lumen\n  ptah.lan_ip = 192.168.8.125  (x)\n  ptah.tailscale_ip = 100.78.207.56\n"
        with mock.patch.object(nb.subprocess, "run",
                               return_value=subprocess.CompletedProcess([], 0, state, "")):
            self.assertEqual(nb.resolve("ptah"), ("lumen", "100.78.207.56", "192.168.8.125"))

    def test_resolve_host_override_has_no_fallback(self):
        state = "  ptah.user = lumen\n  ptah.lan_ip = 192.168.8.125\n  ptah.tailscale_ip = 100.78.207.56\n"
        with mock.patch.object(nb.subprocess, "run",
                               return_value=subprocess.CompletedProcess([], 0, state, "")):
            self.assertEqual(nb.resolve("ptah", host="10.0.0.9"), ("lumen", "10.0.0.9", None))


if __name__ == "__main__":
    unittest.main()
