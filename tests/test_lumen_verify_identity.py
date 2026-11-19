"""wq#1688: identity probes confirm curated free text by stable tokens, never rewrite it."""
import importlib.machinery
import importlib.util
import json
import os
import shutil
import sqlite3
import sys

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
SRC = os.path.join(os.path.dirname(HERE), "lumen-verify")
LIVE_DB = "/home/lumen/minerva/minerva.db"


def _load():
    loader = importlib.machinery.SourceFileLoader("lumen_verify_under_test", SRC)
    spec = importlib.util.spec_from_loader(loader.name, loader)
    mod = importlib.util.module_from_spec(spec)
    loader.exec_module(mod)
    return mod


@pytest.fixture
def lv(monkeypatch):
    mod = _load()
    mod._SSH_TARGETS.clear()
    return mod


def _lsblk(*disks):
    return json.dumps({"blockdevices": [
        {"name": n, "size": sz, "type": t, "serial": s} for n, sz, t, s in disks]})


def _fact(pred, value, subject="lumen"):
    return {"id": "x", "subject": subject, "predicate": pred, "value": value}


def fake_run(mapping):
    """argv substring -> (rc, out, err)."""
    def run(argv, timeout=20):
        j = " ".join(argv)
        for needle, res in mapping.items():
            if needle in j:
                return res
        return 255, "", "ssh: connect to host timed out"
    return run


def test_storage_serial_survives_drive_rename_and_ignores_zram(lv, monkeypatch):
    stored = "Samsung 512G sn S5JYNS0RB07557V (was nvme4 on 07-16)"
    monkeypatch.setattr(lv, "run", fake_run({"NAME,SIZE,TYPE,SERIAL": (0, _lsblk(
        ("nvme5n1", 512110190592, "disk", "S5JYNS0RB07557V"),
        ("zram0", 8000000000, "disk", "")), "")}))
    out = lv.probe_node_storage("lumen", [_fact("storage", stored)])
    assert out[("lumen", "storage")] == (stored, None)


def test_storage_missing_serial_is_mismatch_never_a_value(lv, monkeypatch):
    monkeypatch.setattr(lv, "run", fake_run({"NAME,SIZE,TYPE,SERIAL": (0, _lsblk(
        ("nvme0n1", 1, "disk", "OTHER12345")), "")}))
    obs, err = lv.probe_node_storage("lumen", [_fact("storage", "x sn GONE99999 (boot)")])[
        ("lumen", "storage")]
    assert obs is None and err.startswith("MISMATCH") and "GONE99999" in err


def test_storage_layout_without_serial_is_unknown(lv, monkeypatch):
    monkeypatch.setattr(lv, "run", fake_run({}))
    obs, err = lv.probe_node_storage("lumen", [_fact("storage_layout", "one big disk")])[
        ("lumen", "storage_layout")]
    assert obs is None and "no disk serial" in err


def test_lsblk_size_sum_drops_zram(lv):
    assert lv._lsblk_bytes(_lsblk(("nvme0n1", 100, "disk", "A"), ("zram0", 50, "disk", ""),
                                  ("loop0", 7, "disk", ""))) == 100


def test_ram_runs_under_sudo_and_reports_denial(lv, monkeypatch):
    seen = []

    def run(argv, timeout=20):
        seen.append(argv)
        return 1, "", "sudo: a password is required"
    monkeypatch.setattr(lv, "run", run)
    obs, err = lv.probe_node_storage("lumen", [_fact("ram", "16GB")])[("lumen", "ram")]
    assert seen[0][:2] == ["sudo", "-n"] and obs is None and err == "sudo -n denied"


IDENT = "---SYS---\n{sys}\n---BOARD---\n{board}\n---LINK---\n{link}\n"
LINK = ("2: enp2s0: <BROADCAST,UP> mtu 1500 link/ether c0:25:a5:d0:66:d1 brd ff:ff:ff:ff:ff:ff\n"
        "3: wlo1: <BROADCAST> mtu 1500 link/ether c4:3d:1a:d7:04:a1 brd ff:ff:ff:ff:ff:ff")


def _ident(lv, monkeypatch, sys_="1XLN9R3", board="/1XLN9R3/CNWS20024C00CP/", link=LINK, rc=0):
    monkeypatch.setattr(lv, "run", fake_run({"system-serial-number": (
        rc, IDENT.format(sys=sys_, board=board, link=link), "")}))


def test_hardware_confirms_curated_text_unchanged(lv, monkeypatch):
    _ident(lv, monkeypatch)
    v = "Dell OptiPlex 3090 UFF, sn 1XLN9R3, board 05G6P6 A00; Frank was right"
    assert lv.probe_node_identity("lumen", [_fact("hardware", v)])[("lumen", "hardware")] == (v, None)


def test_hardware_serial_mismatch_is_unknown(lv, monkeypatch):
    _ident(lv, monkeypatch, sys_="NEWBOX123", board="NEWBOARD99")
    obs, err = lv.probe_node_identity("lumen", [_fact("hardware", "Dell, sn 1XLN9R3")])[
        ("lumen", "hardware")]
    assert obs is None and err.startswith("MISMATCH")


def test_hardware_sudo_denied_still_checks_nic(lv, monkeypatch):
    _ident(lv, monkeypatch, sys_="sudo: interactive authentication is required",
           board="sudo: interactive authentication is required")
    out = lv.probe_node_identity("lumen", [_fact("hardware", "N100, sn X12345"),
                                           _fact("nic", "enp2s0 c0:25:a5:d0:66:d1 fleet LAN")])
    assert out[("lumen", "hardware")] == (None, "sudo -n denied")
    assert out[("lumen", "nic")][1] is None


def test_nic_missing_mac_is_mismatch(lv, monkeypatch):
    _ident(lv, monkeypatch)
    obs, err = lv.probe_node_identity("lumen", [_fact("nic", "enp2s0 aa:bb:cc:dd:ee:ff")])[
        ("lumen", "nic")]
    assert obs is None and err.startswith("MISMATCH") and "aa:bb:cc:dd:ee:ff" in err


def test_offline_node_is_unknown_for_every_fact(lv, monkeypatch):
    monkeypatch.setattr(lv, "_ssh_target", lambda n: ("u@100.64.0.9", None))
    monkeypatch.setattr(lv, "run", fake_run({}))
    out = lv.probe_node_identity("khepri", [_fact("hardware", "sn A", "khepri"),
                                            _fact("nic", "aa:bb:cc:dd:ee:ff", "khepri")])
    assert all(o is None and e.startswith("ssh khepri") for o, e in out.values())


def test_roster_resolves_names_bare_ssh_cannot(lv):
    t, _ = lv._ssh_target("gameboy")
    assert t and "@" in t and not t.endswith("@gameboy")
    with pytest.raises(lv.NotReachable):
        lv._ssh_argv("mirror", ["true"])          # ssh method n/a
    assert lv._ssh_target("not-a-node-xyz") == ("not-a-node-xyz", None)


@pytest.mark.skipif(not os.path.exists(LIVE_DB), reason="needs the live store to copy")
def test_node_run_never_supersedes_on_a_copy_of_the_store(lv, monkeypatch, tmp_path):
    db = tmp_path / "m.db"
    src = sqlite3.connect(LIVE_DB)
    dst = sqlite3.connect(db)
    src.backup(dst)
    src.close()
    dst.close()
    reg = os.environ.get("TEST_REGISTRY", "/home/lumen/minerva/verify/registry.yaml")

    def rows(c):
        return {(r[0], r[1]): r[2] for r in c.execute(
            "SELECT subject, predicate, value FROM state_facts WHERE valid_to IS NULL "
            "AND subject='anubis'")}

    c = sqlite3.connect(db)
    before = rows(c)
    c.close()
    _ident(lv, monkeypatch)  # anubis serial + a MAC set that confirms nic
    stored_disk = before.get(("anubis", "storage"), "")
    serial = sorted(lv._stated_serials(stored_disk))[0] if lv._stated_serials(stored_disk) else "S"
    base = lv.run
    lsblk = _lsblk(("nvme9n1", 256060514304, "disk", serial), ("zram0", 1, "disk", ""))

    def run(argv, timeout=20):
        j = " ".join(argv)
        if "NAME,SIZE,TYPE" in j:
            return 0, lsblk, ""
        return base(argv, timeout)
    monkeypatch.setattr(lv, "run", run)
    monkeypatch.setattr(lv, "_ssh_target", lambda n: ("anubis@100.121.17.5", None))
    rc = lv.main(["node", "anubis", "--db", str(db), "--registry", reg, "--json"])
    assert rc == 0
    c = sqlite3.connect(db)
    after = rows(c)
    confirmed = c.execute("SELECT count(*) FROM fact_verifications WHERE subject='anubis' "
                          "AND outcome='confirmed' AND probe IN ('node_identity','node_storage')"
                          ).fetchone()[0]
    c.close()
    assert after == before            # nothing superseded, curated text intact
    assert confirmed >= 2             # hardware + nic (+ storage) confirmed


def test_ssh_argv_quotes_remote_script_as_one_arg(lv, monkeypatch):
    monkeypatch.setattr(lv, "_ssh_target", lambda n: ("u@100.64.0.9", None))
    argv = lv._ssh_argv("x", lv.IDENTITY_ARGV)
    import shlex
    assert argv[-2] == "--" and shlex.split(argv[-1]) == lv.IDENTITY_ARGV


def test_dmi_placeholders_are_not_serials(lv, monkeypatch):
    # Trigkey: real system serial, placeholder board serial
    _ident(lv, monkeypatch, sys_="BN1003LG20791", board="Default string")
    v = "Trigkey Green G4, sn BN1003LG20791, BIOS ADLNV104"
    assert lv.probe_node_identity("lumen", [_fact("hardware", v)])[("lumen", "hardware")] == (v, None)
    _ident(lv, monkeypatch, sys_="Default string", board="To Be Filled By O.E.M.")
    obs, err = lv.probe_node_identity("lumen", [_fact("hardware", v)])[("lumen", "hardware")]
    assert obs is None and err == "no DMI serial readable"


def test_apple_multi_line_board_serials(lv, monkeypatch):
    _ident(lv, monkeypatch, sys_="F5KR806KF9VN", board="F5K546600EZFWC21P\nF5K607300F8FD6P1S")
    v = "Apple MacPro6,1, sn F5KR806KF9VN, BootROM 474"
    assert lv.probe_node_identity("lumen", [_fact("hardware", v)])[("lumen", "hardware")] == (v, None)


def test_slash_joined_raid_serials_each_checked(lv, monkeypatch):
    assert lv._stated_serials("md0 sn AAAAA111/BBBBB222/CCCCC333 raid5") == {
        "AAAAA111", "BBBBB222", "CCCCC333"}
    monkeypatch.setattr(lv, "run", fake_run({"NAME,SIZE,TYPE,SERIAL": (0, _lsblk(
        ("sda", 1, "disk", "AAAAA111"), ("sdb", 1, "disk", "BBBBB222"),
        ("sdc", 1, "disk", "CCCCC333")), "")}))
    v = "md0 sn AAAAA111/BBBBB222/CCCCC333 raid5"
    assert lv.probe_node_storage("lumen", [_fact("storage", v)])[("lumen", "storage")] == (v, None)


def test_wq5026_na_writes_no_row_and_closed_subject_skips_every_probe(lv, monkeypatch):
    """wq#5026: an inapplicable value is `n/a` (no fact_verifications row, so not BLOCKED);
    a retired subject is skipped for every probe except node_liveness."""
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("CREATE TABLE fact_verifications (id, fact_id, subject, predicate, probe,"
                 " observed, outcome, ts, detail)")
    monkeypatch.setattr(lv, "migrate", lambda c: None)
    monkeypatch.setattr(lv, "closed_subjects", lambda c: {"p3": "retired"})
    called = []
    monkeypatch.setitem(lv.PROBES, "plug_map", lambda s, f: called.append(s) or {})
    monkeypatch.setitem(lv.PROBES, "node_liveness",
                        lambda s, f: {(s, x["predicate"]): ("offline", None) for x in f})
    batches = {("service_state", "lumen"): [_fact("status", "LIVE")],
               ("plug_map", "p3"): [_fact("powers_node", "hathor", "p3")],
               ("node_liveness", "p3"): [_fact("availability", "offline", "p3")]}
    r = lv.sweep(conn, None, dry_run=True, batches=batches)
    assert r["counts"]["n/a"] == 1 and r["counts"]["skipped"] == 1
    assert r["counts"]["unknown"] == 0 and r["counts"]["confirmed"] == 1
    assert called == []
    assert conn.execute("SELECT COUNT(*) FROM fact_verifications").fetchone()[0] == 0
