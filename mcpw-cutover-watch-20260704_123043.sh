#!/usr/bin/env bash
for i in $(seq 1 72); do [ -f "/home/lumen/cutover-OK-20260704_123043" ] && { logger -t mcpw-cutover committed; exit 0; }; sleep 5; done
logger -t mcpw-cutover "AUTO-REVERT: unconfirmed 6min -> stateless"
sudo cp "/etc/systemd/system/desktop-commander-mcp.service.revert-20260704_123043" "/etc/systemd/system/desktop-commander-mcp.service"; cp "/home/lumen/.npm-global/lib/node_modules/supergateway/dist/gateways/stdioToStatefulStreamableHttp.js.bak.20260704_123043" "/home/lumen/.npm-global/lib/node_modules/supergateway/dist/gateways/stdioToStatefulStreamableHttp.js"; sudo systemctl daemon-reload; sudo systemctl restart desktop-commander-mcp.service
