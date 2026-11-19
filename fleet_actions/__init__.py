"""wq#5010 fleet action registry: one definition per action, one dispatch pipeline, three
surfaces (CLI / MCP / HTTP). See README.md."""
from .core import (REGISTRY, Action, ActionError, Ctx, Registry, Result, approval_key,  # noqa: F401
                   define_action, define_errors)
from . import actions  # noqa: F401  (registers the wq.* and state.* actions)
