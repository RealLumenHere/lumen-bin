#!/usr/bin/env python3
"""lint-handoff.py - structural linter for /home/lumen/ORCHESTRATOR-HANDOFF.md

wq#4314 (follow-up of wq#4295/wq#4297).  Stdlib + PyYAML only, read-only, no network, no LLM.

WHY THIS EXISTS: wq#4295 evaluated RuleBridge and Rule Coherence Graph and concluded DO NOT
ADOPT (correct: neither parses our file format, RCG needs Neo4j + a per-pair judge).  That
eval also measured that a bag-of-words polarity pass is unusable on real prose.  So this tool
does NOT lead with prose heuristics.  Its checks, in order of trust:

  stale-path        absolute path / .secrets file / bin tool named in the doc does not exist
  alias-*           LiteLLM alias table in the doc disagrees with the live litellm_config.yaml
  container-*       container list/count in the doc disagrees with `docker ps`
  wq-subcommand/flag  a `wq ...` command line cites a subcommand/flag `wq --help` does not have
  duplicate         same / near-same normalized line in two places (info the author must reconcile)
  contradiction-candidate   always-X vs never-X on near-identical object tokens (LOW trust, warn only)

Only the first five can say "this is wrong" from evidence.  The last two are hints.

Exit: 0 clean, 1 issues (error|warn not on the allowlist), 2 tool error.
Allowlist: one `kind:fingerprint  # reason` per line (default <doc>.lint-allow).  Every finding
prints its id, so a reviewed false positive is suppressed with a reason, never silently.
"""
import argparse, hashlib, json, os, re, shutil, subprocess, sys, tempfile
from itertools import combinations

DEFAULT_DOC = "/home/lumen/ORCHESTRATOR-HANDOFF.md"
LUMEN_HOME = "/home/lumen"
LITELLM_CFG = "/home/lumen/agent/litellm_config.yaml"
SECRETS_DIR = "/home/lumen/.secrets"
BIN_DIR = "/home/lumen/bin"

HEADING = re.compile(r"^(#{2,3})\s+(.*\S)\s*$")
HOST_RE = re.compile(r"^\s*On\s+([A-Za-z][\w-]*)\s*:")
PATH_RE = re.compile(r"(?<![\w.~/-])(/(?:home|mnt|etc|data|opt|var|usr|srv|tmp)(?:/[^\s`'\"<>()|,;]*)?)")
SECRET_RE = re.compile(r"\.secrets/([\w./-]+)")
BARE_SECRET_RE = re.compile(r"(?<![\w/.-])([\w-]+\.(?:token|key))\b")
STOP = set("""a an the and or but of to in on at for from with without by is are was were be been being it its this that
these those as if then than so not no do does did done have has had will would can could should may might you your we our
they them their he she his her any all each every both more most other such only own same too very just also into over
under again further once here there when where why how what which who whom about above after before below between during
through up down out off per via etc use used using must never always mandatory required don't dont""".split())
MARKERS = {"never", "always", "must", "mandatory", "required", "not", "dont", "don't", "forbidden", "off-limits"}
NEG_RE = re.compile(r"\b(never|do not|don't|dont|must not|mustn't|off-limits|forbidden)\b", re.I)
POS_RE = re.compile(r"\b(always|must(?! not)|mandatory)\b", re.I)


class Finding:
    def __init__(self, kind, sev, line, msg, key):
        self.kind, self.sev, self.line, self.msg = kind, sev, line, msg
        self.fp = hashlib.sha1((kind + "|" + key).encode()).hexdigest()[:10]

    @property
    def id(self):
        return "%s:%s" % (self.kind, self.fp)

    def d(self):
        return {"id": self.id, "kind": self.kind, "severity": self.sev, "line": self.line, "message": self.msg}


def parse(path):
    with open(path, encoding="utf-8") as f:
        lines = f.read().splitlines()
    secs = [{"title": "(preamble)", "level": 0, "start": 1, "lines": []}]
    cur = secs[0]
    for i, ln in enumerate(lines, 1):
        m = HEADING.match(ln)
        if m:
            cur = {"title": m.group(2), "level": len(m.group(1)) - 1, "start": i, "lines": []}
            secs.append(cur)
        else:
            cur["lines"].append((i, ln))
    return lines, secs


def line_hosts(secs):
    """yield (lineno, text, host, section_title). 'On X:' with X != Lumen scopes that one line to X."""
    for s in secs:
        persistent = "lumen"
        for no, ln in s["lines"]:
            m = HOST_RE.match(ln)
            host = persistent
            if m:
                h = m.group(1).lower()
                if h == "lumen":
                    persistent = "lumen"
                host = h
            yield no, ln, host, s["title"]


def clean_path(p):
    p = p.rstrip(".,;:!?)]}\u2014-")
    return p


def check_paths(secs, findings, info):
    seen = set()
    for no, ln, host, _t in line_hosts(secs):
        if host != "lumen":
            info["remote_unchecked"] = info.get("remote_unchecked", 0) + 1
            continue
        for raw in PATH_RE.findall(ln):
            p = clean_path(raw)
            if not p or p.count("/") < 2 or any(c in p for c in "*{}<>$") or p.startswith("/path/") or "NAME" in p:
                continue
            if (p, no) in seen:
                continue
            seen.add((p, no))
            info["paths_checked"] = info.get("paths_checked", 0) + 1
            if not os.path.exists(p):
                findings.append(Finding("stale-path", "error", no, "path does not exist: %s" % p, p))
        if ".secrets/" in ln:
            names = set(SECRET_RE.findall(ln)) | set(BARE_SECRET_RE.findall(ln))
            for n in sorted(names):
                n = n.rstrip(".,;:")
                info["secrets_checked"] = info.get("secrets_checked", 0) + 1
                if not os.path.exists(os.path.join(SECRETS_DIR, n)):  # existence only, never read
                    findings.append(Finding("stale-path", "error", no, "secret file name not present: .secrets/%s" % n, "secret:" + n))
        m = re.search(r"/home/lumen/bin/\s*\S*\s*.*?\(([^)]*)\)", ln)
        if m:
            for t in [x.strip() for x in m.group(1).split(",")]:
                if not t or t.lower().startswith("etc"):
                    continue
                t = t.split()[0]
                info["tools_checked"] = info.get("tools_checked", 0) + 1
                if not os.path.exists(os.path.join(BIN_DIR, t)):
                    findings.append(Finding("stale-path", "error", no, "bin tool listed but missing: %s/%s" % (BIN_DIR, t), "tool:" + t))


def norm(text):
    t = re.sub(r"^\s*(?:[-*]|\d+[.)])\s+", "", text)
    t = re.sub(r"[`*_]", "", t).lower()
    t = re.sub(r"[^a-z0-9 /._:-]", " ", t)
    return re.sub(r"\s+", " ", t).strip()


def toks(text):
    out = set()
    for w in re.findall(r"[a-z][a-z0-9_./-]{2,}", text.lower()):
        if w in STOP or w in MARKERS:
            continue
        for suf in ("ing", "ed", "es", "s"):
            if w.endswith(suf) and len(w) - len(suf) >= 4:
                w = w[: -len(suf)]
                break
        out.add(w)
    return out


def check_duplicates(secs, findings):
    items = []
    for s in secs:
        for no, ln in s["lines"]:
            n = norm(ln)
            if len(n.split()) >= 6 and not ln.lstrip().startswith("#"):
                items.append((no, ln, n, toks(n), s["title"]))
    done = set()
    for (a, b) in combinations(items, 2):
        if a[0] == b[0]:
            continue
        if a[2] == b[2]:
            kind_msg = "identical"
        else:
            ta, tb = a[3], b[3]
            if len(ta) < 5 or len(tb) < 5:
                continue
            j = len(ta & tb) / len(ta | tb)
            if j < 0.8:
                continue
            kind_msg = "near-identical (jaccard %.2f)" % j
        key = "|".join(sorted([a[2], b[2]]))
        if key in done:
            continue
        done.add(key)
        findings.append(Finding("duplicate", "warn", a[0],
                                "%s lines %d (%s) and %d (%s): %s" % (kind_msg, a[0], a[4][:28], b[0], b[4][:28], a[1].strip()[:70]), key))


def check_contradictions(secs, findings):
    ds = []
    for s in secs:
        for no, ln in s["lines"]:
            neg, pos = bool(NEG_RE.search(ln)), bool(POS_RE.search(ln))
            if neg == pos:
                continue
            t = toks(norm(ln))
            if len(t) >= 3:
                ds.append((no, ln, "neg" if neg else "pos", t))
    for a, b in combinations(ds, 2):
        if a[2] == b[2]:
            continue
        shared = a[3] & b[3]
        j = len(shared) / len(a[3] | b[3])
        if len(shared) >= 3 and j >= 0.5:
            key = "|".join(sorted([norm(a[1]), norm(b[1])]))
            findings.append(Finding("contradiction-candidate", "warn", a[0],
                                    "line %d (%s) vs line %d (%s) share %s: '%s' <> '%s'" % (
                                        a[0], a[2], b[0], b[2], sorted(shared)[:5], a[1].strip()[:55], b[1].strip()[:55]), key))


def load_cfg():
    import yaml
    with open(LITELLM_CFG) as f:
        return yaml.safe_load(f)


def check_aliases(secs, findings, info):
    if not os.path.exists(LITELLM_CFG):
        info["live_skipped"] = info.get("live_skipped", []) + ["litellm cfg absent"]
        return
    cfg = load_cfg()
    models = {m.get("model_name"): m for m in cfg.get("model_list", [])}
    alias_map = (cfg.get("router_settings") or {}).get("model_group_alias") or {}
    upstream = set()
    for m in models.values():
        mm = str((m.get("litellm_params") or {}).get("model", ""))
        upstream.add(mm)
        upstream.add(mm.split("/", 1)[-1])
    known = set(models) | set(alias_map.values()) | upstream
    rx = re.compile(r"^\s+(lumen/[\w.-]+)\s*->\s*(.+)$")
    for no, ln, host, _t in line_hosts(secs):
        m = rx.match(ln)
        if not m:
            continue
        alias, target = m.group(1), m.group(2)
        t = re.split(r"[\s,(]", target.strip())[0].strip(".,;:")
        info["aliases_checked"] = info.get("aliases_checked", 0) + 1
        if alias in alias_map:
            v = alias_map[alias]
            if t != v and t in known:
                findings.append(Finding("alias-target-mismatch", "error", no,
                                        "doc: %s -> %s ; live config model_group_alias: %s -> %s" % (alias, t, alias, v), alias + ">" + t))
        elif alias in models:
            blob = json.dumps(models[alias], default=str)
            if t.lower() not in blob.lower():
                findings.append(Finding("alias-target-unverified", "info", no,
                                        "%s is a model_name; target '%s' not found in its litellm_params" % (alias, t), alias + ">" + t))
        else:
            findings.append(Finding("alias-missing", "error", no, "%s is in the doc but not in live config (model_name or model_group_alias)" % alias, alias))


def check_containers(secs, findings, info):
    if not shutil.which("docker"):
        info["live_skipped"] = info.get("live_skipped", []) + ["docker absent"]
        return
    try:
        out = subprocess.run(["docker", "ps", "--format", "{{.Names}}"], capture_output=True, text=True, timeout=20)
        if out.returncode != 0:
            info["live_skipped"] = info.get("live_skipped", []) + ["docker ps rc=%d" % out.returncode]
            return
    except Exception as e:
        info["live_skipped"] = info.get("live_skipped", []) + ["docker: %s" % e]
        return
    live = set(x for x in out.stdout.split() if x)
    for s in secs:
        for idx, (no, ln) in enumerate(s["lines"]):
            m = re.match(r"^Containers?\s*\((\d+)\s+total", ln.strip()) or (re.match(r"^Containers?\s*\((\d+)\s+total", s["title"]) if idx == 0 else None)
            if not m and not re.match(r"^Containers?\s*\(", s["title"]):
                continue
            n = int(re.match(r"^Containers?\s*\((\d+)", s["title"]).group(1))
            lst = next((x for x in s["lines"] if x[1].strip()), None)
            if not lst:
                continue
            doc = set(x.strip() for x in lst[1].split(",") if x.strip())
            info["containers_checked"] = len(doc)
            if n != len(live):
                findings.append(Finding("container-count", "error", s["start"], "doc says %d containers, docker ps shows %d" % (n, len(live)), "count"))
            if doc - live:
                findings.append(Finding("container-missing", "error", lst[0], "listed in doc but not running: %s" % sorted(doc - live), "m" + ",".join(sorted(doc - live))))
            if live - doc:
                findings.append(Finding("container-unlisted", "error", lst[0], "running but not in doc list: %s" % sorted(live - doc), "u" + ",".join(sorted(live - doc))))
            return


def check_wq(secs, findings, info):
    wq = shutil.which("wq") or os.path.join(BIN_DIR, "wq")
    if not os.path.exists(wq):
        info["live_skipped"] = info.get("live_skipped", []) + ["wq absent"]
        return
    cache = {}

    def helptext(sub=None):
        k = sub or ""
        if k not in cache:
            try:
                r = subprocess.run([wq] + ([sub] if sub else []) + ["--help"], capture_output=True, text=True, timeout=20)
                cache[k] = r.stdout + r.stderr if r.returncode == 0 else None
            except Exception:
                cache[k] = None
        return cache[k]

    top = helptext()
    subs = set(re.findall(r"\{([a-z0-9,-]+)\}", top or "")[0].split(",")) if top and re.search(r"\{([a-z0-9,-]+)\}", top) else set()
    cmdpos = re.compile(r"(?:^|[|;'\"]|\$\(|/bin/)\s*wq\s+([a-z][\w-]*)([^|;'\"\n]*)")
    for no, ln, host, _t in line_hosts(secs):
        for m in cmdpos.finditer(ln):
            sub, rest = m.group(1), m.group(2)
            if not subs:
                continue
            info["wq_cmds_checked"] = info.get("wq_cmds_checked", 0) + 1
            if sub not in subs:
                if re.match(r"^\s*(?:wq|.*[|;'\"]\s*wq)\s", ln):
                    findings.append(Finding("wq-subcommand-missing", "error", no, "`wq %s` is not a wq subcommand" % sub, "sub:" + sub))
                continue
            ht = helptext(sub)
            if ht is None:
                continue
            for fl in re.findall(r"(--[a-z][\w-]*)", rest):
                if fl not in ht:
                    findings.append(Finding("wq-flag-missing", "error", no, "`wq %s` has no flag %s" % (sub, fl), "flag:%s:%s" % (sub, fl)))


def load_allow(path):
    allowed = {}
    if path and os.path.exists(path):
        for ln in open(path, encoding="utf-8"):
            ln = ln.strip()
            if not ln or ln.startswith("#"):
                continue
            ident, _, why = ln.partition("#")
            allowed[ident.strip()] = why.strip()
    return allowed


def run(doc, allow_path, live):
    lines, secs = parse(doc)
    findings, info = [], {"lines": len(lines), "sections": len(secs) - 1}
    check_paths(secs, findings, info)
    check_duplicates(secs, findings)
    check_contradictions(secs, findings)
    if live:
        for fn in (check_aliases, check_containers, check_wq):
            try:
                fn(secs, findings, info)
            except Exception as e:
                info["live_skipped"] = info.get("live_skipped", []) + ["%s: %s" % (fn.__name__, e)]
    allow = load_allow(allow_path)
    kept, supp = [], []
    for f in findings:
        (supp if f.id in allow else kept).append(f)
    return kept, supp, info


CLEAN = "# T\n\n## 1. A\nUse /etc/hostname for the host name.\n\n## 2. B\nSomething unrelated lives here entirely.\n"
DIRTY = ("# T\n\n## 1. A\nAlways restart the service after every single config change.\n"
         "Read /home/lumen/definitely/not/here/file.txt before you start work.\n"
         "Back up every container database before touching the live config files.\n\n"
         "## 2. B\nNever restart the service after every single config change.\n"
         "Back up every container database before touching the live config files.\n")


def selfcheck():
    ok = True
    with tempfile.TemporaryDirectory() as td:
        c, d = os.path.join(td, "clean.md"), os.path.join(td, "dirty.md")
        open(c, "w").write(CLEAN)
        open(d, "w").write(DIRTY)
        kc, _, _ = run(c, None, False)
        kd, _, _ = run(d, None, False)
        kinds = set(f.kind for f in kd)
        print("selfcheck clean fixture: %d findings (expect 0) -> %s" % (len(kc), "PASS" if not kc else "FAIL"))
        ok &= not kc
        for need in ("stale-path", "duplicate", "contradiction-candidate"):
            good = need in kinds
            print("selfcheck dirty fixture detects %-24s -> %s" % (need, "PASS" if good else "FAIL"))
            ok &= good
    return 0 if ok else 1


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("doc", nargs="?", default=DEFAULT_DOC)
    ap.add_argument("--allow", default=None, help="allowlist file (default: <doc without .md>.lint-allow)")
    ap.add_argument("--no-live", action="store_true", help="skip docker/wq/litellm-config probes")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("-v", "--verbose", action="store_true", help="also print info-level findings and suppressed ids")
    ap.add_argument("--selfcheck", action="store_true", help="prove checks can both pass and fail on fixtures")
    a = ap.parse_args()
    if a.selfcheck:
        return selfcheck()
    if not os.path.exists(a.doc):
        print("lint-handoff: no such file: %s" % a.doc, file=sys.stderr)
        return 2
    allow_path = a.allow or (os.path.splitext(a.doc)[0] + ".lint-allow")
    try:
        kept, supp, info = run(a.doc, allow_path, not a.no_live)
    except Exception as e:
        print("lint-handoff: tool error: %s" % e, file=sys.stderr)
        return 2
    issues = [f for f in kept if f.sev in ("error", "warn")]
    if a.json:
        print(json.dumps({"doc": a.doc, "info": info, "issues": [f.d() for f in issues],
                          "info_findings": [f.d() for f in kept if f.sev == "info"], "suppressed": [f.id for f in supp]}, indent=2))
    else:
        print("lint-handoff: %s (%d lines, %d sections) checked: %s" % (
            a.doc, info["lines"], info["sections"],
            ", ".join("%s=%s" % (k, v) for k, v in sorted(info.items()) if k not in ("lines", "sections", "live_skipped"))))
        for s in info.get("live_skipped", []):
            print("  [skipped live probe] %s" % s)
        for f in sorted(issues, key=lambda x: (x.kind, x.line)):
            print("  %-5s L%-4d %-24s %s  [id=%s]" % (f.sev.upper(), f.line, f.kind, f.msg, f.id))
        if a.verbose:
            for f in kept:
                if f.sev == "info":
                    print("  INFO  L%-4d %-24s %s  [id=%s]" % (f.line, f.kind, f.msg, f.id))
            for f in supp:
                print("  SUPPRESSED %s" % f.id)
        by = {}
        for f in issues:
            by[f.kind] = by.get(f.kind, 0) + 1
        print("RESULT: %s  (%d issue(s)%s; %d suppressed by allowlist)" % (
            "CLEAN" if not issues else "ISSUES", len(issues), (" - " + ", ".join("%s=%d" % kv for kv in sorted(by.items()))) if by else "", len(supp)))
    return 1 if issues else 0


if __name__ == "__main__":
    sys.exit(main())
