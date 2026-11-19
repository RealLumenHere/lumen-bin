"""wq#5010: thin surfaces over Registry.dispatch. None of them validates, approves or audits on
its own; each only turns its wire format into (name, args, Ctx) and the Result back.

  CLI   fleet-action list | schema <name> | pins [--write] | <name> '<json>' [--approved KEY]
  MCP   build_mcp_server(registry)  - refuses to build on pin drift
  HTTP  serve_http(registry, port)  - 127.0.0.1 only; GET /actions, POST /actions/<name>
"""
from __future__ import annotations

import json
import os
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from .core import REGISTRY, Ctx, Registry

HTTP_STATUS = {"invalid_input": 400, "unknown_action": 404, "not_found": 404,
               "not_exposed": 403, "approval_required": 409, "refused": 422,
               "invalid_output": 500, "internal": 500, "cli_failed": 502}


class PinDrift(RuntimeError):
    pass


# --- CLI -------------------------------------------------------------------------------
def cli_main(argv=None, registry: Registry = REGISTRY, out=sys.stdout) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if not argv or argv[0] in ("-h", "--help"):
        print(__doc__.strip().splitlines()[2].strip(), file=out)
        return 0 if argv else 2
    cmd = argv.pop(0)
    if cmd == "list":
        for d in registry.catalog("cli"):
            print(f"{d['name']:16s} {'ro' if d['read_only'] else 'rw'}  {d['description']}",
                  file=out)
        return 0
    if cmd == "schema":
        a = registry.actions.get(argv[0] if argv else "")
        if not a:
            print("unknown action", file=sys.stderr)
            return 2
        print(json.dumps(a.tool_def(), indent=2), file=out)
        return 0
    if cmd == "pins":
        if argv[:1] == ["--write"]:
            registry.write_pins()
            print("pins written", file=out)
            return 0
        drift = registry.verify_pins()
        print("\n".join(drift) if drift else "pins OK", file=out)
        return 1 if drift else 0
    approved = set()
    raw = "{}"
    while argv:
        tok = argv.pop(0)
        if tok == "--approved" and argv:
            approved.add(argv.pop(0))
        else:
            raw = tok
    try:
        args = json.loads(raw)
    except ValueError as e:
        args = {"__unparseable_json__": str(e)}  # still goes through dispatch -> invalid_input
    res = registry.dispatch(cmd, args, Ctx("cli", agent=os.environ.get("WQ_AGENT", ""),
                                           approved=frozenset(approved)))
    print(json.dumps(res.to_dict(), indent=2, default=str), file=out)
    return 0 if res.ok else (3 if res.error["code"] == "approval_required" else 1)


# --- MCP -------------------------------------------------------------------------------
def build_mcp_server(registry: Registry = REGISTRY, check_pins: bool = True):
    """A low-level mcp Server whose tool list IS registry.catalog('mcp'). Refuses to build
    when any definition drifted from pins.json: descriptions reach models only once reviewed."""
    from mcp.server.lowlevel import Server
    import mcp.types as types

    if check_pins:
        drift = registry.verify_pins()
        if drift:
            raise PinDrift("tool definitions drifted from pins.json: " + "; ".join(drift))
    server = Server("fleet-actions")

    @server.list_tools()
    async def _list():
        return [types.Tool(name=d["name"], description=d["description"],
                           inputSchema=d["input_schema"],
                           annotations=types.ToolAnnotations(readOnlyHint=d["read_only"]))
                for d in registry.catalog("mcp")]

    @server.call_tool(validate_input=False)  # validation is dispatch's job, on every surface
    async def _call(name, arguments):
        args = dict(arguments or {})
        approved = args.pop("_approval_key", None)
        res = registry.dispatch(name, args, Ctx(
            "mcp", agent=os.environ.get("WQ_AGENT", ""),
            approved=frozenset([approved]) if approved else frozenset()))
        payload = json.dumps(res.to_dict(), default=str)
        if not res.ok:
            return types.CallToolResult(content=[types.TextContent(type="text", text=payload)],
                                        isError=True)
        return [types.TextContent(type="text", text=payload)]

    return server


def mcp_stdio_main():
    import anyio
    from mcp.server.stdio import stdio_server

    server = build_mcp_server()

    async def run():
        async with stdio_server() as (r, w):
            await server.run(r, w, server.create_initialization_options())
    anyio.run(run)


# --- HTTP ------------------------------------------------------------------------------
def make_http_handler(registry: Registry = REGISTRY):
    class H(BaseHTTPRequestHandler):
        def _send(self, code, obj):
            body = json.dumps(obj, default=str).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            if self.path.rstrip("/") == "/actions":
                return self._send(200, {"actions": registry.catalog("http")})
            self._send(404, {"ok": False, "error": {"code": "unknown_action", "message": self.path}})

        def do_POST(self):
            if not self.path.startswith("/actions/"):
                return self._send(404, {"ok": False, "error": {"code": "unknown_action",
                                                               "message": self.path}})
            name = self.path[len("/actions/"):]
            n = int(self.headers.get("Content-Length") or 0)
            if n > 1_000_000:
                return self._send(413, {"ok": False, "error": {"code": "invalid_input",
                                                               "message": "body too large"}})
            try:
                args = json.loads(self.rfile.read(n) or b"{}")
            except ValueError as e:
                args = {"__unparseable_json__": str(e)}
            key = self.headers.get("X-Approval-Key")
            res = registry.dispatch(name, args, Ctx(
                "http", agent=self.headers.get("X-WQ-Agent", ""),
                approved=frozenset([key]) if key else frozenset()))
            self._send(200 if res.ok else HTTP_STATUS.get(res.error["code"], 422), res.to_dict())

        def log_message(self, *a):
            pass

    return H


def serve_http(registry: Registry = REGISTRY, port: int = 8765, host: str = "127.0.0.1"):
    if host not in ("127.0.0.1", "::1", "localhost"):
        raise ValueError("fleet-actions HTTP binds loopback only")
    return ThreadingHTTPServer((host, port), make_http_handler(registry))
