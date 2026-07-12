#!/usr/bin/env python3
"""Enforce Desktop Commander canonical config on every gateway start.
Defeats v0.2.40 onboarding-reset (which clobbers blockedCommands back to the
stock 34-cmd list and zeroes usageStats). Run as ExecStartPre of
desktop-commander-mcp.service. Idempotent. Preserves clientId/usageStats if
present; forces the three keys Frank requires.

Frank's directive 2026-06-26: ZERO command restrictions. blockedCommands MUST
stay []. Do not re-add a trimmed list. This script is the persistence layer."""
import json, os
from pathlib import Path

CFG = Path("/home/lumen/.claude-server-commander/config.json")
FORCE = {
    "blockedCommands": [],          # Frank directive: no restrictions, ever
    "pendingWelcomeOnboarding": False,  # the reset trigger -> off
    "telemetryEnabled": False,
    "allowedDirectories": [],
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
    print(f"[dc-enforce] blockedCommands={cfg.get('blockedCommands')} "
          f"onboarding={cfg.get('pendingWelcomeOnboarding')}")

if __name__ == "__main__":
    main()
