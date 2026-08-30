#!/usr/bin/env bash
# wq#345: export per-node hardware/power inventory from lumen_state -> push to Bes for Grafana
set -euo pipefail
OUT=/tmp/lumen-inventory
/usr/bin/python3 /home/lumen/power-dash/staged/power_inventory_export.py --out "$OUT" >/dev/null 2>&1
ssh -o ConnectTimeout=6 bes@100.110.156.91 'mkdir -p /home/bes/power-dash/inventory'
rsync -az --delete -e 'ssh -o ConnectTimeout=6' "$OUT"/ bes@100.110.156.91:/home/bes/power-dash/inventory/
# wq#384: second target feeds Grafana static serving (/usr/share/grafana/public/inventory symlink)
rsync -az --delete -e 'ssh -o ConnectTimeout=6' "$OUT"/ bes@100.110.156.91:/var/lib/power-inventory/
