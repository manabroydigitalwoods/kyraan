"""A minimal MCP stdio server for the adapter tests: newline-delimited
JSON-RPC, initialize handshake, two tools — `shout` (echoes upper) and
`fail` (isError). Run by the suite as a real subprocess."""
import json
import sys

for line in sys.stdin:
    try:
        msg = json.loads(line)
    except json.JSONDecodeError:
        continue
    method, mid = msg.get("method"), msg.get("id")
    if method == "initialize":
        out = {"jsonrpc": "2.0", "id": mid, "result": {
            "protocolVersion": "2024-11-05", "capabilities": {},
            "serverInfo": {"name": "fake", "version": "0"}}}
    elif method == "notifications/initialized":
        continue
    elif method == "tools/call":
        name = msg["params"]["name"]
        args = msg["params"].get("arguments", {})
        if name == "echo":
            out = {"jsonrpc": "2.0", "id": mid, "result": {"content": [
                {"type": "text", "text": json.dumps(
                    {"echoed_tool": name, "echoed_args": args})}]}}
        elif name == "fail":
            out = {"jsonrpc": "2.0", "id": mid, "result": {
                "isError": True,
                "content": [{"type": "text", "text": "deliberate failure"}]}}
        elif name == "shout":
            out = {"jsonrpc": "2.0", "id": mid, "result": {"content": [
                {"type": "text",
                 "text": str(args.get("text", "")).upper()}]}}
        elif name == "envcheck":
            import os
            out = {"jsonrpc": "2.0", "id": mid, "result": {"content": [
                {"type": "text", "text": os.environ.get("FAKE_KEY", "")}]}}
        else:
            out = {"jsonrpc": "2.0", "id": mid, "result": {
                "isError": True,
                "content": [{"type": "text", "text": "no such tool"}]}}
    else:
        out = {"jsonrpc": "2.0", "id": mid,
               "error": {"code": -32601, "message": f"unknown {method}"}}
    sys.stdout.write(json.dumps(out) + "\n")
    sys.stdout.flush()
