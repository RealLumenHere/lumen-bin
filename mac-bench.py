#!/usr/bin/env python3
"""mac-bench.py - Standardized Mac Pro 6,1 Ollama benchmark"""
import subprocess, json, sys, time

def run_prompt(host, model, prompt, label):
    payload = json.dumps({"model": model, "prompt": prompt, "stream": False})
    cmd = ["ssh", "-n", "-o", "ConnectTimeout=30", host,
           "curl -s --max-time 600 http://localhost:11434/api/generate -d " + repr(payload)]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=660)
        d = json.loads(result.stdout)
        eval_dur = d.get("eval_duration", 1) / 1e9
        eval_count = d.get("eval_count", 0)
        prompt_dur = d.get("prompt_eval_duration", 1) / 1e9
        prompt_count = d.get("prompt_eval_count", 0)
        total = d.get("total_duration", 1) / 1e9
        decode_rate = eval_count / eval_dur if eval_dur > 0 else 0
        prefill_rate = prompt_count / prompt_dur if prompt_dur > 0 else 0
        line = "%s: decode=%.2f tok/s, prefill=%.2f tok/s, prompt_tokens=%d, gen_tokens=%d, total=%.1fs" % (
            label, decode_rate, prefill_rate, prompt_count, eval_count, total)
        print(line)
        return {"decode": decode_rate, "prefill": prefill_rate,
                "prompt_tokens": prompt_count, "gen_tokens": eval_count, "total": total}
    except Exception as e:
        print("%s: FAILED - %s" % (label, e))
        return None

host = sys.argv[1]
model = sys.argv[2]
print("=== Benchmarking %s on %s ===" % (model, host))
ts = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
print("Started: %s" % ts)

short_prompt = "Write a hello world in Python"
medium_prompt = "Write a Python class for a binary search tree with insert, delete, search, and in-order traversal. Include docstrings and type hints."
long_prompt = ("You are reviewing a large Python codebase. The codebase has the following structure: "
    "a main.py file that imports from utils.py, config.py, database.py, and api.py. "
    "The main.py handles CLI argument parsing and orchestration. The utils.py has string manipulation, "
    "file I/O, and logging helpers. The config.py reads TOML configuration files and validates schemas. "
    "The database.py manages SQLite connections with connection pooling and migration support. "
    "The api.py implements a FastAPI REST server with authentication middleware. "
    "There is a bug where the database connection pool exhausts under concurrent API requests because "
    "the middleware creates a new connection per request instead of using the pool. "
    "The config validation silently accepts invalid TOML keys instead of raising errors. "
    "The logging helper writes to stdout instead of a rotating file handler. "
    "Fix all three bugs. Show the corrected code for each file with explanations.")

print("")
print("--- SHORT ---")
r1 = run_prompt(host, model, short_prompt, "SHORT")
print("")
print("--- MEDIUM ---")
r2 = run_prompt(host, model, medium_prompt, "MEDIUM")
print("")
print("--- LONG ---")
r3 = run_prompt(host, model, long_prompt, "LONG")

ts2 = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
print("")
print("=== Done %s on %s at %s ===" % (model, host, ts2))
