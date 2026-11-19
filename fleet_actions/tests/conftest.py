import os
import sys

# wq#2139 (same as tests/conftest.py): `wq add` must not read the live foreman policy.
os.environ["WQ_FOREMAN_ROUTER"] = "/nonexistent/wq2139/router.py"
os.environ["WQ_FOREMAN_DISPATCHER"] = "/nonexistent/wq2139/foreman_dispatcher.py"
os.environ["WQ_FOREMAN_REPO_SWEEP"] = "/nonexistent/wq2139/repo_sweep.py"
os.environ.setdefault("FLEET_ACTIONS_AUDIT", "/tmp/fleet-actions-test-audit-unused.jsonl")

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
