#!/usr/bin/env python3
"""model-ref-lint (wq#2903): every gateway model name a caller references must exist
in the live LiteLLM model_list.

Class: a rung leaves litellm_config.yaml and its callers keep calling it. mistral-small
was dropped 2026-08-30; model-watchdog, lumen-fleet-mistral and model_audit kept calling
it (1643 ProxyModelNotFoundError in 5d). minerva-bench-2 called wq2821-judge-llama8b,
which never existed (wq#2904, 169 failures). Nothing compared the callers to the list.

Two finding kinds:
  RETIRED  a quoted string literal equal to a name that some litellm_config.yaml.bak*
           model_list carried and the live one does not. High precision, any context.
  UNKNOWN  a name in model context (`"model": "x"`, `model="x"`, `*_MODEL*=x`,
           `--model x`) that is in neither the live model_list nor the fleet CLI roster
           (foreman/research/models.yaml ids/aliases). Provider-native ids (`a/b`,
           `name:tag`) are skipped: those go straight to a provider, not the gateway.
Known non-gateway names go in ALLOW (below) or --allow-file, one per line, with a reason.

Read-only. Exit 0 clean, 1 drift, 2 the check could not run.
--aq opens/repeats one aq row (source=model-ref-lint, key=model-ref-drift) on drift and
resolves it when clean. --json for machine output.
"""
import argparse, glob, json, os, re, sys

CONFIG = "/home/lumen/agent/litellm_config.yaml"
MODELS_YAML = "/home/lumen/foreman/research/models.yaml"
ROOTS = ["/home/lumen/bin", "/home/lumen/agents", "/home/lumen/foreman",
         "/home/lumen/services", "/home/lumen/minerva", "/home/lumen/agent",
         "/etc/systemd/system", "/home/lumen/.config/systemd/user"]
EXTS = {".py", ".sh", ".json", ".yaml", ".yml", ".conf", ".service", ".timer", ".toml"}
# docs/history/staging trees: they quote retired names on purpose, and every deployed
# copy of an ops bundle is scanned where it is installed.
SKIP_DIRS = {".git", "__pycache__", "node_modules", ".venv", "venv", "site-packages",
             "tests", "test", "fixtures", "reports", "research", "docs", "plans",
             "pilots", "ops", "worktrees", "examples", "logs", "archive", "data",
             ".pytest_cache", "_runs", ".secrets",
             # runtime data, not callers: backup stages, trace dumps, drift plans
             "backup", "stage", "events", "trace", "drift_plans", "cache"}
SKIP_FILES = {"state.json"}             # a watchdog's own last-run state is not a caller
MAX_BYTES = 1_000_000
# Names that look like gateway aliases but are not gateway calls. Keep a reason on each.
ALLOW = {
    "llama-3.1-8b-instant",             # Groq API direct (agent/lumen_research_agent.py)
    "llama-3.3-70b-versatile",          # Groq API direct (agent/lumen_deep_research_orchestrator.py)
    "gpt-oss-120b",                     # Cerebras API direct (foreman/harness/liveness.py)
    "nomic-embed-text", "minicpm-v4.6", # Ollama native API (minerva verify, vision-eval)
    "recorded-rung",                    # fixture row in bin/minerva-presentation-stability
    "llama33-local",                    # retired-rung registry tuple, minerva/lumen_state_single_owner_migrate.py
}

MODEL_NAME_RE = re.compile(r"^\s*-\s*model_name:\s*[\"']?([^\"'\s#]+)", re.M)
CLI_RE = re.compile(r"^\s*-?\s*(?:id|alias):\s*[\"']?([A-Za-z0-9._:-]+)", re.M)
CONTEXT_RES = [
    re.compile(r"[\"']model[\"']\s*:\s*[\"']([^\"']+)[\"']"),
    re.compile(r"\bmodel\s*=\s*[\"']([^\"']+)[\"']"),
    re.compile(r"\b[A-Z][A-Z0-9_]*MODEL[A-Z0-9_]*\s*[:=]\s*[\"']?([A-Za-z0-9._:/-]+)"),
    re.compile(r"--model[ =][\"']?([A-Za-z0-9._:/-]+)"),
]
# a quoted literal, or one item of a quoted comma list ("a,b,c" as in AUDIT_MODELS)
QUOTED_RE = re.compile(r"[\"']([A-Za-z0-9][A-Za-z0-9._, -]*)[\"']")
ALIAS_SHAPE = re.compile(r"^[a-z0-9][a-z0-9._-]*[a-z0-9]$")


def model_names(text):
    return set(MODEL_NAME_RE.findall(text))


def load_live(config):
    with open(config) as fh:
        names = model_names(fh.read())
    if not names:
        raise RuntimeError(f"no model_name entries parsed from {config}")
    return names


def load_retired(config, live):
    """Every name some backup of the config carried that the live list lacks."""
    seen = set()
    for bak in glob.glob(config + ".bak*"):
        try:
            with open(bak, errors="replace") as fh:
                seen |= model_names(fh.read())
        except OSError:
            continue
    return seen - live


def load_cli(path):
    try:
        with open(path) as fh:
            return set(CLI_RE.findall(fh.read()))
    except OSError:
        return set()


def is_code_line(line):
    s = line.lstrip()
    return bool(s) and not s.startswith(("#", "//", ";"))


def gateway_shaped(name):
    return bool(ALIAS_SHAPE.match(name)) and "/" not in name and ":" not in name \
        and any(c.isalpha() for c in name) and ("-" in name or any(c.isdigit() for c in name))


def scan_text(text, path, live, retired, known):
    out = []
    py = path.endswith(".py") or text.startswith("#!/usr/bin/env python")
    in_doc = False
    for n, line in enumerate(text.splitlines(), 1):
        if py:                          # docstrings narrate history, they don't call
            q = line.count('"""') + line.count("'''")
            doc_line = in_doc or q > 0
            if q % 2:
                in_doc = not in_doc
            if doc_line:
                continue
        if not is_code_line(line):
            continue
        hit = set()
        for m in QUOTED_RE.finditer(line):
            for name in re.split(r"[,\s]+", m.group(1)):
                # bare words ("local", "smart") are ordinary strings too; only a
                # hyphenated retired name is specific enough to flag in any context
                if name in retired and "-" in name and name not in known and name not in hit:
                    out.append({"file": path, "line": n, "model": name, "kind": "RETIRED"})
                    hit.add(name)
        for rx in CONTEXT_RES:
            for m in rx.finditer(line):
                name = m.group(1)
                if name in hit or name in known or name in live or not gateway_shaped(name):
                    continue
                if name.startswith(("claude-", "$", "{")):
                    continue            # Anthropic CLI/API ids and templated values
                kind = "RETIRED" if name in retired else "UNKNOWN"
                out.append({"file": path, "line": n, "model": name, "kind": kind})
                hit.add(name)
    return out


def wanted(path):
    base = os.path.basename(path)
    if base in SKIP_FILES or ".bak" in base or base.endswith((".orig", ".rej", "~")) or base == ".env" \
            or base.endswith(".env") or "litellm_config" in base:
        return False
    ext = os.path.splitext(base)[1]
    if ext in EXTS:
        return True
    if ext:
        return False
    try:                                # extensionless tools in bin/: shebang only
        with open(path, "rb") as fh:
            return fh.read(2) == b"#!"
    except OSError:
        return False


def iter_files(roots):
    for root in roots:
        if os.path.isfile(root):
            yield root
            continue
        for dp, dns, fns in os.walk(root, onerror=lambda e: None):
            dns[:] = [d for d in dns if d not in SKIP_DIRS and not d.startswith(".bak")]
            for fn in fns:
                p = os.path.join(dp, fn)
                if os.path.islink(p) or not wanted(p):
                    continue
                try:
                    if os.path.getsize(p) > MAX_BYTES:
                        continue
                except OSError:
                    continue
                yield p


def lint(roots, config, models_yaml, allow):
    live = load_live(config)
    retired = load_retired(config, live)
    known = load_cli(models_yaml) | allow
    findings = []
    for p in iter_files(roots):
        try:
            with open(p, errors="replace") as fh:
                text = fh.read()
        except OSError:
            continue
        findings += scan_text(text, p, live, retired, known)
    return live, retired, findings


def aq_sync(findings):
    sys.path.insert(0, "/home/lumen/agents/alert-queue")
    import aq
    key = "model-ref-drift"
    if findings:
        names = sorted({f["model"] for f in findings})
        where = "; ".join(f"{f['model']} {f['file']}:{f['line']}" for f in findings[:8])
        aq.ingest(source="model-ref-lint", key=key,
                  title=f"model ref drift: {len(names)} name(s) not in LiteLLM model_list",
                  message=f"{', '.join(names)} | {where} (fix: point the caller at a live "
                          f"rung or restore the model_list entry; wq#2903)",
                  priority="high")
    else:
        c = aq.db()
        c.execute("UPDATE alerts SET status='resolved', note='auto-cleared: model-ref-lint clean', "
                  "resolved_ts=? WHERE source='model-ref-lint' AND key=? AND status='open'",
                  (aq.now(), key))
        c.commit(); c.close()


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0],
                                 epilog="example: model-ref-lint --aq   |   model-ref-lint --json /home/lumen/bin")
    ap.add_argument("roots", nargs="*", help=f"files/dirs to scan (default: {' '.join(ROOTS)})")
    ap.add_argument("--config", default=CONFIG)
    ap.add_argument("--models-yaml", default=MODELS_YAML)
    ap.add_argument("--allow-file", help="extra allowed names, one per line (# comments ok)")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--aq", action="store_true", help="open/resolve the aq row")
    a = ap.parse_args(argv)
    allow = set(ALLOW)
    if a.allow_file:
        with open(a.allow_file) as fh:
            allow |= {ln.split("#")[0].strip() for ln in fh} - {""}
    try:
        live, retired, findings = lint(a.roots or ROOTS, a.config, a.models_yaml, allow)
    except (OSError, RuntimeError) as e:
        print(f"model-ref-lint: cannot run: {e}", file=sys.stderr)
        return 2
    if a.json:
        print(json.dumps({"live": len(live), "retired": sorted(retired),
                          "findings": findings}, indent=1))
    else:
        for f in findings:
            print(f"{f['kind']:8} {f['model']:28} {f['file']}:{f['line']}")
        print(f"model-ref-lint: {len(findings)} finding(s); live={len(live)} "
              f"retired_known={len(retired)}")
    if a.aq:
        aq_sync(findings)
    return 1 if findings else 0


if __name__ == "__main__":
    sys.exit(main())
