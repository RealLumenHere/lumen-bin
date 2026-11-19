# fleet_actions - single-definition action registry (wq#5010)

Pattern ported from BuilderIO agent-native `defineAction` (extraction wq#4969). The framework is
not adopted: no Node, no ORM, no cloud.

One `define_action(...)` declares: name, description, Pydantic input model, optional output
model, `run(args, ctx)`, typed error codes (`define_errors`), `read_only`, `needs_approval`
(bool or predicate), and which surfaces may call it. Every surface calls
`Registry.dispatch()`:

```
lookup -> exposure(ctx.caller) -> validate input -> needs_approval -> run -> validate output -> audit
```

so CLI, MCP and HTTP give byte-identical envelopes `{"ok": true, "data": ...}` /
`{"ok": false, "error": {"code", "message"}}` for the same call (tested across all four
entry points, MCP over the real protocol).

## Surfaces
```
fleet-action list | schema <name> | pins [--write]
fleet-action wq.show '{"id": 5010}'
fleet-action wq.add '{"lane":"build","title":"...","pri":1}' --approved ak_...
fleet-action mcp                 # stdio MCP server; refuses to start on pin drift
fleet-action http [--port 8765]  # 127.0.0.1 only; GET /actions, POST /actions/<name>
```
HTTP: approval key in `X-Approval-Key`, identity in `X-WQ-Agent`; 409 = approval required.
MCP: approval key as `_approval_key` in the arguments.

## Actions
| action | rw | approval |
|---|---|---|
| wq.show, wq.list, state.current | ro | - |
| wq.note | rw | - |
| wq.add | rw | P0/P1 from MCP/HTTP (those page Frank) |
| state.ingest | rw | always from MCP/HTTP (facts land in every boot context) |

Writes shell out to the real `wq` / `lumen_state.py` / `state-regen`, so their gates stay the one
implementation; a CLI refusal comes back as error `refused` with the tool's own message.
`wq.list` reads queue.db read-only because `wq list` has no data output.

## Approval
`needs_approval` true (or a predicate returning true, or a predicate that throws) stops the call
before `run()` with `approval_required` and an `approval_key` derived from the action plus the
exact validated args. Re-issue with that key to run it; any change to the args needs a new key.
It is an approval flow, not an authorization boundary.

## Pinning
`pins.json` holds a sha256 per advertised tool definition (name, description, input schema,
read_only, error codes). `fleet-action pins` names every drift (changed, new, removed); the MCP
adapter will not build on drift. After reviewing a definition change: `fleet-action pins --write`
and commit the result. The test suite fails if the committed pins do not match the code.

## Audit
One JSONL line per dispatch in `/home/lumen/agents/fleet-actions/audit.jsonl`
(`FLEET_ACTIONS_AUDIT`): time, action, caller, agent, ok, error code, and the sha256 of the
args. Args themselves are never written.

## Adding an action
Add it to `actions.py` (or a new module imported from `__init__.py`), run the tests,
`fleet-action pins --write`, commit both.

## Tests
`python3 -m pytest fleet_actions/tests` (21): identical envelopes on python/CLI/HTTP/MCP for 7
input classes; approval flow incl. changed args and throwing predicates; exposure; pin drift
blocks MCP; audit has no raw args; real wq.add/show/note/list against the real `wq` on a
scratch DB; P1-from-MCP approval; state.* with a stubbed lumen_state. Letting the MCP surface
validate on its own (`validate_input=True`) fails 3 envelope tests.
