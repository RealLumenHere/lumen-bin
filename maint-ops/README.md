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
