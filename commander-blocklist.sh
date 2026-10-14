#!/bin/bash
# commander-blocklist.sh {clear|min|restore}
# Safely change Desktop Commander blockedCommands. Direct edits while the MCP runs get
# clobbered (the service re-persists its in-memory list), so this stops -> edits -> starts.
# Run from an SSH terminal (NOT through Commander itself, since it restarts the service).
set -e
# wq#3805: desktop-commander-mcp was retired on Lumen (unit masked). Never revive it.
[ "$(systemctl is-enabled desktop-commander-mcp 2>/dev/null)" = "masked" ] && { echo "desktop-commander-mcp retired on this host (masked) - nothing to do (wq#3805)"; exit 0; }
CFG=/home/lumen/.claude-server-commander/config.json
MODE="${1:-}"
case "$MODE" in clear|min|restore) ;; *) echo "usage: $0 {clear|min|restore}"; exit 1;; esac
cp "$CFG" "$CFG.bak.$(date +%Y%m%d_%H%M%S)"
systemctl stop desktop-commander-mcp.service
python3 - "$MODE" "$CFG" <<'PYEOF'
import json,sys
mode,p=sys.argv[1],sys.argv[2]
lists={
 "clear":[],
 "min":["mkfs","format","fdisk","parted","diskpart","dd","shutdown","reboot","halt","poweroff","init"],
 "restore":["mkfs","format","mount","umount","fdisk","dd","parted","diskpart","su","passwd","adduser","useradd","usermod","groupadd","chsh","visudo","shutdown","reboot","halt","poweroff","init","iptables","firewall","netsh","sfc","bcdedit","reg","net","sc","runas","cipher","takeown"],
}
d=json.load(open(p)); d["blockedCommands"]=lists[mode]
json.dump(d,open(p,"w"),indent=2)
print("blockedCommands ->",len(d["blockedCommands"]),"entries")
PYEOF
systemctl start desktop-commander-mcp.service
echo "done ($MODE). Commander restarted; reconnect the connector if it dropped."
