#!/bin/bash
# mac-bench.sh - Standardized Mac Pro 6,1 Ollama benchmark
# Usage: mac-bench.sh <hostname> <model>
set -euo pipefail

HOST=$1
MODEL=$2
echo "=== Benchmarking $MODEL on $HOST ==="
echo "Started: $(date -u +%Y-%m-%dT%H:%M:%SZ)"

# Short prompt (decode speed test)
echo ""
echo "--- SHORT PROMPT ---"
ssh -n -o ConnectTimeout=30 $HOST "curl -s --max-time 120 http://localhost:11434/api/generate -d '{
  \"model\": \"\",
  \"prompt\": \"Write a hello world in Python\",
  \"stream\": false
}'" | python3 -c "
import sys,json
d=json.load(sys.stdin)
eval_dur=d.get(eval_duration,1)/1e9
eval_count=d.get(eval_count,0)
prompt_dur=d.get(prompt_eval_duration,1)/1e9
prompt_count=d.get(prompt_eval_count,0)
total=d.get(total_duration,1)/1e9
print(fSHORT - decode: {eval_count/eval_dur:.2f} tok/s, prefill: {prompt_count/prompt_dur:.2f} tok/s, tokens: {eval_count}, total: {total:.1f}s)
"

# Medium prompt (real coding task)
echo ""
echo "--- MEDIUM PROMPT ---"
ssh -n -o ConnectTimeout=30 $HOST "curl -s --max-time 300 http://localhost:11434/api/generate -d '{
  \"model\": \"\",
  \"prompt\": \"Write a Python class for a binary search tree with insert, delete, search, and in-order traversal. Include docstrings and type hints.\",
  \"stream\": false
}'" | python3 -c "
import sys,json
d=json.load(sys.stdin)
eval_dur=d.get(eval_duration,1)/1e9
eval_count=d.get(eval_count,0)
prompt_dur=d.get(prompt_eval_duration,1)/1e9
prompt_count=d.get(prompt_eval_count,0)
total=d.get(total_duration,1)/1e9
print(fMEDIUM - decode: {eval_count/eval_dur:.2f} tok/s, prefill: {prompt_count/prompt_dur:.2f} tok/s, tokens: {eval_count}, total: {total:.1f}s)
"

# Long prompt (large context prefill test)
echo ""
echo "--- LONG PROMPT ---"
LONG_PROMPT="You are reviewing a large Python codebase. The codebase has the following structure: a main.py file that imports from utils.py, config.py, database.py, and api.py. The main.py handles CLI argument parsing and orchestration. The utils.py has string manipulation, file I/O, and logging helpers. The config.py reads TOML configuration files and validates schemas. The database.py manages SQLite connections with connection pooling and migration support. The api.py implements a FastAPI REST server with authentication middleware. There is a bug where the database connection pool exhausts under concurrent API requests because the middleware creates a new connection per request instead of using the pool. The config validation silently accepts invalid TOML keys instead of raising errors. The logging helper writes to stdout instead of a rotating file handler. Fix all three bugs. Show the corrected code for each file with explanations of what was wrong and why your fix is correct."
ssh -n -o ConnectTimeout=30 $HOST "curl -s --max-time 600 http://localhost:11434/api/generate -d '{
  \"model\": \"\",
  \"prompt\": \"\",
  \"stream\": false
}'" | python3 -c "
import sys,json
d=json.load(sys.stdin)
eval_dur=d.get(eval_duration,1)/1e9
eval_count=d.get(eval_count,0)
prompt_dur=d.get(prompt_eval_duration,1)/1e9
prompt_count=d.get(prompt_eval_count,0)
total=d.get(total_duration,1)/1e9
print(fLONG - decode: {eval_count/eval_dur:.2f} tok/s, prefill: {prompt_count/prompt_dur:.2f} tok/s, prompt_tokens: {prompt_count}, gen_tokens: {eval_count}, total: {total:.1f}s)
"

echo ""
echo "=== Benchmark complete for $MODEL on $HOST ==="
echo "Finished: $(date -u +%Y-%m-%dT%H:%M:%SZ)"
