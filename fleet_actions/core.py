"""wq#5010: single-definition action registry (pattern ported from BuilderIO agent-native,
wq#4969; the framework itself is not adopted).

One `define_action(...)` = one Pydantic input model, one run(), one set of typed error codes,
one approval rule. Every surface (CLI, MCP, HTTP) calls `dispatch()`, so validation, approval,
output checking and the audit line are the same code path whichever door the call came in by.

Pipeline:  lookup -> exposure(ctx.caller) -> validate input -> needs_approval -> run()
           -> validate output -> audit  ->  Result(ok, data | error{code,message})

Tool-definition pinning: `catalog()` is the exact JSON every surface advertises (name,
description, input schema, read_only). `catalog_digest()` hashes each entry; `pins.json`
records the reviewed digests; `verify_pins()` names every drift. The MCP adapter refuses to
serve on drift, so a changed description or schema cannot reach a model without a reviewed
pin update (the rug-pull class wq#4941 pins guard launchers against).
"""
from __future__ import annotations

import datetime as _dt
import hashlib
import json
import os
from dataclasses import dataclass, field
from typing import Any, Callable, Optional, Union

from pydantic import BaseModel, ValidationError

SURFACES = ("cli", "mcp", "http", "python")
AUDIT_LOG = os.environ.get("FLEET_ACTIONS_AUDIT",
                           "/home/lumen/agents/fleet-actions/audit.jsonl")
PINS_PATH = os.environ.get("FLEET_ACTIONS_PINS",
                           os.path.join(os.path.dirname(os.path.abspath(__file__)), "pins.json"))

# Error codes every action may raise in addition to its own declared ones.
BUILTIN_ERRORS = frozenset({"invalid_input", "unknown_action", "not_exposed",
                            "approval_required", "invalid_output", "internal"})


class ActionError(Exception):
    def __init__(self, code: str, message: str = ""):
        super().__init__(f"{code}: {message}")
        self.code, self.message = code, message


class ErrorCodes:
    """define_errors('not_found', 'locked') -> E; E.fail('not_found', msg) raises; an
    undeclared code is a programming error and raises ValueError, never a silent new code."""

    def __init__(self, *codes: str):
        bad = [c for c in codes if c in BUILTIN_ERRORS]
        if bad:
            raise ValueError(f"error codes shadow built-ins: {bad}")
        self.codes = frozenset(codes)

    def fail(self, code: str, message: str = ""):
        if code not in self.codes:
            raise ValueError(f"undeclared error code {code!r}; declared: {sorted(self.codes)}")
        raise ActionError(code, message)


def define_errors(*codes: str) -> ErrorCodes:
    return ErrorCodes(*codes)


@dataclass(frozen=True)
class Ctx:
    caller: str                      # one of SURFACES
    agent: str = ""                  # WQ_AGENT-style identity, passed through to wrapped CLIs
    approved: frozenset = frozenset()  # approval keys the human already granted

    def __post_init__(self):
        if self.caller not in SURFACES:
            raise ValueError(f"unknown caller {self.caller!r}")


@dataclass
class Action:
    name: str
    description: str
    input: type[BaseModel]
    run: Callable[[BaseModel, Ctx], Any]
    output: Optional[type[BaseModel]] = None
    read_only: bool = False
    needs_approval: Union[bool, Callable[[BaseModel, Ctx], bool]] = False
    errors: ErrorCodes = field(default_factory=ErrorCodes)
    surfaces: frozenset = frozenset(SURFACES)

    def tool_def(self) -> dict:
        return {"name": self.name, "description": self.description,
                "input_schema": self.input.model_json_schema(), "read_only": self.read_only,
                "errors": sorted(self.errors.codes)}


@dataclass
class Result:
    ok: bool
    data: Any = None
    error: Optional[dict] = None

    def to_dict(self) -> dict:
        return {"ok": True, "data": self.data} if self.ok else {"ok": False, "error": self.error}


class Registry:
    def __init__(self):
        self.actions: dict[str, Action] = {}

    def define(self, **kw) -> Action:
        a = Action(**kw)
        if not a.name or not a.name.replace("_", "").replace(".", "").isalnum():
            raise ValueError(f"bad action name {a.name!r}")
        if a.name in self.actions:
            raise ValueError(f"duplicate action {a.name!r}")
        if not a.description.strip():
            raise ValueError(f"{a.name}: description is required (it is the tool definition)")
        self.actions[a.name] = a
        return a

    # --- pinning -------------------------------------------------------------------------
    def catalog(self, caller: Optional[str] = None) -> list[dict]:
        return [a.tool_def() for a in sorted(self.actions.values(), key=lambda x: x.name)
                if caller is None or caller in a.surfaces]

    def catalog_digest(self) -> dict[str, str]:
        return {d["name"]: hashlib.sha256(json.dumps(d, sort_keys=True).encode()).hexdigest()
                for d in self.catalog()}

    def verify_pins(self, path: Optional[str] = None) -> list[str]:
        """Drift list; empty means every advertised definition matches its reviewed pin."""
        path = path or PINS_PATH
        try:
            with open(path) as f:
                pinned = json.load(f)
        except FileNotFoundError:
            return [f"no pins file at {path}"]
        except ValueError as e:
            return [f"unreadable pins file {path}: {e}"]
        live = self.catalog_digest()
        drift = [f"unpinned action {n}" for n in sorted(set(live) - set(pinned))]
        drift += [f"pinned action {n} no longer defined" for n in sorted(set(pinned) - set(live))]
        drift += [f"definition of {n} changed since pinned" for n in sorted(set(live) & set(pinned))
                  if live[n] != pinned[n]]
        return drift

    def write_pins(self, path: Optional[str] = None) -> None:
        with open(path or PINS_PATH, "w") as f:
            json.dump(self.catalog_digest(), f, indent=2, sort_keys=True)
            f.write("\n")

    # --- the one pipeline ----------------------------------------------------------------
    def dispatch(self, name: str, raw_args: Any, ctx: Ctx) -> Result:
        a = self.actions.get(name)
        if a is None:
            return self._done(name, ctx, raw_args, _err("unknown_action", f"no action {name!r}"))
        if ctx.caller not in a.surfaces:
            return self._done(name, ctx, raw_args,
                              _err("not_exposed", f"{name} is not exposed to {ctx.caller}"))
        if raw_args is None:
            raw_args = {}
        try:
            args = a.input.model_validate(raw_args)
        except ValidationError as e:
            return self._done(name, ctx, raw_args, _err("invalid_input", _short_ve(e)))
        key = approval_key(name, args)
        try:
            gated = a.needs_approval(args, ctx) if callable(a.needs_approval) \
                else bool(a.needs_approval)
        except Exception:
            gated = True  # a predicate that throws pauses; it never waves the call through
        if gated and key not in ctx.approved:
            return self._done(name, ctx, args, Result(False, error={
                "code": "approval_required", "approval_key": key,
                "message": f"{name} needs human approval; re-issue with approval key {key}"}))
        try:
            out = a.run(args, ctx)
        except ActionError as e:
            if e.code not in a.errors.codes and e.code not in BUILTIN_ERRORS:
                return self._done(name, ctx, args, _err("internal",
                                                        f"undeclared error code {e.code!r}"))
            return self._done(name, ctx, args, _err(e.code, e.message))
        except Exception as e:  # noqa: BLE001 - the envelope is the contract
            return self._done(name, ctx, args, _err("internal", f"{type(e).__name__}: {e}"))
        if a.output is not None:
            try:
                out = a.output.model_validate(out).model_dump(mode="json")
            except ValidationError as e:
                return self._done(name, ctx, args, _err("invalid_output", _short_ve(e)))
        elif isinstance(out, BaseModel):
            out = out.model_dump(mode="json")
        return self._done(name, ctx, args, Result(True, data=out))

    def _done(self, name, ctx, args, res: Result) -> Result:
        audit(name, ctx, args, res, self.actions.get(name))
        return res


def _err(code, message) -> Result:
    return Result(False, error={"code": code, "message": message})


def _short_ve(e: ValidationError) -> str:
    return "; ".join(f"{'.'.join(str(p) for p in d['loc']) or '<root>'}: {d['msg']}"
                     for d in e.errors()[:8])


def approval_key(name: str, args: BaseModel) -> str:
    """Derived from action + exact validated args: a changed call needs a new approval."""
    blob = json.dumps({"a": name, "args": args.model_dump(mode="json")}, sort_keys=True)
    return "ak_" + hashlib.sha256(blob.encode()).hexdigest()[:20]


def audit(name, ctx, args, res: Result, action: Optional[Action]) -> None:
    """One JSONL line per dispatch. Args are recorded as their sha256 only: actions carry
    ticket text and state values, and the audit log must never become a secret store."""
    try:
        argj = args.model_dump(mode="json") if isinstance(args, BaseModel) else args
        rec = {"ts": _dt.datetime.now(_dt.timezone.utc).isoformat(timespec="seconds"),
               "action": name, "caller": ctx.caller, "agent": ctx.agent,
               "read_only": bool(action and action.read_only),
               "args_sha256": hashlib.sha256(
                   json.dumps(argj, sort_keys=True, default=str).encode()).hexdigest(),
               "ok": res.ok, "error": (res.error or {}).get("code")}
        os.makedirs(os.path.dirname(AUDIT_LOG), exist_ok=True)
        with open(AUDIT_LOG, "a") as f:
            f.write(json.dumps(rec, sort_keys=True) + "\n")
    except Exception:
        pass  # audit trouble never changes the answer


REGISTRY = Registry()
define_action = REGISTRY.define
