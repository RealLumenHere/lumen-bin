"""End-to-end test of node-bench against a stub node (a throwaway HOME, stub llama-bench, pre-staged fake models).
Run: python3 -m pytest tests/test_node_bench.py"""
import json
import os
import shutil
import subprocess
import tempfile
import time
import unittest
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


if __name__ == "__main__":
    unittest.main()
