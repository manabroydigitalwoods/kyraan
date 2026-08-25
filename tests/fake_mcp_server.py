"""Minimal MCP stdio server for transport tests: answers initialize and
tools/call (echoes the arguments back as JSON text; the tool name
'mcp.fail' reports an MCP-level error instead). Run as a subprocess by
test_tool_registry's MCP tests — never imported by the app."""
import json
import sys


def main() -> None:
    for raw in sys.stdin:
        try:
            msg = json.loads(raw)
        except json.JSONDecodeError:
            continue
        if "id" not in msg:
            continue  # notification — nothing to answer
        if msg["method"] == "initialize":
            result = {"protocolVersion": "2024-11-05", "capabilities": {}, "serverInfo": {"name": "fake"}}
        elif msg["method"] == "tools/call":
            name = msg["params"]["name"]
            if name == "mcp.fail":
                result = {"isError": True, "content": [{"type": "text", "text": "deliberate failure"}]}
            else:
                payload = {"echoed_tool": name, "echoed_args": msg["params"]["arguments"]}
                result = {"content": [{"type": "text", "text": json.dumps(payload)}]}
        else:
            result = {}
        sys.stdout.write(json.dumps({"jsonrpc": "2.0", "id": msg["id"], "result": result}) + "\n")
        sys.stdout.flush()


if __name__ == "__main__":
    main()
