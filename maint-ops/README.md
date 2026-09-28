# maint-ops

Op scripts for lumen-window batches (wq#1401/#1402). lumen-window runs `cmd`/`verify`/`precheck` as local bash on Lumen
and halts on failure without rolling back, so each op here carries its own precheck, apply, verify and rollback.

Exit codes: 0 ok, 1 failed and rolled back cleanly, 2 failed and rollback failed (needs a human), 3 precheck abort.

## litellm-upgrade
`--precheck | --apply | --verify | --rollback`. Pins the digest of the LiteLLM patch release in the compose file
(default: v1.83.10-stable.patch.3, fixes GHSA-4xpc-pv4p-pm3w), dumps the litellm DB, recreates only the litellm service,
then verifies with the maint-smoke key (models, a real completion, an out-of-scope model must be refused) and the
host-header fix marker in the running code. The patch reports version 1.83.10, so the version string is not a check.
Test to failure with `tests/harness_litellm.sh` (throwaway compose project on 127.0.0.1:4002 and a scratch DB copy; never touches prod).

Note: verify never makes a deliberate 401 on prod (NEG_CONTROL=0). litellm-auth-alert pages at 5 auth_401 in 10 minutes, and an early run of this op tripped it. The harness sets NEG_CONTROL=1 (its port is not monitored) to keep the scope-enforcement assertion.

## tailscale-upgrade
`--precheck | --apply | --verify | --rollback <name> <user> <lan_ip> <ts_ip>`, one op per node, so a rolling upgrade is one queued op per node.
Always controls the node over its LAN ip, so restarting tailscaled cannot kill the session; the tailnet path is only verified afterwards.
apt runs detached on the node (systemd-run --wait). Precheck requires the target as apt candidate and the current package still in apt (so a
downgrade rollback exists). Refuses Thor. Never reboots. Test to failure with `tests/tailscale_canary.sh` on an idle node (default wadjet):
Thor refusal, rollback with no state, forced verify failure that must downgrade, real upgrade, idempotent re-run. 9 checks.
Vault-adjacent nodes (maat, sobek), Lumen and cloudflared hosts are control-plane: queue them with `--tags control-plane`, Frank drives.
