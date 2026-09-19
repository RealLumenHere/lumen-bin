#!/usr/bin/env python3
"""Enforce Desktop Commander canonical config on every gateway start — LIVE
tier-1 enforcer (activated 2026-07-17, wq#1585 corrected this docstring which
had drifted: it previously described this file as a staged, not-yet-activated
draft, but it has been the live config since activation and is wired as
ExecStartPre of desktop-commander-mcp.service). Idempotent; preserves
clientId/usageStats if present; forces the three keys below plus a curated
blockedCommands list.

blockedCommands = irreversible / unrecoverable-data / host-availability ops
only (see BLOCKED below) — this replaced the earlier zero-restrictions config
from Frank's 2026-06-26 directive, preserved for reference in
dc-enforce-config.py.bak.20260717_001558_pre_tier1.

Matching semantics (verified from command-manager.js 2026-07-16): DC blocks on
basename(argv0), lowercased, per chained segment, after stripping env-vars and
leading path. So "dd" blocks /bin/dd, "sudo dd", "a | dd", "$(dd ..)", "X=1 dd".
It does NOT match subcommands/args — this is why the list is destructive
BINARIES only, never "docker" (would kill all docker) or "rm" (would kill all rm).

TO ROLLBACK to zero restrictions:
  cp /home/lumen/bin/dc-enforce-config.py.bak.20260717_001558_pre_tier1 /home/lumen/bin/dc-enforce-config.py
  sudo systemctl restart desktop-commander-mcp     # <- reconnects the mcpw path; Frank present
TO CHANGE the blocklist:
  cp /home/lumen/bin/dc-enforce-config.py /home/lumen/bin/dc-enforce-config.py.bak.$(date +%Y%m%d_%H%M%S)
  <edit BLOCKED below>
  sudo systemctl restart desktop-commander-mcp
"""
import json
from pathlib import Path

CFG = Path("/home/lumen/.claude-server-commander/config.json")

# Tier-1: irreversible / unrecoverable-data / host-availability ops only.
# Every entry blocks by basename(argv0) per segment. Reversible ops are NOT here.
BLOCKED = [
    "dd",                                  # raw block writes (USB policy = Balena only anyway)
    "mkfs", "mkfs.ext2", "mkfs.ext3", "mkfs.ext4",
    "mkfs.xfs", "mkfs.btrfs", "mkfs.vfat", "mkfs.fat", "mkfs.ntfs",
    "wipefs",                              # signature wipe
    "shred",                               # unrecoverable file destruction
    "fdisk", "sgdisk", "parted",           # partition table edits
    "reboot", "shutdown", "poweroff", "halt",  # host availability (no-reboot rule, enforced)
    "macchanger",                          # MAC spoof vector (enp0s25 protection)
]

FORCE = {
    "blockedCommands": BLOCKED,
    "pendingWelcomeOnboarding": False,     # the reset trigger -> off
    "telemetryEnabled": False,
    "allowedDirectories": [],              # keep broad FS operability (Frank operator path)
}


def main():
    CFG.parent.mkdir(parents=True, exist_ok=True)
    cfg = {}
    if CFG.exists():
        try:
            cfg = json.loads(CFG.read_text() or "{}")
        except Exception:
            cfg = {}
    cfg.update(FORCE)
    CFG.write_text(json.dumps(cfg, indent=2))
    print(f"[dc-enforce:tier1] blockedCommands={len(cfg.get('blockedCommands'))} "
          f"onboarding={cfg.get('pendingWelcomeOnboarding')}")


if __name__ == "__main__":
    main()
