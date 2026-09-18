#!/usr/bin/env python3
"""storage-manifest-drift — survey-driven regen of storage_manifest.yaml, diffed against
the committed file, plus a FLEET_MAP.md lag check against the Minerva store (wq#1615).

Diff-and-report only. Nothing here writes storage_manifest.yaml or FLEET_MAP.md; the
regenerated candidate lands in STATE_DIR for review. Applying it is a later decision
(MINERVA_STALENESS_REVIEW.md §2.8, fix #8).

Per run:
  1. survey every manifest node read-only (lsblk -J + findmnt -J, over ssh BatchMode
     for remote nodes, the same transport fleet-telemetry uses);
  2. regenerate the devices/arrays/mounts sections from the survey into
     STATE_DIR/storage_manifest.candidate.yaml, keeping every hand-tuned field;
  3. diff candidate vs committed (findings + STATE_DIR/storage_manifest.diff);
  4. check FLEET_MAP.md against the store: lag behind the newest open fact, and nodes
     the map names whose current IP it does not carry;
  5. --apply: on a drift set not seen before, one aq row + one wq ticket.
"""
import argparse
import copy
import difflib
import hashlib
import json
import os
import re
import socket
import sqlite3
import subprocess
import sys
from datetime import datetime, timezone

import yaml

MANIFEST = os.environ.get("STORAGE_MANIFEST", "/home/lumen/agents/storage_manifest.yaml")
FLEET_MAP = os.environ.get("FLEET_MAP", "/home/lumen/FLEET_MAP.md")
MINERVA_DB = os.environ.get("MINERVA_DB", "/home/lumen/minerva/minerva.db")
STATE_DIR = os.environ.get("STORAGE_DRIFT_STATE",
                           "/home/lumen/.local/state/storage-manifest-drift")
LUMEN_STATE = "/home/lumen/minerva/lumen_state.py"
AQ = "/home/lumen/bin/aq"
WQ = "/home/lumen/bin/wq"
SSH_KEY = "/home/lumen/.ssh/id_ed25519"
CONNECT_TIMEOUT_S = 6
SURVEY_TIMEOUT_S = 40
FLEET_MAP_MAX_LAG_DAYS = 2
FLEET_MAP_IP_PREDICATES = ("tailscale_ip", "lan_ip")

SPLIT = "@@FINDMNT@@"
BYID_SPLIT = "@@BYID@@"
BYID_DIR = "/dev/disk/by-id/"
SURVEY_SCRIPT = (
    "lsblk -J -o NAME,TYPE,SERIAL,MODEL,SIZE,FSTYPE,UUID; "
    f"echo {SPLIT}; "
    "findmnt -J -o TARGET,SOURCE,FSTYPE,UUID; "
    f"echo {BYID_SPLIT}; "
    f"ls -1 {BYID_DIR}"
)
PSEUDO_FSTYPES = {"squashfs", "iso9660"}
# gen_manifest writes these when a node would not report a serial (apep's disk).
PLACEHOLDER_SERIALS = {"", "UNKNOWN"}
IP_RE = re.compile(r"(?<![\d.])\d{1,3}(?:\.\d{1,3}){3}(?![\d.])")
WQ_TITLE = "[storage] storage_manifest.yaml / FLEET_MAP.md drift vs live survey"


def log(msg):
    ts = datetime.now(timezone.utc).strftime("%H:%M:%S UTC")
    print(f"[{ts}] {msg}", flush=True)


def run(cmd, timeout=120, stdin=None):
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, input=stdin)
        return p.returncode, p.stdout or "", p.stderr or ""
    except Exception as exc:
        return 255, "", f"EXEC_FAILED: {exc}"


# --------------------------------------------------------------------- survey

def norm_md_uuid(uuid):
    """mdadm writes 8716cdfb:daba59e6:..., lsblk writes 8716cdfb-daba-59e6-...: same hex."""
    return re.sub(r"[^0-9a-f]", "", str(uuid or "").lower())


def md_colon_form(uuid):
    h = norm_md_uuid(uuid)
    return ":".join(h[i:i + 8] for i in range(0, len(h), 8))


def parse_lsblk(doc):
    """lsblk -J tree -> (disks, arrays). md devices repeat under every member; fold them."""
    disks, arrays = [], {}

    def walk(nodes, parent):
        for n in nodes:
            ntype = n.get("type") or ""
            serial = (n.get("serial") or "").strip()
            if ntype == "disk" and serial:
                disks.append({"serial": serial, "model": (n.get("model") or "").strip(),
                              "size": n.get("size")})
            if ntype.startswith("raid") or ntype == "linear":
                member_uuid = (parent or {}).get("uuid") \
                    if (parent or {}).get("fstype") == "linux_raid_member" else None
                arr = arrays.setdefault(n.get("name"), {
                    "name": n.get("name"), "level": ntype, "uuid": None, "members": set()})
                if member_uuid:
                    arr["uuid"] = md_colon_form(member_uuid)
                if parent:
                    arr["members"].add(parent.get("name"))
            walk(n.get("children") or [], n)

    walk(doc.get("blockdevices") or [], None)
    out_arrays = [{"name": a["name"], "uuid": a["uuid"], "level": a["level"],
                   "members": len(a["members"])} for a in arrays.values()]
    return (sorted(disks, key=lambda d: d["serial"]),
            sorted(out_arrays, key=lambda a: a["name"] or ""))


def parse_findmnt(doc):
    mounts = {}

    def walk(nodes):
        for n in nodes:
            source = (n.get("source") or "").split("[", 1)[0]
            fstype = n.get("fstype") or ""
            if (source.startswith("/dev/") and not source.startswith("/dev/loop")
                    and fstype not in PSEUDO_FSTYPES):
                mounts.setdefault(n["target"], {"target": n["target"], "fstype": fstype,
                                                "uuid": n.get("uuid")})
            walk(n.get("children") or [])

    walk(doc.get("filesystems") or [])
    return [mounts[t] for t in sorted(mounts)]


def parse_survey(text):
    blk, _, rest = text.partition(SPLIT)
    mnt, _, byid = rest.partition(BYID_SPLIT)
    disks, arrays = parse_lsblk(json.loads(blk))
    return {"devices": disks, "arrays": arrays, "mounts": parse_findmnt(json.loads(mnt)),
            "by_id": sorted(line.strip() for line in byid.splitlines() if line.strip())}


def get_roster():
    rc, out, _ = run(["python3", LUMEN_STATE, "current", "", "tailscale_ip"], timeout=15)
    pat = re.compile(r"^\s*(\S+)\.tailscale_ip\s*=\s*(\S+)")
    return {m.group(1): m.group(2) for m in map(pat.match, out.splitlines()) if m}


def survey_node(name, ip, local_host):
    if name == local_host:
        cmd = ["bash", "-c", SURVEY_SCRIPT]
    elif ip:
        cmd = ["ssh", "-i", SSH_KEY, "-o", "BatchMode=yes",
               "-o", f"ConnectTimeout={CONNECT_TIMEOUT_S}",
               "-o", "StrictHostKeyChecking=accept-new", f"{name}@{ip}", SURVEY_SCRIPT]
    else:
        return {"status": "no_address"}
    rc, out, err = run(cmd, timeout=SURVEY_TIMEOUT_S)
    if rc != 0:
        lines = err.strip().splitlines()
        return {"status": "unreachable", "error": (lines[-1] if lines else f"exit {rc}")[:200]}
    try:
        return {"status": "ok", **parse_survey(out)}
    except (ValueError, KeyError) as exc:
        return {"status": "unparseable", "error": str(exc)[:200]}


# ----------------------------------------------------------------------- diff

def diff_node(name, declared, live):
    findings = []

    def add(kind, key, want, got):
        findings.append({"node": name, "kind": kind, "key": key,
                         "declared": want, "live": got})

    optional = set(declared.get("optional_devices") or [])
    want_dev = {d["serial"]: d for d in declared.get("devices") or []
                if str(d.get("serial") or "") not in PLACEHOLDER_SERIALS}
    got_dev = {d["serial"]: d for d in live["devices"]}
    for serial in sorted(want_dev.keys() - got_dev.keys() - optional):
        add("device_missing", serial, want_dev[serial].get("model"), None)
    for serial in sorted(got_dev.keys() - want_dev.keys()):
        add("device_undeclared", serial, None,
            f"{got_dev[serial]['model']} {got_dev[serial]['size']}")
    for serial in sorted(want_dev.keys() & got_dev.keys()):
        for field in ("model", "size"):
            want, got = want_dev[serial].get(field), got_dev[serial].get(field)
            if want is not None and str(want) != str(got):
                add(f"device_{field}_changed", serial, want, got)

    want_arr = {norm_md_uuid(a.get("uuid")): a for a in declared.get("arrays") or []}
    got_arr = {norm_md_uuid(a["uuid"]): a for a in live["arrays"]}
    for uuid in sorted(want_arr.keys() - got_arr.keys()):
        add("array_missing", want_arr[uuid].get("name"), md_colon_form(uuid), None)
    for uuid in sorted(got_arr.keys() - want_arr.keys()):
        add("array_undeclared", got_arr[uuid]["name"], None,
            f"{got_arr[uuid]['level']} uuid={got_arr[uuid]['uuid']}")
    for uuid in sorted(want_arr.keys() & got_arr.keys()):
        want, got = want_arr[uuid], got_arr[uuid]
        if want.get("level") and want["level"] != got["level"]:
            add("array_level_changed", want.get("name"), want["level"], got["level"])
        members = want.get("members")
        count = len(members) if isinstance(members, list) else members
        if count is not None and count != got["members"]:
            add("array_members_changed", want.get("name"), count, got["members"])

    want_mnt = {m["target"]: m for m in declared.get("mounts") or [] if m.get("target")}
    got_mnt = {m["target"]: m for m in live["mounts"]}
    for target in sorted(want_mnt.keys() - got_mnt.keys()):
        m = want_mnt[target]
        if m.get("required", True) and not m.get("noauto"):
            add("mount_missing", target, f"{m.get('fstype')} uuid={m.get('uuid')}", None)
    for target in sorted(got_mnt.keys() - want_mnt.keys()):
        add("mount_undeclared", target, None,
            f"{got_mnt[target]['fstype']} uuid={got_mnt[target]['uuid']}")
    by_id = set(live.get("by_id") or [])
    for target in sorted(want_mnt.keys() & got_mnt.keys()):
        want_fs, got_fs = want_mnt[target].get("fstype"), got_mnt[target]["fstype"]
        if want_fs and want_fs != got_fs:
            add("mount_fstype_changed", target, want_fs, got_fs)
        want_id, got_id = str(want_mnt[target].get("uuid") or ""), got_mnt[target]["uuid"]
        # LVM roots are declared by device identity (/dev/disk/by-id/dm-uuid-LVM-…), not
        # FS-UUID: the check is that the node still carries that identity.
        if want_id.startswith(BYID_DIR):
            if want_id[len(BYID_DIR):] not in by_id:
                add("mount_device_missing", target, want_id, None)
        elif want_id and want_id.lower() != str(got_id or "").lower():
            add("mount_uuid_changed", target, want_id, got_id)
    return findings


def merge_section(declared, live, key, keep_unseen, update, new_entry):
    """Declared entries in declared order (updated from the survey, or kept when unseen
    but declared-optional), then survey-only entries appended — so a clean survey
    regenerates the committed section byte-for-byte and drift shows as a minimal diff."""
    got = {key(e): e for e in live}
    out, seen = [], set()
    for entry in declared:
        k = key(entry)
        seen.add(k)
        if k in got:
            out.append(update(dict(entry), got[k]))
        elif keep_unseen(entry):
            out.append(entry)
    out += [new_entry(e) for e in live if key(e) not in seen]
    return out


def regen_node(declared, live):
    """Declared node with its storage sections rebuilt from the survey. Hand-tuned
    per-entry fields (required, noauto, ...) survive on every entry the survey still sees."""
    node = copy.deepcopy(declared)
    optional = set(declared.get("optional_devices") or [])
    node["devices"] = merge_section(
        declared.get("devices") or [], live["devices"], lambda d: d.get("serial"),
        lambda d: d.get("serial") in optional
        or str(d.get("serial") or "") in PLACEHOLDER_SERIALS,
        lambda d, got: {**d, "model": got["model"], "size": got["size"]}, dict)
    node["arrays"] = merge_section(
        declared.get("arrays") or [], live["arrays"], lambda a: norm_md_uuid(a.get("uuid")),
        lambda a: False,
        lambda a, got: {**a, "level": got["level"], "members": got["members"]}, dict)
    node["mounts"] = merge_section(
        declared.get("mounts") or [], live["mounts"], lambda m: m.get("target"),
        lambda m: not m.get("required", True) or bool(m.get("noauto")),
        lambda m, got: {**m, "fstype": got["fstype"],
                        "uuid": m["uuid"] if str(m.get("uuid") or "").startswith(BYID_DIR)
                        else got["uuid"]},
        lambda m: {**m, "required": False, "noauto": False})
    return node


def check_manifest(manifest, surveys):
    candidate = copy.deepcopy(manifest)
    findings, coverage = [], {}
    for name, declared in sorted((manifest.get("nodes") or {}).items()):
        live = surveys.get(name, {"status": "not_surveyed"})
        coverage[name] = live["status"]
        if live["status"] != "ok":
            continue
        findings += diff_node(name, declared or {}, live)
        candidate["nodes"][name] = regen_node(declared or {}, live)
    return candidate, findings, coverage


def survey_all(manifest, roster=None, local_host=None):
    roster = get_roster() if roster is None else roster
    local_host = local_host or socket.gethostname().split(".")[0]
    surveys = {}
    for name, declared in sorted((manifest.get("nodes") or {}).items()):
        if (declared or {}).get("parked"):
            surveys[name] = {"status": "parked"}
            continue
        surveys[name] = survey_node(name, roster.get(name), local_host)
    return surveys


def yaml_diff(before, after):
    def dump(doc):
        return yaml.safe_dump(doc, sort_keys=True, default_flow_style=False).splitlines(True)
    return "".join(difflib.unified_diff(dump(before), dump(after),
                                        "storage_manifest.yaml (committed)",
                                        "storage_manifest.candidate.yaml (survey)"))


# ------------------------------------------------------------------ FLEET_MAP

def parse_ts(value):
    ts = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    return ts if ts.tzinfo else ts.replace(tzinfo=timezone.utc)


def check_fleet_map(map_path, db_path, max_lag_days=FLEET_MAP_MAX_LAG_DAYS):
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    newest = conn.execute(
        "select max(valid_from) from state_facts where valid_to is null").fetchone()[0]
    marks = ",".join("?" * len(FLEET_MAP_IP_PREDICATES))
    ips = conn.execute(
        f"select subject, predicate, value from state_facts "
        f"where valid_to is null and predicate in ({marks}) order by subject, predicate",
        FLEET_MAP_IP_PREDICATES).fetchall()
    conn.close()

    text = open(map_path, encoding="utf-8", errors="replace").read()
    mtime = datetime.fromtimestamp(os.path.getmtime(map_path), timezone.utc)
    findings = []
    lag_days = None
    if newest:
        lag_days = round((parse_ts(newest) - mtime).total_seconds() / 86400, 1)
        if lag_days > max_lag_days:
            findings.append({"node": "FLEET_MAP.md", "kind": "fleet_map_stale",
                             "key": "mtime", "declared": mtime.isoformat(timespec="seconds"),
                             "live": f"store newest fact {newest} ({lag_days}d ahead)"})
    for subject, predicate, value in ips:
        # Store values carry provenance prose: '192.168.8.211 (enp12s0, Lumen LAN — …)'.
        ip = IP_RE.search(value or "")
        if not ip:
            continue
        named = re.search(rf"(?<![\w-]){re.escape(subject)}(?![\w-])", text, re.IGNORECASE)
        carried = re.search(rf"(?<![\d.]){re.escape(ip.group())}(?![\d.])", text)
        if named and not carried:
            findings.append({"node": subject, "kind": "fleet_map_value_missing",
                             "key": predicate, "declared": None, "live": ip.group()})
    return findings, {"mtime": mtime.isoformat(timespec="seconds"),
                      "store_newest": newest, "lag_days": lag_days}


# --------------------------------------------------------------------- report

def fingerprint(findings):
    keys = sorted(f"{f['node']}|{f['kind']}|{f['key']}|{f['live']}" for f in findings)
    return hashlib.sha256("\n".join(keys).encode()).hexdigest()[:16]


def render(result):
    lines = [f"storage manifest: {result['manifest']}"]
    cov = result["coverage"]
    ok = sorted(n for n, s in cov.items() if s == "ok")
    lines.append(f"surveyed {len(ok)}/{len(cov)} nodes"
                 + "".join(f"; {n}={s}" for n, s in sorted(cov.items()) if s != "ok"))
    lines.append(f"survey-regenerated candidate differs from committed by "
                 f"{result['diff_lines']} line(s)")
    fm =result.get("fleet_map") or {}
    if fm:
        lines.append(f"FLEET_MAP.md mtime {fm['mtime']}, store newest {fm['store_newest']}, "
                     f"lag {fm['lag_days']}d")
    for section, items in (("storage_manifest.yaml drift", result["manifest_findings"]),
                           ("FLEET_MAP.md drift (proposal)", result["fleet_map_findings"])):
        lines.append(f"\n[{section}] {len(items)} finding(s)")
        for f in items[:40]:
            lines.append(f"  {f['node']:<14} {f['kind']:<24} {f['key']}: "
                         f"declared={f['declared']!r} live={f['live']!r}")
        if len(items) > 40:
            lines.append(f"  … {len(items) - 40} more")
    lines.append(f"\nfingerprint {result['fingerprint']}")
    for name, outcome in result.get("actions", {}).items():
        lines.append(f"[applied] {name}: {outcome}")
    return "\n".join(lines)


def load_state(state_dir):
    try:
        with open(os.path.join(state_dir, "state.json")) as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return {}


def raise_drift(result, state_dir):
    """aq + wq once per new drift set. aq itself bumps an open row with the same
    (source, key); wq refuses a duplicate open title. The fingerprint stops a drift set
    that was reviewed and left alone from re-filing every day after the ticket closes."""
    findings = result["manifest_findings"] + result["fleet_map_findings"]
    state = load_state(state_dir)
    if not findings:
        state["clear_since"] = state.get("clear_since") or result["ran_at"]
        return state, {"raised": "no drift"}
    state.pop("clear_since", None)
    if state.get("raised_fingerprint") == result["fingerprint"]:
        return state, {"raised": f"already raised {state.get('raised_at')}"}

    counts = (f"{len(result['manifest_findings'])} manifest, "
              f"{len(result['fleet_map_findings'])} FLEET_MAP")
    detail = (
        f"storage-manifest-drift (wq#1615) found drift ({counts}) on {result['ran_at']}.\n"
        f"Report: {os.path.join(state_dir, 'report.txt')}\n"
        f"Survey-regenerated candidate: {os.path.join(state_dir, 'storage_manifest.candidate.yaml')}\n"
        f"Diff vs committed: {os.path.join(state_dir, 'storage_manifest.diff')}\n"
        "Review each finding: fix the node, or update the manifest (backup first; "
        "apply_overlay.py for hand-truth). FLEET_MAP.md findings are proposals.\n"
        + "\n".join(f"- {f['node']} {f['kind']} {f['key']}: declared={f['declared']!r} "
                    f"live={f['live']!r}" for f in findings[:25])
    )
    title = f"storage manifest drift: {counts} (fp {result['fingerprint']})"
    rc_aq, out_aq, err_aq = run([AQ, "add", "storage-manifest-drift", "drift", title, detail])
    rc_wq, out_wq, err_wq = run([WQ, "add", "--title", WQ_TITLE, "--lane", "build",
                                 "--pri", "3", "--detail", detail])
    if rc_aq == 0:
        state["raised_fingerprint"] = result["fingerprint"]
        state["raised_at"] = result["ran_at"]
    return state, {"aq": f"rc={rc_aq} {(out_aq or err_aq).strip()[:80]}",
                   "wq": f"rc={rc_wq} {(out_wq or err_wq).strip()[:120]}"}


def write_artifacts(state_dir, result, candidate, diff_text, state):
    os.makedirs(state_dir, exist_ok=True)
    with open(os.path.join(state_dir, "storage_manifest.candidate.yaml"), "w") as fh:
        fh.write(f"# survey-regenerated by storage-manifest-drift {result['ran_at']} "
                 f"(wq#1615) — NOT the committed manifest\n")
        yaml.safe_dump(candidate, fh, sort_keys=False, default_flow_style=False)
    with open(os.path.join(state_dir, "storage_manifest.diff"), "w") as fh:
        fh.write(diff_text)
    with open(os.path.join(state_dir, "report.txt"), "w") as fh:
        fh.write(render(result) + "\n")
    with open(os.path.join(state_dir, "state.json"), "w") as fh:
        json.dump({**state, "last_run": result["ran_at"],
                   "last_fingerprint": result["fingerprint"]}, fh, indent=2)


def check(manifest_path, map_path, db_path, surveys=None, apply=False, state_dir=STATE_DIR,
          write=True):
    with open(manifest_path) as fh:
        manifest = yaml.safe_load(fh)
    surveys = survey_all(manifest) if surveys is None else surveys
    candidate, manifest_findings, coverage = check_manifest(manifest, surveys)
    diff_text = yaml_diff(manifest, candidate)
    fleet_map_findings, fleet_map = [], None
    if map_path:
        fleet_map_findings, fleet_map = check_fleet_map(map_path, db_path)
    result = {
        "ran_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "manifest": manifest_path,
        "coverage": coverage,
        "diff_lines": sum(1 for line in diff_text.splitlines()
                          if line[:1] in "+-" and line[:3] not in ("+++", "---")),
        "manifest_findings": manifest_findings,
        "fleet_map_findings": fleet_map_findings,
        "fleet_map": fleet_map,
        "fingerprint": fingerprint(manifest_findings + fleet_map_findings),
        "actions": {},
    }
    state = load_state(state_dir)
    if apply:
        state, result["actions"] = raise_drift(result, state_dir)
    if write:
        write_artifacts(state_dir, result, candidate, diff_text, state)
    return result


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--manifest", default=MANIFEST)
    ap.add_argument("--fleet-map", default=FLEET_MAP)
    ap.add_argument("--no-fleet-map", action="store_true")
    ap.add_argument("--db", default=MINERVA_DB)
    ap.add_argument("--state-dir", default=STATE_DIR)
    ap.add_argument("--survey-json", help="use a saved {node: survey} map instead of ssh")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--apply", action="store_true",
                    help="raise aq + wq on a new drift set (never edits the manifest)")
    ap.add_argument("--strict", action="store_true", help="exit 1 when drift exists")
    args = ap.parse_args(argv)

    surveys = None
    if args.survey_json:
        with open(args.survey_json) as fh:
            surveys = json.load(fh)
    try:
        result = check(args.manifest, None if args.no_fleet_map else args.fleet_map,
                       args.db, surveys=surveys, apply=args.apply, state_dir=args.state_dir)
    except Exception as exc:
        print(f"CHECK_FAILED: {exc!r}", file=sys.stderr)
        return 2
    print(json.dumps(result, indent=2, default=str) if args.json else render(result))
    drift = result["manifest_findings"] or result["fleet_map_findings"]
    return 1 if (args.strict and drift) else 0


if __name__ == "__main__":
    sys.exit(main())
