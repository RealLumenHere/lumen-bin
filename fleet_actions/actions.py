"""wq#5010: the wq + lumen_state actions. Writes go through the real `wq` / `lumen_state.py`
CLIs, so every gate those tools enforce (claim fences, deps guard, done gates, predicate
registry) stays the one implementation; this layer adds schema, approval, pinning and audit
in front of them. Reads that the CLIs cannot emit as data use a read-only DB handle.
"""
from __future__ import annotations

import datetime as _dt
import json
import os
import re
import sqlite3
import subprocess
from typing import Literal, Optional

from pydantic import BaseModel, ConfigDict, Field, field_validator

from .core import Ctx, define_action, define_errors

WQ = os.environ.get("FLEET_ACTIONS_WQ", "/home/lumen/bin/wq")
WQ_DB = os.environ.get("WQ_DB", "/home/lumen/agents/work-queue/queue.db")
LUMEN_STATE = os.environ.get("FLEET_ACTIONS_LUMEN_STATE", "/home/lumen/minerva/lumen_state.py")
STATE_REGEN = os.environ.get("FLEET_ACTIONS_STATE_REGEN", "/home/lumen/bin/state-regen")
CLI_TIMEOUT = 120
# the values present in queue.db (2026-11-19)
STATUSES = Literal["open", "claimed", "blocked", "done", "frank_gated", "pending_review",
                   "awaiting_grant", "cancelled", "rejected", "retired"]
_SLUG = r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,79}$"


class _In(BaseModel):
    model_config = ConfigDict(extra="forbid")  # an unknown field is an error, on every surface


def _cli(argv, ctx: Ctx, errs, timeout=CLI_TIMEOUT):
    env = dict(os.environ)
    if ctx.agent:
        env["WQ_AGENT"] = ctx.agent
    try:
        p = subprocess.run(argv, capture_output=True, text=True, env=env, timeout=timeout)
    except subprocess.TimeoutExpired:
        errs.fail("cli_failed", f"{os.path.basename(argv[0])} timed out after {timeout}s")
    if p.returncode != 0:
        tail = (p.stderr or p.stdout).strip().splitlines()[-6:]
        errs.fail("cli_failed" if p.returncode == 127 else "refused",
                  f"{os.path.basename(argv[0])} rc={p.returncode}: " + " | ".join(tail)[:1500])
    return p.stdout


# --- wq.show ---------------------------------------------------------------------------
class WqShowIn(_In):
    id: int = Field(gt=0, description="ticket id (the N in wq#N)")


WQ_ERR = define_errors("not_found", "refused", "cli_failed")


def _wq_show(a: WqShowIn, ctx: Ctx):
    p = subprocess.run([WQ, "show", str(a.id), "--json"], capture_output=True, text=True,
                       timeout=CLI_TIMEOUT, env=dict(os.environ))
    if p.returncode != 0:
        WQ_ERR.fail("not_found", f"wq#{a.id}: " + (p.stderr or p.stdout).strip()[-300:])
    row = json.loads(p.stdout)
    # `wq show N --json` exits 0 with {"error": "no such task"} for a missing id
    if not isinstance(row, dict) or (set(row) == {"error"}):
        WQ_ERR.fail("not_found", f"wq#{a.id}: {row.get('error') if isinstance(row, dict) else row}")
    return row


define_action(
    name="wq.show", input=WqShowIn, run=_wq_show, read_only=True, errors=WQ_ERR,
    description="Read one work-queue ticket (wq#N): title, detail, status, owner, notes.")


# --- wq.list ---------------------------------------------------------------------------
class WqListIn(_In):
    status: Optional[STATUSES] = Field(None, description="filter by status")
    lane: Optional[str] = Field(None, pattern=r"^[a-z0-9_-]{1,40}$", description="filter by lane")
    limit: int = Field(50, ge=1, le=500)


class WqRow(BaseModel):
    id: int
    lane: str
    pri: int
    status: str
    title: str
    claimed_by: Optional[str] = None


class WqListOut(BaseModel):
    tickets: list[WqRow]


def _wq_list(a: WqListIn, ctx: Ctx):
    q, params = "SELECT id, lane, pri, status, title, claimed_by FROM tasks WHERE 1=1", []
    if a.status:
        q += " AND status=?"
        params.append(a.status)
    if a.lane:
        q += " AND lane=?"
        params.append(a.lane)
    q += " ORDER BY pri ASC, id DESC LIMIT ?"
    params.append(a.limit)
    try:
        c = sqlite3.connect(f"file:{WQ_DB}?mode=ro", uri=True, timeout=10)
        try:
            rows = c.execute(q, params).fetchall()
        finally:
            c.close()
    except sqlite3.Error as e:
        WQ_ERR.fail("cli_failed", f"queue.db read failed: {e}")
    return {"tickets": [dict(zip(("id", "lane", "pri", "status", "title", "claimed_by"), r))
                        for r in rows]}


define_action(
    name="wq.list", input=WqListIn, output=WqListOut, run=_wq_list, read_only=True,
    errors=WQ_ERR,
    description="List work-queue tickets, highest priority first, filtered by status and/or lane.")


# --- wq.note ---------------------------------------------------------------------------
class WqNoteIn(_In):
    id: int = Field(gt=0)
    text: str = Field(min_length=1, max_length=2000, description="note appended to the ticket")


def _wq_note(a: WqNoteIn, ctx: Ctx):
    _cli([WQ, "note", str(a.id), a.text], ctx, WQ_ERR)
    return {"id": a.id, "noted": True}


define_action(
    name="wq.note", input=WqNoteIn, run=_wq_note, errors=WQ_ERR,
    description="Append a timestamped note to a work-queue ticket.")


# --- wq.add ----------------------------------------------------------------------------
class WqAddIn(_In):
    lane: str = Field(pattern=r"^[a-z0-9_-]{1,40}$")
    title: str = Field(min_length=8, max_length=200)
    detail: str = Field("", max_length=8000)
    pri: int = Field(2, ge=0, le=3, description="0 = P0 ... 3 = P3")
    deps: list[int] = Field(default_factory=list, description="ticket ids this one depends on")

    @field_validator("deps")
    @classmethod
    def _pos(cls, v):
        if any(d <= 0 for d in v):
            raise ValueError("deps must be positive ticket ids")
        return v


def _wq_add(a: WqAddIn, ctx: Ctx):
    argv = [WQ, "add", "--lane", a.lane, "--pri", str(a.pri), "--title", a.title]
    if a.detail:
        argv += ["--detail", a.detail]
    if a.deps:
        argv += ["--deps", ",".join(map(str, a.deps))]
    out = _cli(argv, ctx, WQ_ERR)
    m = re.search(r"added task (\d+)", out)
    if not m:
        WQ_ERR.fail("cli_failed", f"wq add printed no id: {out.strip()[-200:]}")
    return {"id": int(m.group(1))}


def _add_needs_approval(a: WqAddIn, ctx: Ctx) -> bool:
    # P0/P1 tickets page Frank; a model on an outside surface may not file one unreviewed.
    return a.pri <= 1 and ctx.caller in ("mcp", "http")


define_action(
    name="wq.add", input=WqAddIn, run=_wq_add, errors=WQ_ERR, needs_approval=_add_needs_approval,
    description="File a new work-queue ticket. P0/P1 from MCP or HTTP needs human approval.")


# --- state.current ---------------------------------------------------------------------
class StateCurrentIn(_In):
    subject: Optional[str] = Field(None, pattern=_SLUG, description="e.g. gitea, thor")
    predicate: Optional[str] = Field(None, pattern=_SLUG)
    limit: int = Field(100, ge=1, le=1000)

    @field_validator("predicate")
    @classmethod
    def _needs_subject(cls, v, info):
        if v and not info.data.get("subject"):
            raise ValueError("predicate requires subject")
        return v


STATE_ERR = define_errors("refused", "cli_failed")


def _state_current(a: StateCurrentIn, ctx: Ctx):
    argv = ["python3", LUMEN_STATE, "current", "--limit", str(a.limit)]
    argv += [x for x in (a.subject, a.predicate) if x]
    out = _cli(argv, ctx, STATE_ERR)
    return {"facts": [ln.rstrip() for ln in out.splitlines() if ln.strip()]}


define_action(
    name="state.current", input=StateCurrentIn, run=_state_current, read_only=True,
    errors=STATE_ERR,
    description="Current fleet facts from the lumen_state bi-temporal store, optionally for one "
                "subject (and predicate).")


# --- state.ingest ----------------------------------------------------------------------
class StateIngestIn(_In):
    subject: str = Field(pattern=_SLUG)
    predicate: str = Field(pattern=_SLUG)
    value: str = Field(min_length=1, max_length=2000)
    valid_from: _dt.date
    source: str = Field(min_length=3, max_length=300, description="where this fact was verified")


def _state_ingest(a: StateIngestIn, ctx: Ctx):
    _cli(["python3", LUMEN_STATE, "ingest", a.subject, a.predicate, a.value,
          "--valid-from", a.valid_from.isoformat(), "--source", a.source], ctx, STATE_ERR)
    _cli([STATE_REGEN], ctx, STATE_ERR, timeout=300)
    return {"subject": a.subject, "predicate": a.predicate, "regenerated": True}


define_action(
    name="state.ingest", input=StateIngestIn, run=_state_ingest, errors=STATE_ERR,
    # a fact written from an outside surface lands in every future session's boot context
    needs_approval=lambda a, ctx: ctx.caller in ("mcp", "http"),
    description="Record a changed fleet fact in lumen_state and regenerate LIVE_FLEET_STATE. "
                "From MCP or HTTP it needs human approval.")
