# maint-ops

Op scripts for lumen-window batches (wq#1401/#1402). lumen-window runs `cmd`/`verify`/`precheck` as local bash on Lumen
and halts on failure without rolling back, so each op here carries its own precheck, apply, verify and rollback.

Exit codes: 0 ok, 1 failed and rolled back cleanly, 2 failed and rollback failed (needs a human), 3 precheck abort.

## litellm-upgrade
`--precheck | --apply | --verify | --rollback`. Pins the digest of the target LiteLLM release in the compose file
(default TARGET_DIGEST: v1.96.2, fixes GHSA-3cv6-jpf6-8222; FALLBACK_DIGEST: v1.88.6, also carries the fix; prod is
already on v1.83.10-stable.patch.3 which fixed GHSA-4xpc-pv4p-pm3w), dumps the litellm DB, recreates only the litellm service,
then verifies with the maint-smoke key (models, a real completion, an out-of-scope model must be refused) and the
fix markers in the running code (host-header `request.scope`; `reject_url_valued_destination` for the target). The API does not
report the version, so the running code is the check. The package path differs by image (`/app/litellm` on patch.3, a venv
under `/app/.venv/...` on v1.96.x), so verify asks the container (`pkg_dir`) instead of hardcoding it.
Test to failure with `tests/harness_litellm.sh` (throwaway compose project on 127.0.0.1:4002 and a scratch DB copy; never touches prod).

Note: verify never makes a deliberate 401 on prod (NEG_CONTROL=0). litellm-auth-alert pages at 5 auth_401 in 10 minutes, and an early run of this op tripped it. The harness sets NEG_CONTROL=1 (its port is not monitored) to keep the scope-enforcement assertion.

## tailscale-upgrade
`--precheck | --apply | --verify | --rollback <name> <user> <lan_ip> <ts_ip>`, one op per node, so a rolling upgrade is one queued op per node.
Always controls the node over its LAN ip, so restarting tailscaled cannot kill the session; the tailnet path is only verified afterwards.
apt runs detached on the node (systemd-run --wait). Precheck requires the target as apt candidate and the current package still in apt (so a
downgrade rollback exists). Refuses Thor. Never reboots. Test to failure with `tests/tailscale_canary.sh` on an idle node (default wadjet):
Thor refusal, rollback with no state, forced verify failure that must downgrade, real upgrade, idempotent re-run. 9 checks.
Vault-adjacent nodes (maat, sobek), Lumen and cloudflared hosts are control-plane: queue them with `--tags control-plane`, Frank drives.

## ha-upgrade
`--precheck | --apply | --verify | --rollback`. Home Assistant container on Bes (default 2026.7.2 -> 2026.9.4), driven over Bes's LAN ip.
Order: precheck (registry tag, disk, no influx in HA YAML, live E-plug baseline) -> pull (no downtime) -> tag the old image `ha-rollback:<ver>-<ts>` ->
`check_config` with the NEW image on a copy of the config (bad config aborts with rc 3, nothing changed) -> stop only homeassistant -> tar the config ->
pin compose to the target tag -> `up -d --no-deps --pull never` -> verify (HTTP 200, running version, real E-plug read via eplug-fetch.py).
Any failure restores config from the tar (failed config kept as `homeassistant.failed-<ts>`), compose from its `.bak`, and the old image, then verifies the old version.
Kept afterwards: `_ha_backups/ha-config-<ts>.tar.gz`, the rollback image tag, `compose.yaml.bak.<ts>.ha` (prune by hand after a week).
Test to failure with `tests/ha_canary.sh` (throwaway `ha-canary` on Bes :18123, own compose project; asserts prod HA start time and image unchanged; 21 checks:
no-state rollback, precheck, broken-config gate, forced verify failure with config/compose restore, real upgrade, idempotent re-apply, manual rollback).
Est ~4 min for prod (894 MB config). HA is the plug controller: run it in a fleet window, never mid-experiment.

## ollama-upgrade
`--precheck | --apply | --verify | --rollback <name> <user> <lan_ip>`, one op per node, so a rolling upgrade is one queued op per node
(lumen, isis, nephthys 0.32.6 -> 0.34.4; hathor 0.30.7 -> 0.34.4). Controls the node over its LAN ip; restarting `ollama.service` never touches
the ssh session or the tailnet, unlike tailscaled.
Never runs the vendor `install.sh` (it rewrites the systemd unit and can drop our OLLAMA_MODELS/KEEP_ALIVE/MAX_LOADED_MODELS/NUM_PARALLEL env
overrides - manifest gap G6, wq#2992). Instead follows docs.ollama.com/linux's "Manual install"/"Updating" path: precheck downloads and
sha256-verifies the pinned release tarball (`ollama-linux-amd64.tar.zst`) into a node-local cache; apply backs up the binary, `lib/ollama`, the
unit and any drop-ins into one tar, stops the service, wipes the old libs and extracts the new tarball (never touching `/etc/systemd`), diffs the
unit's sha256 before/after to prove it was untouched, starts the service, then verifies with a REAL generation (`/api/generate`, not just
`/api/version`) against a pulled model - `first_model` skips embedding-only models (e.g. `nomic-embed-text`) by checking `/api/show`'s
`capabilities` for `completion`. Any failure restores the backup tar (also wiping libs first) and verifies the old version.
Kept afterwards: `/var/backups/ollama-upgrade/ollama-<name>-<ts>.tar.gz` per run (prune by hand), `/tmp/ollama-upgrade-cache` (reused across runs).
Test to failure with `tests/ollama_canary.sh` (throwaway `ollama-canary` on wadjet :11435, own prefix `/opt/ollama-canary`, starts on real 0.32.6):
no-state rollback, precheck, forced verify failure rolls back, real upgrade to 0.34.4 with a real generation, idempotent re-apply, manual rollback.
Hathor had model pulls a few hours before the manifest probe (wq#1402): check idle before touching it, same as HA's fleet-window rule.

## dc-upgrade
`--precheck | --apply | --verify | --rollback <n> <user> <lan_ip>` (wq#1509). Upgrades @wonderwhy-er/desktop-commander on a `<node>-mcp-failover` host (bes today; nephthys's unit is deliberately stopped+disabled, so its precheck refuses).
Refuses lumen (the primary gateway Commander rides, Frank-driven via dc-narrow-deploy) and thor. `TARGET_VERSION` (default 0.2.46) selects a bundle in `bin/maint-ops/dc-upgrade.d/<version>/`
(Lumen's live patched dist files: command-manager sudo-unwrap, config-manager atomic write, terminal-manager + handle-store detach, plus manifest.env with pristine and patched sha256).
Flow: precheck (node>=22.5 for node:sqlite, service healthy, registry has the version, disk) -> `npm install -g --prefix stage --ignore-scripts` (skips DC's telemetry postinstall; ripgrep comes from the
`@vscode/ripgrep-linux-x64` optional dep) -> REFUSE unless the staged pristine files match the bundle's recorded sha256 (the patches are line-number diffs, only valid on that exact base) -> overlay the four
files -> throwaway supergateway on :3199 with a real MCP round trip (`dc_mcp_probe.py`: initialize, tools/list, start_process echo nonce) -> swap module dir, restart the service, re-probe on :3100.
Any failure after the swap restores the old module dir and re-verifies it. Old module kept at `~/.dc-upgrade/backup-<ts>` on the node (prune by hand after a week).
Proven on bes 2026-09-30: forced verify failure (`FORCE_VERIFY_FAIL=1`) rolled back cleanly to 0.2.40 with a real round trip, then the real 0.2.40 -> 0.2.46 apply passed.
To support a new DC version: add a bundle dir (re-cut the patches against that version's pristine files, record shas), test it on Lumen scratch with `staged_canary.sh`, and Lumen goes before any failover.
