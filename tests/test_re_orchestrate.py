"""re-fingerprint / re-orchestrate / re-exec-runner (wq#5200). Synthetic fixtures only: no ssh, no
runner, no live registry, no lumen_state. Covers format + nested-format detection, vendor hints,
tool selection (format/arch matching, specificity ranking, member follow-ups, fallbacks), the
permission gate (read/write/hw_write/network), envelope validation and the runner's refusals."""
import gzip
import importlib.machinery
import importlib.util
import io
import json
import os
import struct
import subprocess
import sys

import pytest

BIN = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def load(name, mod):
    loader = importlib.machinery.SourceFileLoader(mod, os.path.join(BIN, name))
    spec = importlib.util.spec_from_loader(mod, loader)
    m = importlib.util.module_from_spec(spec)
    loader.exec_module(m)
    return m


fpm = load("re-fingerprint", "re_fingerprint_t")
orc = load("re-orchestrate", "re_orchestrate_t")


# ------------------------------------------------------------------ fixtures
def elf_header(machine=40, bits=32, etype=2):
    cls = 2 if bits == 64 else 1
    h = b"\x7fELF" + bytes([cls, 1, 1, 0]) + b"\0" * 8 + struct.pack("<HHI", etype, machine, 1)
    return h + b"\0" * (64 - len(h))


def fdt(compatible=b"rockchip,rk3399-evb\0rockchip,rk3399\0", model=b"Rockchip RK3399 EVB\0"):
    strings = b"model\0compatible\0"
    def prop(nameoff, val):
        pad = (4 - len(val) % 4) % 4
        return struct.pack(">III", 3, len(val), nameoff) + val + b"\0" * pad
    struct_blk = struct.pack(">I", 1) + b"\0\0\0\0" + prop(0, model) + prop(6, compatible) + struct.pack(">II", 2, 9)
    off_struct = 40
    off_strings = off_struct + len(struct_blk)
    total = off_strings + len(strings)
    hdr = struct.pack(">IIIIIIIIII", 0xd00dfeed, total, off_struct, off_strings, 40, 17, 16, 0, len(strings), len(struct_blk))
    return hdr + struct_blk + strings


def squashfs_le():
    sb = bytearray(96)
    sb[0:4] = b"hsqs"
    struct.pack_into("<H", sb, 28, 4)  # s_major
    return bytes(sb)


@pytest.fixture
def mkfile(tmp_path):
    def _mk(name, data):
        p = tmp_path / name
        p.write_bytes(data)
        return str(p)
    return _mk


def fp(path):
    return fpm.fingerprint(path, use_binwalk=False)


# ------------------------------------------------------------------ fingerprinter
def test_elf_arm(mkfile):
    r = fp(mkfile("a.elf", elf_header(40) + b"\0" * 4096))
    assert r["format"] == "ELF" and r["arch"] == "arm"
    assert "arch:non_x86" in r["conditions"]


def test_go_marker_condition(mkfile):
    r = fp(mkfile("g.elf", elf_header(62, 64) + b"\0" * 100 + b"runtime.main" + b"\0" * 100))
    assert "go_buildinfo_section_or_pclntab" in r["conditions"]


def test_rockchip_krnl_wrapping_gzip_cpio(mkfile):
    cpio = b"070701" + b"0" * 104 + b"init\0" + b"\0" * 2000
    gz = gzip.compress(cpio, mtime=0)
    blob = b"KRNL" + struct.pack("<I", len(gz)) + gz + b"\0" * 1024
    r = fp(mkfile("boot.img", blob))
    assert r["format"] == "firmware" and r["subformat"] == "rockchip_krnl"
    assert r["vendor_hints"][0]["vendor"] == "rockchip"
    gzhit = next(h for h in r["embedded_formats"] if h["format"] == "gzip")
    assert any(c["format"] == "cpio" for c in gzhit["children"]), "nested cpio inside gzip not found"


def test_composite_firmware_with_squashfs_and_dtb(mkfile):
    blob = b"\xff" * 4096 + fdt() + b"\xff" * 1000 + squashfs_le() + b"\0" * 4096
    r = fp(mkfile("dump.raw", blob))
    assert r["format"] == "firmware"
    assert "embedded_filesystem" in r["conditions"]
    assert r["device_trees"][0]["model"] == "Rockchip RK3399 EVB"
    rk = next(v for v in r["vendor_hints"] if v["vendor"] == "rockchip")
    assert "rk3399-evb" in rk["socs"] and rk["confidence"] >= 0.9


def test_apk_frameworks(mkfile, tmp_path):
    import zipfile
    p = tmp_path / "f.apk"
    with zipfile.ZipFile(p, "w") as z:
        z.writestr("AndroidManifest.xml", b"\0" * 10)
        z.writestr("classes.dex", b"dex\n035\0")
        z.writestr("lib/arm64-v8a/libapp.so", elf_header(183, 64))
        z.writestr("lib/arm64-v8a/libflutter.so", elf_header(183, 64))
    r = fp(str(p))
    assert r["format"] == "APK" and r["arch"] == "aarch64"
    assert "flutter_libapp" in r["conditions"] and "native_libs" in r["conditions"]
    assert not any(h["format"] == "zip" for h in r["embedded_formats"])


def test_memory_dump_lime(mkfile):
    assert fp(mkfile("m.lime", b"EMiL" + b"\0" * 4096))["format"] == "memory_dump"


def test_unknown(mkfile):
    r = fp(mkfile("x.dat", b"hello world, nothing here " * 50))
    assert r["format"] == "unknown"


def test_ext4_validator_rejects_noise(mkfile):
    blob = bytearray(b"\0" * 8192)
    blob[0x438:0x43A] = b"\x53\xef"  # magic alone, superblock garbage
    r = fp(mkfile("n.bin", bytes(blob)))
    assert not any(h["format"] == "ext2_3_4" for h in r["embedded_formats"])


# ------------------------------------------------------------------ selection
REG = {
    "version": "test",
    "routing": {"default_chain": ["rizin", "ghidra"], "rules": [
        {"format": "ELF", "chain": ["rizin", "ghidra", "capa"], "augments": [
            {"condition": "arch:non_x86", "add": ["qiling"], "position": "after"}]},
        {"format": "APK", "chain": ["apktool", "jadx"], "augments": [
            {"condition": "flutter_libapp", "add": ["blutter"], "position": "after"},
            {"condition": "native_libs", "add": ["ghidra"], "position": "after"}]},
        {"format": "firmware", "chain": ["unblob", "binwalk"], "augments": []}]},
    "output_envelope": {"required": ["target", "tool", "tool_version", "analysis_id", "started_at", "status"],
                        "properties": {"status": {"enum": ["ok", "partial", "error", "timeout", "killed"]}}},
    "orchestration": None,
    "tools": {},
}


def T(stage, cls, formats, archs, inv=True, hosts=("maksoor",)):
    return {"name": "t", "target_formats": formats, "architectures": archs, "hosts": {h: "/x" for h in hosts},
            "orchestration": {"stage": stage, "action_class": cls, "runner": "maksoor",
                              **({"invocation": {"argv": ["/x", "{target}"]}} if inv else {})}}


REG["tools"] = {
    "rizin": T("static", "read", ["ELF", "raw"], ["x86", "x86_64", "arm", "aarch64"]),
    "ghidra": T("static", "read", ["ELF", "PE", "raw"], ["x86", "x86_64", "arm", "aarch64"]),
    "capa": T("capability", "read", ["ELF", "PE"], ["x86", "x86_64"]),
    "qiling": T("dynamic", "write", ["ELF"], ["arm", "aarch64", "x86_64"], inv=False),
    "apktool": T("extract", "read", ["APK"], ["dalvik"]),
    "jadx": T("static", "read", ["APK", "DEX"], ["dalvik"]),
    "blutter": T("runtime_recovery", "read", ["APK", "ELF"], ["aarch64"]),
    "unblob": T("extract", "read", ["firmware", "raw"], ["any"]),
    "binwalk": T("extract", "read", ["firmware", "raw"], ["any"]),
    "emba": T("firmware_analysis", "write", ["firmware"], ["any"], hosts=("lumen",)),
    "angr": T("symbolic", "read", ["ELF"], ["x86_64", "arm"], inv=False),
    "afl++": T("fuzzing", "write", ["ELF"], ["x86_64", "arm"], inv=False),
}


def plan(fpd, **kw):
    return orc.build_plan(fpd, REG, hardware=[], **kw)


def stage(p, name):
    return next(s for s in p["stages"] if s["stage"] == name)


def test_apk_flutter_blutter_before_jadx_and_ghidra_deferred():
    p = plan({"format": "APK", "arch": "aarch64", "confidence": 0.95,
              "conditions": ["arch:aarch64", "arch:non_x86", "flutter_libapp", "native_libs"]})
    spec = next(c for c in p["ranked_chains"] if c["label"] == "specific")
    assert spec["chain"].index("blutter") < spec["chain"].index("jadx")
    assert spec["chain"][0] == "apktool"
    assert any(m["tool"] == "ghidra" for m in p["member_followups"])
    assert "ghidra" not in [c["tool"] for c in stage(p, "static")["candidates"]]


def test_arm_elf_skips_x86_only_capa_and_ranks_specific_first():
    p = plan({"format": "ELF", "arch": "arm", "confidence": 0.95, "conditions": ["arch:arm", "arch:non_x86"],
              "elf": {"type": "executable"}})
    capa = stage(p, "capability")["candidates"][0]
    assert capa["executable"] is False and "arch" in capa["skip_reason"]
    st = stage(p, "static")
    assert [c["tool"] for c in st["candidates"]][:2] == ["rizin", "ghidra"]
    assert p["ranked_chains"][0]["label"] == "specific"
    assert p["ranked_chains"][0]["fallbacks_by_stage"]["static"] == ["ghidra"]
    q = stage(p, "dynamic")["candidates"][0]
    assert q["tool"] == "qiling" and q["gate_preview"] == "needs_approval"


def test_firmware_plan_marks_emba_plan_only_off_runner():
    p = plan({"format": "firmware", "arch": "aarch64", "confidence": 0.85, "conditions": [], "os": "linux"})
    assert stage(p, "extract")["candidates"][0]["tool"] == "unblob"
    emba = stage(p, "firmware_analysis")["candidates"][0]
    assert emba["executable"] is False and "runner" in emba["skip_reason"]


def test_describe_only_rockchip_firmware():
    dec = orc.parse_describe("unknown firmware dump from a Rockchip device")
    assert dec["format"] == "firmware" and dec["vendors"] == ["rockchip"]
    f = orc.merge_declared(orc.described_only_fingerprint(dec), dec)
    p = plan(f)
    assert stage(p, "extract")["candidates"][0]["tool"] == "unblob"


def test_unregistered_tool_is_never_executable():
    reg = json.loads(json.dumps(REG))
    reg["routing"]["rules"][0]["chain"].append("nonexistent-tool")
    p = orc.build_plan({"format": "ELF", "arch": "x86_64", "confidence": 0.9, "conditions": []}, reg, hardware=[])
    allc = [c for s in p["stages"] for c in s["candidates"]]
    ne = next(c for c in allc if c["tool"] == "nonexistent-tool")
    assert ne["executable"] is False and ne["registered"] is False


# ------------------------------------------------------------------ permission gate
class A:
    def __init__(self, approve=(), approve_class=(), frank_ticket=None):
        self.approve_list, self.approve_class_list, self.frank_ticket = list(approve), list(approve_class), frank_ticket


def test_gate_matrix():
    assert orc.gate_decision("rizin", "read", A())["decision"] == "allow"
    assert orc.gate_decision("bettercap", "network", A(approve=["bettercap"], approve_class=["write"]))["decision"] == "blocked"
    assert orc.gate_decision("qiling", "write", A())["decision"] == "needs_approval"
    assert orc.gate_decision("qiling", "write", A(approve=["qiling"]))["decision"] == "allow"
    assert orc.gate_decision("openocd", "hw_read", A(approve_class=["hw_read"]))["decision"] == "allow"
    # approve-class write never unlocks hw_write
    assert orc.gate_decision("flashrom", "hw_write", A(approve=["flashrom"], approve_class=["write"]))["decision"] == "needs_frank"


def test_hw_write_frank_ticket(monkeypatch):
    now = orc.iso()
    good = {"title": "approve flashrom write on board X", "detail": "", "decision_prompt": "", "frank_response": "approve",
            "response_ts": now}
    monkeypatch.setattr(orc, "wq_show", lambda t: (good, None))
    assert orc.gate_decision("flashrom", "hw_write", A(frank_ticket=1))["decision"] == "allow"
    monkeypatch.setattr(orc, "wq_show", lambda t: ({**good, "frank_response": "no, wait"}, None))
    assert orc.gate_decision("flashrom", "hw_write", A(frank_ticket=1))["decision"] == "needs_frank"
    monkeypatch.setattr(orc, "wq_show", lambda t: ({**good, "title": "approve rkdeveloptool"}, None))
    assert orc.gate_decision("flashrom", "hw_write", A(frank_ticket=1))["decision"] == "needs_frank"
    monkeypatch.setattr(orc, "wq_show", lambda t: ({**good, "response_ts": "2026-01-01 00:00:00"}, None))
    assert orc.gate_decision("flashrom", "hw_write", A(frank_ticket=1))["decision"] == "needs_frank"
    assert orc.gate_decision("flashrom", "hw_write", A())["decision"] == "needs_frank"


# ------------------------------------------------------------------ envelope + runner refusals
def test_envelope_validation():
    env = {"target": {}, "tool": "rizin", "tool_version": "0.8", "analysis_id": "x", "started_at": "t", "status": "ok"}
    assert orc.validate_envelope(env, REG["output_envelope"]) == []
    assert orc.validate_envelope({**env, "status": "weird"}, REG["output_envelope"])
    assert "missing tool" in orc.validate_envelope({**env, "tool": None}, REG["output_envelope"])


def runner(spec):
    p = subprocess.run([sys.executable, os.path.join(BIN, "re-exec-runner"), "run"], input=json.dumps(spec),
                       capture_output=True, text=True, timeout=30)
    return json.loads(p.stdout)


def test_runner_refuses_network_and_foreign_workdir(tmp_path):
    base = {"analysis_id": "a", "tool": "t", "target": str(tmp_path), "argv": ["/bin/true"]}
    r = runner({**base, "workdir": "/data/re-work/x", "isolation": {"network": True}})
    assert r["status"] == "error" and "network" in r["runner_error"]
    r = runner({**base, "workdir": str(tmp_path / "w")})
    assert r["status"] == "error" and "/data/re-work" in r["runner_error"]


def test_limits_resolution_precedence():
    orch = {"defaults": {"limits": {"timeout_s": 300, "memory_mb": 4096, "disk_mb": 10240}}}
    o = {"limits": {"timeout_s": 900}}
    assert orc.resolve_limits(orch, o, None)["timeout_s"] == 900
    class X: timeout, mem, disk = 5, 64, None
    lim = orc.resolve_limits(orch, o, X())
    assert lim == {"timeout_s": 5, "memory_mb": 64, "disk_mb": 10240, "tasks_max": 2048}
