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


LKG = CFG.with_suffix(".json.lkg")   # last-known-good snapshot, refreshed by dc-config-guard on every clean read


def _load(path):
    try:
        t = path.read_text()
        return json.loads(t) if t.strip() else None
    except Exception:
        return None


def _atomic_write(path, data):
    # dc-config-atomic (2026-09-08): never leave the file observably empty.
    import os, tempfile
    fd, tmp = tempfile.mkstemp(prefix=path.name + ".enforce.", dir=str(path.parent))
    with os.fdopen(fd, "w") as f:
        f.write(data)
        f.flush()
        os.fsync(f.fileno())
    os.chmod(tmp, 0o644)
    st = os.stat(path.parent)
    os.chown(tmp, st.st_uid, st.st_gid)   # guard runs as root; keep the file owned by lumen
    os.replace(tmp, path)


def main():
    CFG.parent.mkdir(parents=True, exist_ok=True)
    cfg = _load(CFG) if CFG.exists() else None
    src = "live"
    if cfg is None:
        # Empty/garbage config: DO NOT clobber down to FORCE keys (that drops
        # clientId/version and makes the next child rewrite them -> guard churn).
        cfg = _load(LKG) if LKG.exists() else None
        src = "lkg" if cfg is not None else "empty"
        cfg = cfg or {}
    cfg.update(FORCE)
    _atomic_write(CFG, json.dumps(cfg, indent=2))
    print(f"[dc-enforce:tier1] blockedCommands={len(cfg.get('blockedCommands'))} "
          f"onboarding={cfg.get('pendingWelcomeOnboarding')} base={src}")


if __name__ == "__main__":
    main()
