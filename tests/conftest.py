"""wq#2139: `wq add` reads the live foreman policy (git HEAD) to refuse a dispatch-enabled lane
with no LANE_REPO row, and the CLI default lane ('auto') is exactly such a lane. Suites that
shell out to `wq add` are testing something else, so point the check at nothing: it fails open
(one stderr warning) and never depends on the state of the real foreman checkout.
tests/test_wq_lane_repo_guard.py builds its own scratch policy and overrides these."""
import os

os.environ["WQ_FOREMAN_ROUTER"] = "/nonexistent/wq2139/router.py"
os.environ["WQ_FOREMAN_DISPATCHER"] = "/nonexistent/wq2139/foreman_dispatcher.py"
