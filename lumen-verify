#!/usr/bin/env python3
"""lumen_verify.py — wq#1613.

Verifier registry + sweep (MINERVA_STALENESS_REVIEW.md §2.2, §3.3, §3.4). No
probe existed for `lan_ip`, `ssh_user`, `role`, `status`, `hardware`, `ram`,
`storage`, `power_plug`, `kernel`, `os`, `*_version` or any `*_status`
predicate; the oldest status-class facts sat 67/67/61/61/58 days unverified.

Seven seed probes wrap tools that already exist (tailscale, fleet-inv,
systemctl, lsblk+mdstat+dmidecode, uname/pkg, plug-ctl, HTTP). A fact is due
when `evidence_age > COALESCE(fact.verify_interval_days, registry predicate
interval, class default)` (status 3d, config 14d, identity 90d). Probes run
batched per node, never per fact.

Reconciliation contract (§3.2, §3.4): NOT owned here (wq#2053). reconcile()
reports what a probe saw to lumen_state.report_observation() (wq#1608) and
lets the real ingest() decide, so every write keeps the store invariants
(lifecycle-closed gate, canonical predicate, out-of-order guard, memory_audit
trail):
  match     -> reconfirmed: bump last_confirmed_at, clear contradicted_at.
  mismatch  -> superseded: close the old fact, open a new observed one --
               UNLESS the open fact is `frank_stated`, which is never
               auto-superseded. That case writes a `fact_verifications` row
               with outcome=unknown and files an aq row for a human.
  error/absent/refused-by-the-gate -> unknown. Never mutates the fact (fail-open).

  lumen_verify.py sweep [--db PATH] [--registry PATH] [--dry-run] [--json]
  lumen_verify.py sweep --now ISO8601   # deterministic for tests
"""
import argparse
import shlex
import json
import os
import re
import sqlite3
import subprocess
import sys
import uuid
from datetime import datetime, timedelta, timezone

import yaml

MINERVA_DB = os.environ.get("MINERVA_DB", "/home/lumen/minerva/minerva.db")
REGISTRY_PATH = os.environ.get(
    "PREDICATE_REGISTRY", "/home/lumen/minerva/verify/registry.yaml"
)
MINERVA_DIR = os.environ.get("MINERVA_DIR", "/home/lumen/minerva")
AQ = "/home/lumen/bin/aq"
WQ = "/home/lumen/bin/wq"

CLASS_DEFAULTS = {"status": 3, "config": 14, "identity": 90}

# wq#5026: a probe that cannot apply to a fact's VALUE (free-text `status` is not a unit
# name, a prose url is not a URL, a described ssh_user is not a login) is not a probe
# failure. Such an observation starts with NA_PREFIX; the sweep counts it as `n/a` and writes
# NO fact_verifications row, so the fact reads as unprobed (UNVERIFIABLE) instead of as a
# failing probe (BLOCKED in the wq#5008 taxonomy). 107 of 212 unknowns were this.
NA_PREFIX = "N/A: "

MIGRATION_SQL = (
    ("state_facts", "source_kind", "TEXT"),
    ("state_facts", "verify_recipe", "TEXT"),
    ("state_facts", "verify_interval_days", "REAL"),
    ("state_facts", "contradicted_at", "TEXT"),
    ("state_facts", "derived_from", "TEXT"),
)

FACT_VERIFICATIONS_DDL = """
CREATE TABLE IF NOT EXISTS fact_verifications (
  id TEXT PRIMARY KEY, fact_id TEXT NOT NULL, subject TEXT, predicate TEXT,
  probe TEXT NOT NULL, observed TEXT, outcome TEXT NOT NULL,
  ts TEXT NOT NULL, detail TEXT
);
"""
FACT_VERIFICATIONS_IDX = (
    "CREATE INDEX IF NOT EXISTS idx_fv_fact ON fact_verifications(fact_id, ts);"
)

FRANK_STATED = "frank_stated"


def now_iso():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def log(msg):
    print(f"[{now_iso()}] {msg}", flush=True)


def run(argv, timeout=20):
    try:
        p = subprocess.run(argv, capture_output=True, text=True, timeout=timeout)
        return p.returncode, p.stdout, p.stderr
    except (OSError, subprocess.SubprocessError) as e:
        return 255, "", str(e)


# ------------------------------------------------------------------ registry

class Registry:
    """Predicate + probe registry (shared predicate list with wq#1632)."""

    def __init__(self, path):
        self.path = path
        with open(path) as f:
            self.doc = yaml.safe_load(f) or {}
        self.predicates = self.doc.get("predicates", {})
        self.patterns = self.doc.get("patterns", [])
        self.probes = self.doc.get("probes", {})
        self.class_defaults = self.doc.get("class_defaults", CLASS_DEFAULTS)

    def _entry(self, predicate):
        if predicate in self.predicates:
            return self.predicates[predicate] or {}
        for pat in self.patterns:
            if re.fullmatch(pat.get("match", "").replace("*", ".*"), predicate):
                return pat
        return {}

    def probe_id(self, predicate):
        return self._entry(predicate).get("probe")

    def predicate_class(self, predicate):
        return self._entry(predicate).get("class")

    def interval_days(self, predicate):
        entry = self._entry(predicate)
        if entry.get("verify_interval_days") is not None:
            return entry["verify_interval_days"]
        return self.class_defaults.get(entry.get("class"))

    def source_kind_default(self, predicate):
        return self._entry(predicate).get("source_kind")


# -------------------------------------------------------------------- schema

def migrate(conn):
    cols = {r[1] for r in conn.execute("PRAGMA table_info(state_facts)")}
    for table, col, coltype in MIGRATION_SQL:
        if col not in cols:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {col} {coltype}")
    conn.execute(FACT_VERIFICATIONS_DDL)
    conn.execute(FACT_VERIFICATIONS_IDX)
    conn.commit()


# ---------------------------------------------------------------- selection

def evidence_age_days(fact, now):
    basis = fact["last_confirmed_at"] or fact["ingested_at"] or fact["valid_from"]
    if not basis:
        return None
    age = now - datetime.fromisoformat(basis)
    return age.total_seconds() / 86400.0


def due_facts(conn, registry, now):
    """Open facts whose evidence_age exceeds their resolved interval, and
    which have a probe assigned. Grouped by (probe_id, subject) for batching."""
    batches = {}
    for row in conn.execute(
        "SELECT * FROM state_facts WHERE valid_to IS NULL"
    ):
        fact = dict(row)
        probe_id = registry.probe_id(fact["predicate"])
        if not probe_id or probe_id not in PROBES:
            continue
        interval = fact.get("verify_interval_days")
        if interval is None:
            interval = registry.interval_days(fact["predicate"])
        if interval is None:
            continue
        age = evidence_age_days(fact, now)
        if age is None or age <= interval:
            continue
        batches.setdefault((probe_id, fact["subject"]), []).append(fact)
    return batches


# -------------------------------------------------------------------- probes
#
# Each probe takes (subject, [fact dicts]) and returns
# {(subject, predicate): (observed_value_or_None, error_or_None)}.
# A raised exception is caught by the caller and treated as an error for
# every fact in the batch -- fail-open, per probe.

def probe_tailscale_ip(subject, facts):
    rc, out, err = run(["tailscale", "status", "--json"])
    if rc != 0:
        return {(subject, f["predicate"]): (None, f"tailscale rc={rc}: {err[:120]}")
                for f in facts}
    data = json.loads(out)
    peers = {p.get("HostName", "").lower(): p for p in data.get("Peer", {}).values()}
    self_peer = data.get("Self") or {}
    if subject.lower() == (self_peer.get("HostName") or "").lower():
        ips = self_peer.get("TailscaleIPs") or []
    else:
        ips = (peers.get(subject.lower()) or {}).get("TailscaleIPs") or []
    observed = ips[0] if ips else None
    return {(subject, f["predicate"]): (observed, None if observed else "not in tailscale status")
            for f in facts}


SSH_USER_RE = re.compile(r"[a-z_][a-z0-9_-]{0,31}")
IPV4_RE = re.compile(r"\binet (\d{1,3}(?:\.\d{1,3}){3})/")


def probe_fleet_inventory(subject, facts):
    """`lan_ip` and `ssh_user`, observed on the node itself (wq#2150).

    This probe used to run `fleet-inv get <subject> --json`, which fleet-inv
    rejects (rc=2), so every fact came back unknown. The obvious repair -- read
    `fleet-inv get <subject> network.lan_ip` -- would be worse than the bug:
    fleet-inv reads network.lan_ip / ssh.user LIVE FROM state_facts (its YAML is
    only a fallback), so it would hand the store's own value back and every
    sweep would 'confirm' it, bumping last_confirmed_at with no evidence
    (MINERVA_STALENESS_REVIEW.md 3.2: a probe must observe, not re-read). So the
    node is asked instead, and only an observation that matches confirms:
      lan_ip   the fact value is one of the node's IPv4 addresses (`ip -o -4 addr`)
      ssh_user a BatchMode login as that user succeeds
    Anything else, including a mismatch, is unknown: a multi-homed node's other
    address is not proof the LAN address changed. `power_plug` has no
    independent observation (plug-ctl labels are dated observations, fleet-inv's
    plug binding is declared) and is no longer assigned to this probe.
    """
    result = {}
    addresses = None
    for f in facts:
        key = (subject, f["predicate"])
        if f["predicate"] == "lan_ip":
            if addresses is None:
                argv = ["ip", "-o", "-4", "addr", "show"]
                rc, out, err = run(argv if subject == "lumen" else _ssh_argv(subject, argv))
                addresses = (None, f"ssh {subject}: {err[:120] or 'rc=%s' % rc}") if rc != 0 \
                    else (IPV4_RE.findall(out), None)
            found, why = addresses
            if found is None:
                result[key] = (None, why)
            elif f["value"] in found:
                result[key] = (f["value"], None)
            else:
                result[key] = (None, f"{f['value']} not among {subject} addresses {found}")
        elif f["predicate"] == "ssh_user":
            user = f["value"]
            if not SSH_USER_RE.fullmatch(user or ""):
                result[key] = (None, f"{NA_PREFIX}value {(user or '')[:40]!r} is not a login name")
            elif subject == "lumen":
                rc, out, err = run(["id", "-un"])
                result[key] = (user, None) if rc == 0 and out.strip() == user else (
                    None, f"local user is {out.strip() or 'unknown'!r}")
            else:
                rc, out, err = run(["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=10",
                                    "-l", user, subject, "--", "true"])
                result[key] = (user, None) if rc == 0 else (
                    None, f"ssh -l {user} {subject}: rc={rc} {err[:100]}")
        else:
            result[key] = (None, f"fleet_inventory cannot observe {f['predicate']}")
    return result


FLEET_INVENTORY_DIR = os.environ.get("FLEET_INVENTORY_DIR", "/home/lumen/fleet/inventory")
_SSH_TARGETS = {}


class NotReachable(RuntimeError):
    """The roster says this node is not reachable by design (ssh method n/a)."""


def _ssh_target(node):
    """(target, key) for `node` (wq#1688). The bare subject name only resolves for nodes
    with a matching MagicDNS / ssh-config entry (`ssh gameboy` fails name resolution), so
    every SSH probe silently answered unknown for the rest. Resolve user@tailscale_ip from
    the roster (store-first, wq#1631); bare name only when the roster has no such node."""
    if node in _SSH_TARGETS:
        return _SSH_TARGETS[node]
    try:
        if FLEET_INVENTORY_DIR not in sys.path:
            sys.path.insert(0, FLEET_INVENTORY_DIR)
        import fleet_inventory as inv
    except Exception:  # noqa: BLE001 - no roster module: behave as before
        _SSH_TARGETS[node] = (node, None)
        return _SSH_TARGETS[node]
    rec = inv.get(node)
    if rec is None:
        _SSH_TARGETS[node] = (node, None)
    else:
        try:
            _SSH_TARGETS[node] = (inv.ssh_target(node), inv.ssh_key(rec))
        except inv.InventoryError as e:
            _SSH_TARGETS[node] = (None, str(e))
    return _SSH_TARGETS[node]


def _ssh_argv(node, remote_argv):
    target, key = _ssh_target(node)
    if target is None:
        raise NotReachable(key)
    argv = ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=10"]
    if key:
        argv += ["-i", key]
    # ssh joins its trailing args with spaces into ONE remote shell string, so an argv
    # like ["sh", "-c", "a; b"] loses its quoting and runs `sh -c a` then `b` in the login
    # shell (wq#1688 lost the system-serial section that way). Quote it ourselves.
    return argv + [target, "--", shlex.join(remote_argv)]


UNIT_NAME_RE = re.compile(
    r"[A-Za-z0-9:_.@\\-]+\.(service|socket|timer|mount|path|target)"
)


def probe_service_state(subject, facts):
    """A `status` / `*_status` fact holds free text (RESTORED, healthy, ...), not
    a unit name, so `systemctl is-active <value>` reports "inactive" for a
    non-unit and the sweep wrote that over the real value (wq#2192). Only a
    value that IS a unit name is probed, and an active unit confirms the fact
    (observed == value); anything else is unknown, never a new value.
    """
    result = {}
    for f in facts:
        key = (subject, f["predicate"])
        unit = f["value"]
        if not UNIT_NAME_RE.fullmatch(unit or ""):
            result[key] = (None, f"{NA_PREFIX}value {unit[:40]!r} is not a systemd unit name")
            continue
        argv = ["systemctl", "is-active", unit]
        rc, out, err = run(argv if subject == "lumen" else _ssh_argv(subject, argv))
        out = out.strip()
        if rc in (255, 99):
            result[key] = (None, f"ssh {subject}: {err[:120]}")
        elif out == "active":
            result[key] = (unit, None)
        else:
            result[key] = (None, f"{unit} is {out or 'unknown'} (rc={rc})")
    return result


SIZE_UNITS = {"T": 10**12, "TB": 10**12, "G": 10**9, "GB": 10**9, "M": 10**6, "MB": 10**6}
SIZE_UNITS_BINARY = {"T": 2**40, "TB": 2**40, "TIB": 2**40, "G": 2**30, "GB": 2**30,
                     "GIB": 2**30, "M": 2**20, "MB": 2**20, "MIB": 2**20}
SIZE_TOKEN_RE = re.compile(
    r"(?:(\d+)\s*[x×]\s*)?(\d+(?:\.\d+)?)\s*(TiB|GiB|MiB|TB|GB|MB|T|G|M)\b", re.I)
SIZE_TOLERANCE = 0.05


def _stated_size_candidates(text):
    """Byte totals a free-text hardware value could be stating: each size token
    on its own (`64GB DDR5 (4x16GB)` repeats one total) and all of them summed
    (`1TB SSD + 1TB HDD`), each as decimal and as binary units."""
    tokens = []
    for count, amount, unit in SIZE_TOKEN_RE.findall(text or ""):
        n = int(count or 1) * float(amount)
        unit = unit.upper()
        tokens.append((n * SIZE_UNITS.get(unit, SIZE_UNITS_BINARY[unit]),
                       n * SIZE_UNITS_BINARY[unit]))
    if not tokens:
        return []
    return [c for pair in tokens for c in pair] + [sum(t[0] for t in tokens),
                                                    sum(t[1] for t in tokens)]


def _size_matches(observed_bytes, stated):
    return any(abs(observed_bytes - c) <= SIZE_TOLERANCE * c
               for c in _stated_size_candidates(stated))


def _confirm_size(subject, fact, observed_bytes, what):
    """ram/storage hold free text (`64GB`, `2x 1TB NVMe RAID1`), so a measured
    total is never the new value (wq#2204): it only confirms a stored value
    that states a matching size, and anything else is unknown."""
    key = (subject, fact["predicate"])
    if not observed_bytes:
        return {key: (None, f"no {what} observed")}
    if _size_matches(observed_bytes, fact["value"]):
        return {key: (fact["value"], None)}
    return {key: (None, f"stored {fact['value'][:40]!r} does not state the observed "
                        f"{what} ({observed_bytes} bytes)")}


def _dmidecode_bytes(out):
    """Total installed RAM: every DIMM's `Size:`, not just the first."""
    total = 0
    for amount, unit in re.findall(r"^\s*Size:\s*(\d+)\s*(GB|MB)\b", out, re.M):
        total += int(amount) * SIZE_UNITS_BINARY[unit]
    return total


def _lsblk_bytes(out):
    """Whole-disk capacity: top-level `disk` devices only, so partitions, loop
    and optical devices are not counted twice or at all."""
    devices = json.loads(out).get("blockdevices", [])
    return sum(int(d.get("size") or 0) for d in _real_disks(devices))


def _real_disks(devices):
    """lsblk reports zram swap as TYPE=disk (wq#1580 recorded thor's zram0 as a drive)."""
    return [d for d in devices if d.get("type") == "disk"
            and not str(d.get("name") or "").startswith(("zram", "loop"))]


STATED_SERIAL_RE = re.compile(r"\bsn[:\s]+([A-Za-z0-9][A-Za-z0-9./_-]{4,})", re.I)
MAC_RE = re.compile(r"\b([0-9a-f]{2}(?::[0-9a-f]{2}){5})\b", re.I)


def _stated_serials(text):
    """`sn A/B/C` (RAID members written together) states three serials."""
    out = set()
    for m in STATED_SERIAL_RE.findall(text or ""):
        out |= {p.strip(".,;:()").upper() for p in m.split("/") if len(p.strip(".,;:()")) >= 5}
    return out


DMI_PLACEHOLDERS = {"default string", "to be filled by o.e.m.", "system serial number",
                    "not specified", "not applicable", "none", "0123456789", "0",
                    "chassis serial number", "base board serial number", ""}


def _dmi_serials(text):
    """Real serials from `dmidecode -s ...` output: one per line, vendor placeholders
    ('Default string' on the Trigkey/WeiBu boards) dropped whole, never tokenized."""
    out = set()
    for line in (text or "").splitlines():
        for part in line.split("/"):
            v = part.strip()
            if v and v.lower() not in DMI_PLACEHOLDERS and not v.startswith("#") and len(v) >= 5:
                out.add(v.upper())
    return out


def _sudo_refused(err):
    e = (err or "").lower()
    return "password is required" in e or "interactive authentication" in e or "sorry, user" in e


NODE_STORAGE_OBSERVERS = {
    "ram": (["sudo", "-n", "dmidecode", "--type", "17"], _dmidecode_bytes, "RAM"),
    "storage": (["lsblk", "-J", "-b", "-d", "-o", "NAME,SIZE,TYPE"], _lsblk_bytes, "disk"),
}


LSBLK_SERIAL_ARGV = ["lsblk", "-J", "-b", "-d", "-o", "NAME,SIZE,TYPE,SERIAL"]


def _confirm_storage_serials(subject, fact):
    """wq#1688: storage is keyed by SERIAL, never by /dev name (lumen's boot nvme moved
    nvme4 -> nvme5 across boots). Every serial the stored text states must be a whole disk
    on the node now. Returns None when the value states no serial (caller falls back)."""
    stated = _stated_serials(fact["value"])
    if not stated:
        return None
    key = (subject, fact["predicate"])
    rc, out, err = run(LSBLK_SERIAL_ARGV if subject == "lumen" else _ssh_argv(subject, LSBLK_SERIAL_ARGV))
    if rc != 0:
        return {key: (None, f"ssh {subject}: {err[:120] or 'lsblk failed'}")}
    try:
        seen = {str(d.get("serial") or "").strip().upper()
                for d in _real_disks(json.loads(out).get("blockdevices", []))}
    except ValueError as e:
        return {key: (None, f"lsblk unparseable: {e}")}
    missing = sorted(stated - seen)
    if missing:
        return {key: (None, f"MISMATCH: stated disk serial(s) {missing} not on {subject} "
                            f"(observed {sorted(s for s in seen if s)})")}
    return {key: (fact["value"], None)}


def probe_node_storage(subject, facts):
    result = {}
    for f in facts:
        key = (subject, f["predicate"])
        if f["predicate"] in ("storage", "storage_layout"):
            by_serial = _confirm_storage_serials(subject, f)
            if by_serial is not None:
                result.update(by_serial)
                continue
            if f["predicate"] == "storage_layout":
                result[key] = (None, "storage_layout states no disk serial to confirm")
                continue
        if f["predicate"] not in NODE_STORAGE_OBSERVERS:
            result[key] = (None, f"node_storage cannot observe {f['predicate']}")
            continue
        argv, measure, what = NODE_STORAGE_OBSERVERS[f["predicate"]]
        rc, out, err = run(argv if subject == "lumen" else _ssh_argv(subject, argv))
        if rc != 0:
            result[key] = (None, "sudo -n denied" if _sudo_refused(err) else f"ssh {subject}: {err[:120]}")
            continue
        try:
            result.update(_confirm_size(subject, f, measure(out), what))
        except ValueError as e:
            result[key] = (None, f"{argv[0]} unparseable: {e}")
    return result


IDENTITY_ARGV = ["sh", "-c",
                 "echo ---SYS---; sudo -n dmidecode -s system-serial-number 2>&1; "
                 "echo ---BOARD---; sudo -n dmidecode -s baseboard-serial-number 2>&1; "
                 "echo ---LINK---; ip -o link 2>&1"]


def _sections(out):
    sec, cur = {}, None
    for line in out.splitlines():
        m = re.fullmatch(r"---([A-Z]+)---", line.strip())
        if m:
            cur = m.group(1)
            sec[cur] = []
        elif cur:
            sec[cur].append(line)
    return {k: "\n".join(v).strip() for k, v in sec.items()}


def probe_node_identity(subject, facts):
    """wq#1688: hardware / nic / nic_config hold curated free text, so a probe string is
    never the new value (wq#2204). Stable tokens CONFIRM the stored value or it is unknown:
    hardware -> DMI system or baseboard serial appears in the text; nic -> every MAC the
    text states is on the node. A stated token that is not observed is a MISMATCH, logged
    as unknown for a human, never superseded."""
    result = {}
    argv = IDENTITY_ARGV
    rc, out, err = run(argv if subject == "lumen" else _ssh_argv(subject, argv))
    if rc != 0 and not out.strip():
        return {(subject, f["predicate"]): (None, f"ssh {subject}: {err[:120] or 'no output'}")
                for f in facts}
    sec = _sections(out)
    serials = set()
    sudo_denied = False
    for part in ("SYS", "BOARD"):
        v = sec.get(part, "")
        if _sudo_refused(v):
            sudo_denied = True
            continue
        serials |= _dmi_serials(v)
    macs = {m.lower() for m in re.findall(r"link/ether\s+([0-9a-f:]{17})", sec.get("LINK", ""), re.I)}
    for f in facts:
        key = (subject, f["predicate"])
        text = f["value"] or ""
        if f["predicate"] == "hardware":
            if sudo_denied or not serials:
                result[key] = (None, "sudo -n denied" if sudo_denied else "no DMI serial readable")
                continue
            words = {w.strip("/.,;:()").upper() for w in re.split(r"\s+", text)}
            if serials & words:
                result[key] = (text, None)
            elif _stated_serials(text):
                result[key] = (None, f"MISMATCH: stated serial(s) {sorted(_stated_serials(text))} "
                                     f"vs observed {sorted(serials)}")
            else:
                result[key] = (None, "stored value states no serial to confirm")
        elif f["predicate"] in ("nic", "nic_config"):
            stated = {m.lower() for m in MAC_RE.findall(text)}
            if not stated:
                result[key] = (None, "stored value states no MAC to confirm")
            elif not macs:
                result[key] = (None, "no MAC observed (ip -o link empty)")
            elif stated <= macs:
                result[key] = (text, None)
            else:
                result[key] = (None, f"MISMATCH: stated MAC(s) {sorted(stated - macs)} not on {subject}")
        else:
            result[key] = (None, f"node_identity cannot observe {f['predicate']}")
    return result


def probe_os_kernel(subject, facts):
    """Only `kernel` (uname -r) is answerable. `uname -s` ("Linux") was once
    returned for every other predicate and overwrote ollama_version, *_version
    and os facts (wq#2192); those now come back unknown.
    """
    result = {}
    for f in facts:
        if f["predicate"] != "kernel":
            result[(subject, f["predicate"])] = (
                None, f"os_kernel cannot observe {f['predicate']}")
            continue
        argv = ["uname", "-r"]
        rc, out, err = run(argv if subject == "lumen" else _ssh_argv(subject, argv))
        out = out.strip()
        if rc != 0 or not out:
            result[(subject, f["predicate"])] = (None, f"ssh {subject}: {err[:120] or 'no output'}")
        else:
            result[(subject, f["predicate"])] = (out, None)
    return result


def probe_plug_map(subject, facts):
    rc, out, err = run(["plug-ctl", "status", "--json"])
    if rc != 0:
        return {(subject, f["predicate"]): (None, f"plug-ctl rc={rc}: {err[:120]}")
                for f in facts}
    try:
        data = json.loads(out)
    except ValueError as e:
        return {(subject, f["predicate"]): (None, f"plug-ctl non-json: {e}") for f in facts}
    entry = data.get(subject, {})
    result = {}
    for f in facts:
        val = entry.get(f["predicate"])
        result[(subject, f["predicate"])] = (
            str(val) if val is not None else None,
            None if val is not None else f"{f['predicate']} absent from plug-ctl",
        )
    return result


def _endpoint_url(subject, fact):
    value = (fact["value"] or "").strip()
    if fact["predicate"] == "url":
        return value if re.fullmatch(r"https?://\S+", value) else None
    return f"http://{subject}:{value}/" if value.isdigit() else None


def probe_endpoint_live(subject, facts):
    """A url/port fact is the address itself, and an HTTP GET can only say the
    address answers. The status code ('200') used to be returned as the
    observation and superseded the stored url/port (wq#2204); a good GET now
    confirms (observed == value), everything else is unknown.
    """
    import urllib.request
    result = {}
    for f in facts:
        key = (subject, f["predicate"])
        url = _endpoint_url(subject, f)
        if url is None:
            result[key] = (None, f"{NA_PREFIX}value {f['value'][:40]!r} is not a probeable "
                                 f"{f['predicate']}")
            continue
        try:
            with urllib.request.urlopen(url, timeout=8) as resp:
                status = resp.status
        except Exception as e:  # noqa: BLE001 - any transport failure is fail-open
            result[key] = (None, f"{type(e).__name__}: {e}")
            continue
        if 200 <= status < 400:
            result[key] = (f["value"], None)
        else:
            result[key] = (None, f"GET {url} answered {status}")
    return result


def probe_node_liveness(subject, facts):
    """wq#1620: khepri/wadjet sat offline for 26d/53d while lumen_state kept

    stale POWERED ON / IDLE+available facts, because no probe ever backed a
    node's own reachability -- unlike probe_service_state, an ssh failure
    here IS the observation (offline), not an inconclusive error. Mirrors
    the manual check that caught the drift: ssh, then tailscale ping
    (app-layer, so it isn't fooled by ICMP filtering that a bare ping would
    miss).
    """
    ssh_rc, _, _ = run(_ssh_argv(subject, ["true"]))
    ts_rc, _, _ = run(["tailscale", "ping", "--c", "1", subject])
    observed = "available" if ssh_rc == 0 or ts_rc == 0 else "offline"
    return {(subject, f["predicate"]): (observed, None) for f in facts}


PROBES = {
    "tailscale_ip": probe_tailscale_ip,
    "fleet_inventory": probe_fleet_inventory,
    "service_state": probe_service_state,
    "node_storage": probe_node_storage,
    "os_kernel": probe_os_kernel,
    "node_identity": probe_node_identity,
    "plug_map": probe_plug_map,
    "endpoint_live": probe_endpoint_live,
    "node_liveness": probe_node_liveness,
}


# ------------------------------------------------------------- reconciliation

def file_conflict(subject, predicate, old_value, observed, probe_id):
    title = (
        f"minerva verify conflict: {subject}.{predicate} frank_stated={old_value!r} "
        f"observed={observed!r} via {probe_id}"
    )
    run([WQ, "add", title, "--lane", "review", "--pri", "2",
         "--detail", f"{subject}.{predicate} is frank_stated and must never be "
         f"auto-superseded (MINERVA_STALENESS_REVIEW.md §3.4). Reconcile by hand.",
         "--archetype", "standard-engineering"])
    run([AQ, "add", "--source", "lumen-verify", "--severity", "warn", "--message", title])


def _lumen_state():
    """lumen_state is imported lazily: probes and --help must not need the store."""
    if MINERVA_DIR not in sys.path:
        sys.path.insert(0, MINERVA_DIR)
    import lumen_state
    return lumen_state


def _log_unknown(conn, fact, probe_id, observed, detail, now):
    """An 'unknown' outcome mutates no fact, but a fact_verifications row keeps
    'checked, could not tell' distinct from 'never checked' (§2.3). The store
    writes its own rows for confirmed / superseded / frank_stated conflicts."""
    conn.execute(
        "INSERT INTO fact_verifications "
        "(id, fact_id, subject, predicate, probe, observed, outcome, ts, detail) "
        "VALUES (?,?,?,?,?,?,?,?,?)",
        (str(uuid.uuid4()), fact["id"], fact["subject"], fact["predicate"], probe_id,
         observed, "unknown", now.isoformat(timespec="seconds"), detail),
    )


def reconcile(conn, fact, probe_id, observed, error, now):
    """Report `observed` for `fact` to lumen_state.report_observation() and
    return the outcome: confirmed | superseded | unknown. This function never
    compares old to new itself (MINERVA_STALENESS_REVIEW.md section 3.2)."""
    if error is not None or observed is None or not str(observed).strip():
        _log_unknown(conn, fact, probe_id, observed, error, now)
        return "unknown"

    ls = _lumen_state()
    try:
        res = ls.report_observation(
            fact["subject"], fact["predicate"], str(observed), probe_id,
            source_ref=f"verify:{probe_id}", conn=conn)
    except ls.StateGateError as e:  # gate refused: nothing was written
        _log_unknown(conn, fact, probe_id, observed,
                     f"refused by state gate: {type(e).__name__}: {e}", now)
        return "unknown"

    if res.get("conflict"):  # frank_stated disagrees: ingest logged the row, a human decides
        file_conflict(fact["subject"], fact["predicate"], res.get("stored"),
                      observed, probe_id)
        return "unknown"

    outcome = res.get("outcome")
    if outcome in ("confirmed", "superseded"):
        # ingest() carries no probe id onto the row, and lumen_state's staleness view
        # labels a row with no verify_recipe UNVERIFIABLE however often a probe
        # confirms it (14 of ~1000 open facts had one). A probe that just observed
        # the fact owns it, so record that (wq#2150).
        conn.execute(
            "UPDATE state_facts SET verify_recipe=? WHERE subject=? AND predicate=? "
            "AND valid_to IS NULL AND verify_recipe IS NULL",
            (probe_id, res["subject"], res["predicate"]))
        conn.commit()
    if outcome in ("confirmed", "superseded"):
        return outcome

    # no open fact, or ingest filed it as a historical row: the current value did not change
    _log_unknown(conn, fact, probe_id, observed,
                 "state store did not apply it as current "
                 f"(action={res.get('action', outcome)})", now)
    return "unknown"


# ------------------------------------------------------------------- sweep

SSH_PROBES = frozenset({"fleet_inventory", "service_state", "node_storage", "os_kernel",
                        "node_identity"})
# wq#5026: a closed (retired/decommissioned) subject is skipped for EVERY probe except
# node_liveness (still a real observation of a dead box). plug_map/endpoint_live/
# tailscale_ip used to keep logging `unknown` for retired plugs and boxes forever.
CLOSED_SUBJECT_PROBES_KEPT = frozenset({"node_liveness"})


def closed_subjects(conn):
    """{subject: lifecycle value} for subjects whose open lifecycle fact closes them."""
    ls = _lumen_state()
    reg = ls.registry()
    return {r["subject"]: r["value"] for r in conn.execute(
        "SELECT subject, value FROM state_facts WHERE predicate=? AND valid_to IS NULL",
        (reg["lifecycle"],)) if r["value"] in reg["closing"]}


def node_facts(conn, registry, subjects, probes):
    """wq#1688: every open fact of `subjects` whose predicate maps to one of `probes`,
    due or not -- `fleet-inv verify` asks now, it does not wait for the interval."""
    batches = {}
    marks = ",".join("?" * len(subjects))
    for row in conn.execute(
            f"SELECT * FROM state_facts WHERE valid_to IS NULL AND subject IN ({marks})",
            [s.lower() for s in subjects]):
        fact = dict(row)
        probe_id = registry.probe_id(fact["predicate"])
        if probe_id in probes and probe_id in PROBES:
            batches.setdefault((probe_id, fact["subject"]), []).append(fact)
    return batches


def sweep(conn, registry, now=None, dry_run=False, batches=None):
    now = now or datetime.now(timezone.utc)
    migrate(conn)
    if batches is None:
        batches = due_facts(conn, registry, now)
    counts = {"confirmed": 0, "superseded": 0, "unknown": 0, "skipped": 0, "n/a": 0}
    detail = []
    closed = closed_subjects(conn)
    for (probe_id, subject), facts in batches.items():
        if subject in closed and probe_id not in CLOSED_SUBJECT_PROBES_KEPT:
            # wq#2150: a retired/decommissioned node cannot answer; ssh would only
            # time out and log an unknown per fact per sweep. Nothing was checked.
            for f in facts:
                counts["skipped"] += 1
                detail.append({"subject": subject, "predicate": f["predicate"],
                               "probe": probe_id, "value": f["value"], "observed": None,
                               "outcome": "skipped",
                               "error": f"{subject} is {closed[subject]}; not probed"})
            continue
        try:
            observations = PROBES[probe_id](subject, facts)
        except Exception as e:  # noqa: BLE001 - fail-open: a broken probe never supersedes
            observations = {(subject, f["predicate"]): (None, f"{type(e).__name__}: {e}")
                             for f in facts}
        for f in facts:
            observed, error = observations.get((subject, f["predicate"]), (None, "no observation"))
            if error and str(error).startswith(NA_PREFIX):
                counts["n/a"] += 1
                detail.append({"subject": subject, "predicate": f["predicate"], "probe": probe_id,
                               "value": f["value"], "observed": None, "outcome": "n/a",
                               "error": error})
                continue
            if dry_run:
                outcome = "unknown" if (error or observed is None) else (
                    "confirmed" if observed == f["value"] else "superseded"
                )
            else:
                outcome = reconcile(conn, f, probe_id, observed, error, now)
            counts[outcome] = counts.get(outcome, 0) + 1
            detail.append({
                "subject": subject, "predicate": f["predicate"], "probe": probe_id,
                "value": f["value"], "observed": observed, "outcome": outcome,
                "error": error,
            })
    if not dry_run:
        conn.commit()
    return {"due": sum(len(v) for v in batches.values()), "counts": counts, "detail": detail}


# ---------------------------------------------------------------------- CLI

def main(argv=None):
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    sp = sub.add_parser("sweep")
    sp.add_argument("--db", default=MINERVA_DB)
    sp.add_argument("--registry", default=REGISTRY_PATH)
    sp.add_argument("--dry-run", action="store_true")
    sp.add_argument("--json", action="store_true")
    sp.add_argument("--now", default=None, help="ISO8601, for deterministic runs")
    np_ = sub.add_parser("node", help="wq#1688: run probes on these subjects now, due or not")
    np_.add_argument("subjects", nargs="+")
    np_.add_argument("--probes", default="node_identity,node_storage,os_kernel",
                     help="comma list (default: the hardware identity set)")
    np_.add_argument("--db", default=MINERVA_DB)
    np_.add_argument("--registry", default=REGISTRY_PATH)
    np_.add_argument("--dry-run", action="store_true")
    np_.add_argument("--json", action="store_true")
    args = ap.parse_args(argv)

    conn = sqlite3.connect(args.db)
    conn.row_factory = sqlite3.Row
    registry = Registry(args.registry)
    now = datetime.fromisoformat(args.now) if getattr(args, "now", None) else None
    batches = None
    if args.cmd == "node":
        probes = {p.strip() for p in args.probes.split(",") if p.strip()}
        unknown = probes - set(PROBES)
        if unknown:
            print(f"unknown probe(s): {sorted(unknown)}", file=sys.stderr)
            return 2
        migrate(conn)
        batches = node_facts(conn, registry, args.subjects, probes)
    result = sweep(conn, registry, now=now, dry_run=args.dry_run, batches=batches)
    if args.json:
        print(json.dumps(result, indent=2))
    else:
        log(f"due={result['due']} counts={result['counts']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
