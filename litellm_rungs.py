"""litellm_rungs (wq#3212) — the ONE rung-list derivation both LiteLLM monitors
share, so `down=none` can never again coexist with dead rungs.

Before this module, lumen-model-watchdog probed a hardcoded 6-name roster
(ops/wq2903-model-ref-drift/lumen_model_watchdog.py CLOUD/LOCAL) while
litellm-completion-probe (wq#3169) derived per-host rungs from
litellm_config.yaml — so the watchdog logged `probed=6 down=none` while the
completion probe had 3 hosts dead (aq 2579/2583/2585). Both now enumerate
hosts through this module, reading the same config file.

Rules folded in from both callers:
  - NEVER_PROBE (LiteLLM bug #22249): concurrent /chat/completions on the
    messenger passthrough rungs corrupts Anthropic-passthrough state. These
    names are excluded from ELIGIBILITY, not just from selection — otherwise
    a host whose other rungs were removed would elect a never-probe model as
    its representative and probe it anyway.
  - SKIP_SUBSTRINGS (wq#3169): embed/rerank/whisper/tts deployments can't be
    exercised by a chat-completion probe.
  - Host key = api_base netloc, falling back to the model id's provider
    prefix for rungs with no explicit api_base (e.g. `anthropic/...`).

Deployed to /home/lumen/bin/litellm_rungs.py next to litellm-completion-probe;
the watchdog imports it from there (see install.sh).
"""
import collections
import pathlib
from urllib.parse import urlparse

import yaml

NEVER_PROBE = {"messenger-chat", "messenger-re", "gemini-flash"}  # wq#3675: gemini excluded to stop 429s
SKIP_SUBSTRINGS = ("embed", "rerank", "whisper", "tts")

# timeout = the rung's own litellm_params.timeout (seconds), None when unset
Rung = collections.namedtuple("Rung", "host model_name timeout", defaults=(None,))


def _eligible(entry: dict) -> bool:
    lp = entry.get("litellm_params", {}) or {}
    model_id = str(lp.get("model", ""))
    if any(s in model_id for s in SKIP_SUBSTRINGS):
        return False
    return entry.get("model_name") not in NEVER_PROBE


def _host(entry: dict) -> str:
    lp = entry.get("litellm_params", {}) or {}
    return (urlparse(str(lp.get("api_base", ""))).netloc
            or str(lp.get("model", "")).split("/")[0])


def _timeout(entry: dict):
    try:
        return float((entry.get("litellm_params", {}) or {})["timeout"])
    except (KeyError, TypeError, ValueError):
        return None


def _eligible_entries(config_path: str):
    cfg = yaml.safe_load(pathlib.Path(config_path).read_text())
    return [e for e in cfg.get("model_list", []) or [] if _eligible(e)]


def host_groups(config_path: str) -> "collections.OrderedDict[str, list]":
    """host -> [eligible chat model_names], in config order. A host whose only
    rungs are ineligible (never-probe / embed-class) does not appear at all."""
    groups = collections.OrderedDict()
    for entry in _eligible_entries(config_path):
        groups.setdefault(_host(entry), []).append(entry["model_name"])
    return groups


def one_rung_per_host(config_path: str) -> list:
    """One rung per distinct api_base host (a dead host fails every model on it
    identically, wq#3169). wq#3463: the representative is the rung with the
    SMALLEST configured timeout (the operator's own "this one answers fast"),
    config order breaking ties and untimed rungs ranking last — first-in-config
    elected gemma4-e4b-local (timeout 600) on Lumen over lumen-lfm2-1b (120)."""
    best = collections.OrderedDict()
    for entry in _eligible_entries(config_path):
        rung = Rung(_host(entry), entry["model_name"], _timeout(entry))
        cur = best.get(rung.host)
        if cur is None or (rung.timeout is not None
                           and (cur.timeout is None or rung.timeout < cur.timeout)):
            best[rung.host] = rung
    return list(best.values())
