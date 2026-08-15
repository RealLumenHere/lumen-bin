#!/usr/bin/env python3
"""STAGED tier-1 variant of dc-enforce-config.py — FRANK-GATED, not live.

Difference from the live enforcer: FORCE["blockedCommands"] carries the
irreversible-op blocklist instead of []. Everything else identical
(idempotent, preserves clientId/usageStats, kills onboarding reset).

Rationale: the live enforcer exists to defeat DC v0.2.40's onboarding reset,
which clobbers blockedCommands back to a stock 34-cmd list. If we want a
CURATED blocklist to survive that same reset, it has to live HERE — otherwise
the reset wipes it. So enforcing tier-1 = swapping this FORCE dict in.

Matching semantics (verified from command-manager.js 2026-07-16): DC blocks on
basename(argv0), lowercased, per chained segment, after stripping env-vars and
leading path. So "dd" blocks /bin/dd, "sudo dd", "a | dd", "$(dd ..)", "X=1 dd".
It does NOT match subcommands/args — this is why the list is destructive
BINARIES only, never "docker" (would kill all docker) or "rm" (would kill all rm).

TO ACTIVATE (Frank-gated):
  cp /home/lumen/bin/dc-enforce-config.py /home/lumen/bin/dc-enforce-config.py.bak.$(date +%Y%m%d_%H%M%S)
  cp /home/lumen/bin/dc-enforce-config.py.tier1-staged /home/lumen/bin/dc-enforce-config.py
  sudo systemctl restart desktop-commander-mcp     # <- reconnects the mcpw path; Frank present
TO ROLLBACK:
  cp /home/lumen/bin/dc-enforce-config.py.bak.<ts> /home/lumen/bin/dc-enforce-config.py
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
