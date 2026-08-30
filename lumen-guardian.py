#!/usr/bin/env python3
"""lumen-guardian — deterministic hardware-threshold graceful shutdown daemon.

Safety-critical: fails closed. No LLM in loop. No guessing.
Reads sensors from sysfs, RAS events from rasdaemon sqlite.
Actions: notify (ntfy-send), throttle (governor=powersave), shutdown (systemctl poweroff).

Config: /etc/lumen/guardian.rules.yml
Dry-run: /etc/lumen/guardian.dry-run (presence = no real actions)
Log: /var/log/lumen-guardian.log + journald
"""

import glob
import json
import logging
import os
import signal
import sqlite3
import subprocess
import sys
import time
import yaml
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

VERSION = "1.0.0"
DEFAULT_RULES = "/etc/lumen/guardian.rules.yml"
DRY_RUN_FLAG = "/etc/lumen/guardian.dry-run"
NTFY_SEND = "/home/lumen/bin/ntfy-send"
NTFY_TOPIC = "lumen-fleet"
RAS_DB = "/var/lib/rasdaemon/ras-mc_event.db"

log = logging.getLogger("lumen-guardian")


# ── Sensor discovery ──────────────────────────────────────────────

def discover_hwmon():
    """Map hwmon name -> list of hwmon dirs. Handles reboot renumbering."""
    result = defaultdict(list)
    for d in sorted(glob.glob("/sys/class/hwmon/hwmon*")):
        name_file = os.path.join(d, "name")
        try:
            with open(name_file) as f:
                result[f.read().strip()].append(d)
        except (FileNotFoundError, PermissionError):
            pass
    return dict(result)


def read_sysfs_int(path):
    """Read an integer from sysfs. Returns None on any failure."""
    try:
        with open(path) as f:
            return int(f.read().strip())
    except (FileNotFoundError, ValueError, PermissionError, OSError):
        return None


def millideg_to_c(val):
    """Convert millidegree sysfs value to °C float."""
    if val is None:
        return None
    return val / 1000.0


# ── Sensor readers ────────────────────────────────────────────────

class SensorReader:
    """Discovers and reads hardware sensors by name, not hwmon number."""

    def __init__(self):
        self.hwmon = discover_hwmon()
        self._validate()

    def _validate(self):
        required = ["coretemp", "dell_smm"]
        missing = [r for r in required if r not in self.hwmon]
        if missing:
            raise RuntimeError(f"Required hwmon not found: {missing}. "
                               f"Available: {list(self.hwmon.keys())}")

    def rediscover(self):
        self.hwmon = discover_hwmon()

    def _hwmon_dir(self, name):
        dirs = self.hwmon.get(name, [])
        return dirs[0] if dirs else None

    def cpu_package_temp_c(self):
        d = self._hwmon_dir("coretemp")
        if not d:
            return None
        return millideg_to_c(read_sysfs_int(os.path.join(d, "temp1_input")))

    def cpu_fan_rpm(self):
        """Read Processor Fan (fan1) from dell_smm."""
        d = self._hwmon_dir("dell_smm")
        if not d:
            return None
        return read_sysfs_int(os.path.join(d, "fan1_input"))

    def dimm_temps_c(self):
        """Read DIMM temps from dell_smm (temp2, temp3, temp4)."""
        d = self._hwmon_dir("dell_smm")
        if not d:
            return []
        temps = []
        for i in [2, 3, 4]:
            v = millideg_to_c(read_sysfs_int(os.path.join(d, f"temp{i}_input")))
            if v is not None:
                temps.append(v)
        return temps

    def nvme_temps_c(self):
        """Read composite temp from all NVMe hwmon entries."""
        temps = []
        for d in self.hwmon.get("nvme", []):
            v = millideg_to_c(read_sysfs_int(os.path.join(d, "temp1_input")))
            if v is not None:
                temps.append(v)
        return temps

    def amdgpu_edge_temp_c(self):
        d = self._hwmon_dir("amdgpu")
        if not d:
            return None
        return millideg_to_c(read_sysfs_int(os.path.join(d, "temp1_input")))

    def snapshot(self):
        """Full sensor snapshot for logging/notification."""
        return {
            "cpu_pkg_c": self.cpu_package_temp_c(),
            "cpu_fan_rpm": self.cpu_fan_rpm(),
            "dimm_temps_c": self.dimm_temps_c(),
            "nvme_temps_c": self.nvme_temps_c(),
            "amdgpu_edge_c": self.amdgpu_edge_temp_c(),
            "ts": datetime.now(timezone.utc).isoformat(),
        }


# ── RAS event monitor ─────────────────────────────────────────────

class RasMonitor:
    """Polls rasdaemon sqlite for new uncorrectable errors."""

    def __init__(self, db_path=RAS_DB):
        self.db_path = db_path
        self._last_mce_id = self._get_max_id("mce_record")
        self._last_mc_id = self._get_max_id("mc_event")

    def _get_max_id(self, table):
        try:
            conn = sqlite3.connect(f"file:{self.db_path}?mode=ro", uri=True,
                                   timeout=2)
            cur = conn.execute(f"SELECT MAX(id) FROM {table}")
            row = cur.fetchone()
            conn.close()
            return row[0] or 0
        except (sqlite3.Error, OSError) as e:
            log.warning("RAS DB read failed (%s): %s — will retry next poll", table, e)
            return 0

    def check_new_ue(self):
        """Returns list of new UE events since last check. Empty = safe."""
        events = []
        try:
            conn = sqlite3.connect(f"file:{self.db_path}?mode=ro", uri=True,
                                   timeout=2)
            # Check mce_record for uncorrected errors
            cur = conn.execute(
                "SELECT id, timestamp, mcistatus_msg, error_msg "
                "FROM mce_record WHERE id > ? AND mcistatus_msg NOT LIKE 'Corrected%'",
                (self._last_mce_id,))
            for row in cur:
                events.append({
                    "source": "mce_record", "id": row[0],
                    "timestamp": row[1], "status": row[2], "msg": row[3]
                })

            # Check mc_event for uncorrected errors
            cur = conn.execute(
                "SELECT id, timestamp, err_type, err_msg "
                "FROM mc_event WHERE id > ? AND err_type != 'Corrected'",
                (self._last_mc_id,))
            for row in cur:
                events.append({
                    "source": "mc_event", "id": row[0],
                    "timestamp": row[1], "type": row[2], "msg": row[3]
                })

            # Update watermarks even if no UE — track CE progress too
            cur = conn.execute("SELECT MAX(id) FROM mce_record")
            r = cur.fetchone()
            if r and r[0]:
                self._last_mce_id = r[0]
            cur = conn.execute("SELECT MAX(id) FROM mc_event")
            r = cur.fetchone()
            if r and r[0]:
                self._last_mc_id = r[0]

            conn.close()
        except (sqlite3.Error, OSError) as e:
            log.warning("RAS DB poll failed: %s — skipping this cycle", e)
        return events


# ── Actions ───────────────────────────────────────────────────────

def is_dry_run():
    return os.path.exists(DRY_RUN_FLAG)


def ntfy_push(title, body, priority="urgent"):
    """Push notification via ntfy-send. Best-effort — never blocks action."""
    try:
        subprocess.run(
            [NTFY_SEND, "-t", NTFY_TOPIC, "-p", priority, "-T", title, body],
            timeout=10, capture_output=True)
    except Exception as e:
        log.warning("ntfy-send failed: %s — proceeding with action anyway", e)


def set_governor_powersave():
    """Set all CPU governors to powersave. Returns True on success."""
    try:
        cpus = glob.glob("/sys/devices/system/cpu/cpu*/cpufreq/scaling_governor")
        for path in cpus:
            with open(path, "w") as f:
                f.write("powersave\n")
        log.info("Set %d CPUs to governor=powersave", len(cpus))
        return True
    except (PermissionError, OSError) as e:
        log.error("Failed to set governor: %s", e)
        return False


def graceful_shutdown(reason, snapshot, dry_run):
    """Execute graceful shutdown. This is the point of no return."""
    body = (f"REASON: {reason}\n"
            f"SNAPSHOT: {json.dumps(snapshot, indent=2)}")
    log.critical("SHUTDOWN TRIGGERED: %s", reason)
    log.critical("Sensor snapshot: %s", json.dumps(snapshot))

    if dry_run:
        log.warning("DRY-RUN: WOULD shutdown — %s", reason)
        ntfy_push("GUARDIAN DRY-RUN: WOULD SHUTDOWN", body, "urgent")
        return

    ntfy_push("GUARDIAN: SHUTTING DOWN LUMEN", body, "max")
    # Give ntfy a moment to deliver
    time.sleep(2)
    log.critical("Executing: systemctl poweroff")
    subprocess.run(["systemctl", "poweroff"], timeout=30)


# ── Rule evaluator ────────────────────────────────────────────────

class RuleEvaluator:
    """Tracks duration-based threshold violations and triggers actions."""

    def __init__(self, rules_cfg):
        self.rules = rules_cfg.get("rules", {})
        self.poll_interval = rules_cfg.get("poll_interval_s", 5)
        # Track how long each rule has been in violation (seconds)
        self._violation_start = {}  # rule_name -> first_violation_time
        self._throttle_applied = False
        self._notified = set()  # rule_names that have already sent a notification

    def _start_violation(self, name):
        if name not in self._violation_start:
            self._violation_start[name] = time.monotonic()
            log.debug("Rule %s: violation started", name)

    def _clear_violation(self, name):
        if name in self._violation_start:
            log.debug("Rule %s: violation cleared", name)
            del self._violation_start[name]
        self._notified.discard(name)

    def _violation_duration(self, name):
        start = self._violation_start.get(name)
        if start is None:
            return 0
        return time.monotonic() - start

    def evaluate(self, sensors, ras_monitor, dry_run):
        """Run all rules against current readings. Returns list of triggered actions."""
        triggered = []
        snapshot = sensors.snapshot()

        for name, rule in self.rules.items():
            action = self._evaluate_rule(name, rule, sensors, ras_monitor, snapshot)
            if action:
                triggered.append(action)

        # Execute actions in priority order: shutdown > throttle > notify
        shutdowns = [a for a in triggered if a["action"] == "shutdown"]
        throttles = [a for a in triggered if a["action"] == "throttle"]
        notifies  = [a for a in triggered if a["action"] == "notify"]

        for s in shutdowns:
            graceful_shutdown(s["reason"], snapshot, dry_run)
            return  # after shutdown, nothing else matters

        for t in throttles:
            if not self._throttle_applied:
                log.warning("THROTTLE: %s", t["reason"])
                if dry_run:
                    log.warning("DRY-RUN: WOULD set governor=powersave")
                else:
                    set_governor_powersave()
                self._throttle_applied = True
            if t["name"] not in self._notified:
                ntfy_push("GUARDIAN: CPU throttled", t["reason"], "high")
                self._notified.add(t["name"])

        for n in notifies:
            if n["name"] not in self._notified:
                log.warning("NOTIFY: %s", n["reason"])
                ntfy_push(f"GUARDIAN: {n['name']}", n["reason"], "high")
                self._notified.add(n["name"])


    def _evaluate_rule(self, name, rule, sensors, ras_monitor, snapshot):
        """Evaluate a single rule. Returns action dict or None."""
        sensor = rule.get("sensor", "")
        action_type = rule["action"]
        duration_s = rule.get("duration_s", 0)

        # ── CPU package thermal ──
        if sensor == "coretemp.package":
            temp = sensors.cpu_package_temp_c()
            threshold = rule["threshold_c"]
            if temp is None:
                log.warning("Rule %s: coretemp package sensor unreadable", name)
                # Fail closed: do NOT clear violation on read failure
                return None
            if temp >= threshold:
                self._start_violation(name)
                dur = self._violation_duration(name)
                if dur >= duration_s:
                    return {"name": name, "action": action_type,
                            "reason": f"{name}: CPU {temp:.1f}°C >= {threshold}°C for {dur:.0f}s"}
            else:
                self._clear_violation(name)
                if name == "cpu_thermal_throttle" and self._throttle_applied:
                    # Restore governor when temp drops below throttle threshold
                    log.info("CPU temp below throttle threshold, restoring governor")
                    self._throttle_applied = False
                    try:
                        cpus = glob.glob("/sys/devices/system/cpu/cpu*/cpufreq/scaling_governor")
                        for path in cpus:
                            with open(path, "w") as f:
                                f.write("schedutil\n")
                        log.info("Restored %d CPUs to governor=schedutil", len(cpus))
                    except (PermissionError, OSError) as e:
                        log.error("Failed to restore governor: %s", e)


        # ── CPU fan stall ──
        elif sensor == "dell_smm.fan.processor":
            fan_rpm = sensors.cpu_fan_rpm()
            cpu_temp = sensors.cpu_package_temp_c()
            guard_threshold = rule.get("guard_threshold_c", 60)
            if fan_rpm is None or cpu_temp is None:
                log.warning("Rule %s: sensor unreadable (fan=%s, cpu=%s)", name, fan_rpm, cpu_temp)
                return None
            if fan_rpm == 0 and cpu_temp >= guard_threshold:
                self._start_violation(name)
                dur = self._violation_duration(name)
                if dur >= duration_s:
                    return {"name": name, "action": action_type,
                            "reason": f"{name}: CPU fan=0 RPM while CPU={cpu_temp:.1f}°C >= {guard_threshold}°C for {dur:.0f}s"}
            else:
                self._clear_violation(name)

        # ── NVMe thermal (any drive) ──
        elif sensor == "nvme.all":
            threshold = rule["threshold_c"]
            temps = sensors.nvme_temps_c()
            if not temps:
                log.warning("Rule %s: no NVMe temps readable", name)
                return None
            hot = max(temps)
            if hot >= threshold:
                self._start_violation(name)
                dur = self._violation_duration(name)
                if dur >= duration_s:
                    return {"name": name, "action": action_type,
                            "reason": f"{name}: NVMe max {hot:.1f}°C >= {threshold}°C for {dur:.0f}s (all: {temps})"}
            else:
                self._clear_violation(name)


        # ── DIMM thermal (any DIMM) ──
        elif sensor == "dell_smm.dimm.all":
            threshold = rule["threshold_c"]
            temps = sensors.dimm_temps_c()
            if not temps:
                log.warning("Rule %s: no DIMM temps readable", name)
                return None
            hot = max(temps)
            if hot >= threshold:
                self._start_violation(name)
                dur = self._violation_duration(name)
                if dur >= duration_s:
                    return {"name": name, "action": action_type,
                            "reason": f"{name}: DIMM max {hot:.1f}°C >= {threshold}°C for {dur:.0f}s (all: {temps})"}
            else:
                self._clear_violation(name)

        # ── AMDGPU edge thermal ──
        elif sensor == "amdgpu.edge":
            threshold = rule["threshold_c"]
            temp = sensors.amdgpu_edge_temp_c()
            if temp is None:
                log.warning("Rule %s: AMDGPU sensor unreadable", name)
                return None
            if temp >= threshold:
                self._start_violation(name)
                dur = self._violation_duration(name)
                if dur >= duration_s:
                    return {"name": name, "action": action_type,
                            "reason": f"{name}: AMDGPU edge {temp:.1f}°C >= {threshold}°C for {dur:.0f}s"}
            else:
                self._clear_violation(name)

        # ── RAS uncorrectable error ──
        elif sensor == "rasdaemon.ue":
            events = ras_monitor.check_new_ue()
            if events:
                detail = json.dumps(events, indent=2)
                return {"name": name, "action": action_type,
                        "reason": f"{name}: {len(events)} uncorrectable error(s): {detail}"}

        else:
            log.error("Unknown sensor type: %s in rule %s", sensor, name)

        return None


# ── Config loading ────────────────────────────────────────────────

def load_config(path):
    """Load and validate rules config. Raises on any problem (fail closed)."""
    with open(path) as f:
        cfg = yaml.safe_load(f)
    if not isinstance(cfg, dict) or "rules" not in cfg:
        raise ValueError(f"Invalid config: missing 'rules' key in {path}")

    valid_actions = {"notify", "throttle", "shutdown"}
    valid_sensors = {
        "coretemp.package", "dell_smm.fan.processor", "nvme.all",
        "dell_smm.dimm.all", "amdgpu.edge", "rasdaemon.ue",
    }
    for name, rule in cfg["rules"].items():
        if rule.get("action") not in valid_actions:
            raise ValueError(f"Rule {name}: invalid action '{rule.get('action')}'")
        if rule.get("sensor") not in valid_sensors:
            raise ValueError(f"Rule {name}: unknown sensor '{rule.get('sensor')}'")
    return cfg


# ── SIGHUP reload ─────────────────────────────────────────────────

_reload_requested = False

def _handle_sighup(signum, frame):
    global _reload_requested
    _reload_requested = True


# ── Sensor history ring for snapshot ──────────────────────────────

class SensorHistory:
    """Keeps last N sensor snapshots for notification context."""

    def __init__(self, maxlen=6):  # 6 * 5s = 30s
        self.maxlen = maxlen
        self._ring = []

    def add(self, snapshot):
        self._ring.append(snapshot)
        if len(self._ring) > self.maxlen:
            self._ring.pop(0)

    def recent(self):
        return list(self._ring)


# ── Main loop ─────────────────────────────────────────────────────

def main():
    global _reload_requested

    rules_path = sys.argv[1] if len(sys.argv) > 1 else DEFAULT_RULES

    # Fail closed: refuse to start if config is bad
    try:
        cfg = load_config(rules_path)
    except Exception as e:
        print(f"FATAL: Cannot load config {rules_path}: {e}", file=sys.stderr)
        sys.exit(1)

    log_path = cfg.get("log_path", "/var/log/lumen-guardian.log")
    poll_s = cfg.get("poll_interval_s", 5)

    # Setup logging: file + stderr (journald)
    logging.basicConfig(
        level=logging.DEBUG,
        format="%(asctime)s %(levelname)-8s %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%S%z",
        handlers=[
            logging.FileHandler(log_path),
            logging.StreamHandler(sys.stderr),
        ],
    )

    dry_run = is_dry_run()
    log.info("lumen-guardian v%s starting (dry_run=%s, poll=%ds, config=%s)",
             VERSION, dry_run, poll_s, rules_path)

    signal.signal(signal.SIGHUP, _handle_sighup)

    # Initialize sensor reader — fail closed if required sensors missing
    try:
        sensors = SensorReader()
    except RuntimeError as e:
        log.critical("FATAL: Sensor init failed: %s", e)
        sys.exit(1)

    ras = RasMonitor()
    evaluator = RuleEvaluator(cfg)
    history = SensorHistory(maxlen=max(1, 30 // poll_s))

    log.info("Initial sensor snapshot: %s", json.dumps(sensors.snapshot()))
    log.info("RAS watermarks: mce_id=%d, mc_id=%d", ras._last_mce_id, ras._last_mc_id)
    log.info("Loaded %d rules: %s", len(cfg["rules"]), list(cfg["rules"].keys()))


    while True:
        try:
            # Handle SIGHUP config reload
            if _reload_requested:
                _reload_requested = False
                log.info("SIGHUP received — reloading config")
                try:
                    cfg = load_config(rules_path)
                    evaluator = RuleEvaluator(cfg)
                    poll_s = cfg.get("poll_interval_s", 5)
                    history = SensorHistory(maxlen=max(1, 30 // poll_s))
                    log.info("Config reloaded: %d rules", len(cfg["rules"]))
                except Exception as e:
                    log.error("Config reload failed: %s — keeping previous config", e)

            # Check dry-run flag each cycle (can be toggled live)
            dry_run = is_dry_run()

            # Rediscover hwmon periodically (handles hot-plug, though unlikely)
            sensors.rediscover()

            # Take snapshot and record history
            snap = sensors.snapshot()
            history.add(snap)

            # Evaluate all rules
            evaluator.evaluate(sensors, ras, dry_run)

            time.sleep(poll_s)

        except KeyboardInterrupt:
            log.info("Interrupted — shutting down cleanly")
            break
        except Exception as e:
            # Unexpected error: log and continue (fail closed = keep running)
            log.exception("Unexpected error in main loop: %s", e)
            time.sleep(poll_s)

    log.info("lumen-guardian stopped")


if __name__ == "__main__":
    main()
