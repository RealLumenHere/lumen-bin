"""wq#3466: unit-drift --digest must publish under one fixed title (one aq row, not one per change count)
and resolve its open aq rows once the check is CLEAN."""
import importlib.machinery, importlib.util, os, subprocess
import pytest

PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "unit-drift")

# `aq list open` lines in the live format (aq cmd_list prints key[:40]); ids 1062/1131 are the
# count-keyed rows the ticket named, 1152/2671113 are other lumen-alerts rows that must stay open.
AQ_OPEN = """\
[2671113] open     x1   ntfy:lumen-alerts Failed unit: issue-log-hygiene.service   owner=unassigned   2026-10-07T07:41:53Z  Failed unit: issue-log-hygiene.service
[1152] open     x20  ntfy:lumen-alerts [DISPATCH] nightly gate FAILED           owner=unassigned   2026-10-07T05:00:03Z  [DISPATCH] nightly gate FAILED
[1062] open     x383 ntfy:lumen-alerts unit-drift: 38 change(s) on lumen        owner=unassigned   2026-10-01T02:11:50Z  unit-drift: 38 change(s) on lumen
[1131] open     x338 ntfy:lumen-alerts unit-drift: 6 change(s) on lumen         owner=unassigned   2026-09-20T03:26:31Z  unit-drift: 6 change(s) on lumen
[3001] open     x4   ntfy:lumen-alerts unit-drift: drift on lumen               owner=unassigned   2026-10-07T08:00:00Z  unit-drift: drift on lumen
[ 777] open     x2   other-source      unit-drift: not ours                     owner=unassigned   2026-10-07T08:00:00Z  unit-drift: not ours
"""

def load(sysdir):
    os.environ["UNIT_DRIFT_SYS"] = str(sysdir)
    loader = importlib.machinery.SourceFileLoader("unit_drift_under_test", PATH)
    spec = importlib.util.spec_from_loader(loader.name, loader); m = importlib.util.module_from_spec(spec); loader.exec_module(m)
    return m

@pytest.fixture
def m(tmp_path, monkeypatch):
    (tmp_path / "a.service").write_text("[Unit]\n")
    mod = load(tmp_path)
    calls = []
    def fake(args, **kw):
        calls.append(list(args))
        out = AQ_OPEN if args[1:3] == ["list", "open"] else ""
        return subprocess.CompletedProcess(args, 0, stdout=out, stderr="")
    monkeypatch.setattr(mod.subprocess, "run", fake)
    monkeypatch.setattr(mod, "dangling_target_lint", lambda: [])
    monkeypatch.setattr(mod, "guard_lint", lambda: [])
    monkeypatch.setattr(mod, "unexplained_masks", lambda units: [])
    mod.calls = calls
    return mod

def baseline(m, units):
    return {"units": units, "dropins": {}, "enable": {}}

def test_digest_title_is_count_free(m, monkeypatch):
    titles = []
    for n in (1, 3):
        units = {"x%d.service" % i: "h" for i in range(n)}
        monkeypatch.setattr(m, "load_manifest", lambda u=units: baseline(m, u))
        assert m.cmd_check(digest=True) == 1
        ntfy = [c for c in m.calls if c[0] == m.NTFY][-1]
        titles.append(ntfy[ntfy.index("-T") + 1])
        assert ntfy[-1].startswith("%d change(s): " % (n + 1))   # count moved into the body
    assert titles == ["unit-drift: drift on lumen"] * 2

def test_clean_digest_resolves_only_unit_drift_rows(m, monkeypatch):
    cur = m.scan()
    monkeypatch.setattr(m, "load_manifest", lambda: {"units": cur["units"], "dropins": cur["dropins"], "enable": cur["enable"]})
    assert m.cmd_check(digest=True) == 0
    resolved = sorted(c[2] for c in m.calls if c[0] == m.AQ and c[1] == "resolve")
    assert resolved == ["1062", "1131", "3001"]

def test_clean_without_digest_touches_no_aq(m, monkeypatch):
    cur = m.scan()
    monkeypatch.setattr(m, "load_manifest", lambda: {"units": cur["units"], "dropins": cur["dropins"], "enable": cur["enable"]})
    assert m.cmd_check(digest=False) == 0
    assert not [c for c in m.calls if c[0] == m.AQ]

def test_unreadable_aq_does_not_crash_clean_check(m, monkeypatch):
    cur = m.scan()
    monkeypatch.setattr(m, "load_manifest", lambda: {"units": cur["units"], "dropins": cur["dropins"], "enable": cur["enable"]})
    monkeypatch.setattr(m.subprocess, "run", lambda *a, **k: (_ for _ in ()).throw(subprocess.TimeoutExpired("aq", 60)))
    assert m.cmd_check(digest=True) == 0
