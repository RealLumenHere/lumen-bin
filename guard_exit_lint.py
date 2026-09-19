#!/usr/bin/env python3
"""guard_exit_lint (wq#1657) — lint fleet units for the guard exit-code contract.

Contract (ops/wq1657-guard-exit-convention/DESIGN.md): a guard either always
exits 0 and reports via ntfy+aq (Mode A, preferred), or exits 1 on a finding
AND declares both `X-Lumen-Guard=finding-exit` and `OnFailure=` in its unit
file or a drop-in (Mode B), so "guard fired" is distinguishable from "guard
crashed" in systemctl --failed / handoff verify / lumen-failed-units.

Rules:
  GEL1  X-Lumen-Guard=finding-exit declared but no OnFailure= -> the finding
        would be silent everywhere except `systemctl --failed`.
  GEL2  known finding-exit guard (KNOWN_FINDING_EXIT, atlas §3) without the
        tag -> fired/crashed conflation persists for it.

Used by `unit-drift check` (fail-open import) and standalone:
  guard_exit_lint.py <unit-file>...    exit 1 if violations
"""
import os, re, sys

# The 10 guards that exit 1 on a finding today without declaring it
# (FLEET_SERVICE_ATLAS.md §3 / §5 #1, wq#1627). Remove entries as they are
# tagged (Mode B) or migrated to exit-0 (Mode A).
KNOWN_FINDING_EXIT = {
    "unit-drift.service",
    "wq-drift-guard.service",
    "primary-tree-guard.service",
    "lumen-repo-sweep.service",
    "lumen-pat-scan.service",
    "lumen-dispatch-lint.service",
    "lumen-retirement-audit.service",
    "lumen-budget-alert.service",
    "git-guard-sentinel.service",
    "repo-visibility-sentinel.service",
}

TAG_RE       = re.compile(r"(?im)^\s*X-Lumen-Guard\s*=\s*finding-exit\s*$")
ONFAILURE_RE = re.compile(r"(?im)^\s*OnFailure\s*=\s*\S")


def lint_unit(name, text):
    """Lint one unit. `text` = unit file content plus all drop-in content
    concatenated. Returns a list of violation strings (empty = clean)."""
    tagged = bool(TAG_RE.search(text))
    v = []
    if tagged and not ONFAILURE_RE.search(text):
        v.append("GEL1 %s: X-Lumen-Guard=finding-exit without OnFailure= "
                 "(finding would be silent)" % name)
    if name in KNOWN_FINDING_EXIT and not tagged:
        v.append("GEL2 %s: known finding-exit guard missing "
                 "X-Lumen-Guard=finding-exit tag" % name)
    return v


def lint_dir(sysdir, read=None):
    """Lint every *.service file in `sysdir`, honoring `<unit>.d/*.conf`
    drop-ins. `read` reads a path to bytes (injectable for the sudo fallback
    in unit-drift). Returns all violations sorted by unit name."""
    if read is None:
        read = lambda p: open(p, "rb").read()

    def text_of(path):
        try:
            return read(path).decode("utf-8", "replace")
        except Exception:
            return ""

    out = []
    for name in sorted(os.listdir(sysdir)):
        if not name.endswith(".service"):
            continue
        full = os.path.join(sysdir, name)
        if not os.path.isfile(full):        # skip symlinks (aliases) and dirs
            continue
        text = text_of(full)
        dropdir = os.path.join(sysdir, name + ".d")
        if os.path.isdir(dropdir):
            for conf in sorted(os.listdir(dropdir)):
                if conf.endswith(".conf"):
                    text += "\n" + text_of(os.path.join(dropdir, conf))
        out.extend(lint_unit(name, text))
    return out


def main(argv):
    if not argv:
        print(__doc__)
        return 2
    v = []
    for path in argv:
        if os.path.isdir(path):
            v.extend(lint_dir(path))
        else:
            v.extend(lint_unit(os.path.basename(path),
                               open(path, "rb").read().decode("utf-8", "replace")))
    for line in v:
        print(line)
    return 1 if v else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
