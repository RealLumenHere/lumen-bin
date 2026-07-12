#!/usr/bin/env bash
set -uo pipefail
[ "$(hostname)" = lumen ] || { echo "REFUSING: not lumen ($(hostname))"; exit 9; }
SG=/home/lumen/.npm-global/lib/node_modules/supergateway/dist/gateways/stdioToStatefulStreamableHttp.js
UNIT=/etc/systemd/system/desktop-commander-mcp.service
TS=$(date +%Y%m%d_%H%M%S); SENTINEL=/home/lumen/cutover-OK-$TS; REVERT_UNIT=$UNIT.revert-$TS
WORK=/home/lumen/bets/interview-prep-uplift/work; mkdir -p "$WORK"
printf 'TS=%s\nSENTINEL=%s\nSG=%s\nUNIT=%s\nREVERT_UNIT=%s\nSG_BAK=%s\n' "$TS" "$SENTINEL" "$SG" "$UNIT" "$REVERT_UNIT" "$SG.bak.$TS" | tee "$WORK/cutover-last.env"
echo "== 0 preflight =="; /home/lumen/.npm-global/bin/supergateway --help 2>&1 | grep -q -- '--stateful' || { echo "ABORT: no --stateful flag"; exit 2; }
echo "== 1 backups =="; cp "$SG" "$SG.bak.$TS"; sudo cp "$UNIT" "$REVERT_UNIT"; sudo cp "$UNIT" "$UNIT.bak.$TS"
echo "== 2 patch 400->404 =="; sed -i "s#res.status(400).json(#res.status(404).json(#; s#res.status(400).send('Invalid or missing session ID')#res.status(404).send('Invalid or missing session ID')#" "$SG"
[ "$(grep -c 'res.status(400)' "$SG")" = "0" ] || { echo "ABORT patch"; cp "$SG.bak.$TS" "$SG"; exit 3; }
echo "== 3 add --stateful =="; sudo sed -i 's#--outputTransport streamableHttp#--stateful --outputTransport streamableHttp#' "$UNIT"
sudo grep -q -- '--stateful --outputTransport' "$UNIT" || { echo "ABORT unit"; sudo cp "$REVERT_UNIT" "$UNIT"; cp "$SG.bak.$TS" "$SG"; exit 3; }
echo "== 4 arm 6min auto-revert =="; WATCH=/home/lumen/bin/mcpw-cutover-watch-$TS.sh
cat > "$WATCH" <<WATCHEOF
#!/usr/bin/env bash
for i in \$(seq 1 72); do [ -f "$SENTINEL" ] && { logger -t mcpw-cutover committed; exit 0; }; sleep 5; done
logger -t mcpw-cutover "AUTO-REVERT: unconfirmed 6min -> stateless"
sudo cp "$REVERT_UNIT" "$UNIT"; cp "$SG.bak.$TS" "$SG"; sudo systemctl daemon-reload; sudo systemctl restart desktop-commander-mcp.service
WATCHEOF
chmod +x "$WATCH"; sudo systemd-run --unit=mcpw-cutover-watch-$TS --collect "$WATCH" && echo "watcher armed" || echo "WARN watcher failed"
echo "== 5 FLIP (drops MCP ~4s) =="; sudo systemctl daemon-reload; sudo systemctl restart desktop-commander-mcp.service; sleep 4
echo "== 6 health gate =="; curl -fsS http://localhost:3100/health >/dev/null 2>&1 && echo "health OK" || echo "health FAIL"
curl -s -D - -o /dev/null -X POST http://localhost:3100/mcp -H 'Content-Type: application/json' -H 'Accept: application/json, text/event-stream' -d '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-06-18","capabilities":{},"clientInfo":{"name":"c","version":"1"}}}' | grep -qi 'mcp-session-id' && echo "session-id ISSUED (good)" || echo "WARN no session-id"
echo "unknown-session -> HTTP $(curl -s -o /dev/null -w '%{http_code}' -X POST http://localhost:3100/mcp -H 'Content-Type: application/json' -H 'Accept: application/json, text/event-stream' -H 'mcp-session-id: bogus' -d '{"jsonrpc":"2.0","id":2,"method":"tools/list"}') (want 404)"
echo; echo "FLIP DONE. Auto-revert in ~6min unless confirmed. To commit after Claude verifies: touch $SENTINEL"
echo "Manual revert: sudo cp $REVERT_UNIT $UNIT && cp $SG.bak.$TS $SG && sudo systemctl daemon-reload && sudo systemctl restart desktop-commander-mcp.service"
