#!/usr/bin/env python3
"""dc_mcp_probe.py <port> [--expect-version X] [--path /mcp]
Real MCP streamable-HTTP round trip against a desktop-commander gateway on localhost:
initialize -> notifications/initialized -> tools/list -> tools/call start_process(echo nonce).
Exit 0 = pass, 1 = fail. Prints one JSON line."""
import json, sys, urllib.request, uuid

def main():
    port = sys.argv[1]
    want = sys.argv[sys.argv.index("--expect-version") + 1] if "--expect-version" in sys.argv else None
    path = sys.argv[sys.argv.index("--path") + 1] if "--path" in sys.argv else "/mcp"
    url = f"http://127.0.0.1:{port}{path}"
    sid = {"v": None}
    def post(body, expect_reply=True):
        h = {"Content-Type": "application/json", "Accept": "application/json, text/event-stream"}
        if sid["v"]:
            h["mcp-session-id"] = sid["v"]
        req = urllib.request.Request(url, data=json.dumps(body).encode(), headers=h, method="POST")
        with urllib.request.urlopen(req, timeout=30) as r:
            if r.headers.get("mcp-session-id"):
                sid["v"] = r.headers.get("mcp-session-id")
            raw = r.read().decode("utf-8", "replace")
        if not expect_reply or not raw.strip():
            return None
        msg = None
        for line in raw.splitlines():
            line = line.strip()
            if line.startswith("data:"):
                try:
                    msg = json.loads(line[5:].strip())
                except Exception:
                    pass
        if msg is None:
            msg = json.loads(raw)
        return msg
    out = {"port": port}
    try:
        init = post({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {
            "protocolVersion": "2025-03-26", "capabilities": {}, "clientInfo": {"name": "dc-upgrade-probe", "version": "1"}}})
        info = (init.get("result") or {}).get("serverInfo") or {}
        out["server"] = info.get("name"); out["version"] = info.get("version")
        post({"jsonrpc": "2.0", "method": "notifications/initialized"}, expect_reply=False)
        tl = post({"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
        names = [t["name"] for t in (tl.get("result") or {}).get("tools", [])]
        out["tools"] = len(names)
        assert "start_process" in names, "start_process missing from tools/list"
        nonce = "dcupg-" + uuid.uuid4().hex[:10]
        call = post({"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {
            "name": "start_process", "arguments": {"command": f"echo {nonce}", "timeout_ms": 8000}}})
        txt = json.dumps(call)
        out["echo_ok"] = nonce in txt
        assert out["echo_ok"], "start_process echo did not return the nonce"
        if want:
            assert out["version"] == want, f"version {out['version']} != expected {want}"
        out["pass"] = True
    except Exception as e:
        out["pass"] = False; out["error"] = f"{type(e).__name__}: {e}"[:300]
    print(json.dumps(out))
    sys.exit(0 if out.get("pass") else 1)
main()
