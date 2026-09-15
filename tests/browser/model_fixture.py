"""Deterministic loopback model protocol for controller browser acceptance.

This simulator returns typed read/proposal calls only. It cannot dispatch an
Oracle command, set an actor or approve a plan. The real local-model client and
assistant controller must validate every returned call themselves.
"""
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import threading

PORT = 18767
PLAN_ID = "assistant-native-patch"


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *_):
        pass  # Prompts and principal data must not enter HTTP logs.

    def do_POST(self):
        length = int(self.headers.get("Content-Length", "0"))
        if self.path != "/v1/chat/completions" or not 0 < length <= 2_000_000:
            self.send_error(400)
            return
        body = json.loads(self.rfile.read(length))
        messages = body.get("messages", [])
        last_user = max((i for i, message in enumerate(messages) if message.get("role") == "user"), default=-1)
        current = messages[last_user + 1:]
        calls = [call.get("function", {}).get("name") for message in current
                 for call in message.get("tool_calls", [])]
        tool = "inspect_plan" if "inspect_plan" not in calls else "execute_plan" if "execute_plan" not in calls else None
        if tool:
            message = {"role": "assistant", "content": None, "tool_calls": [{
                "id": f"fixture-{tool}", "type": "function",
                "function": {"name": tool, "arguments": json.dumps({"plan_id": PLAN_ID})}}]}
        else:
            message = {"role": "assistant", "content": "Fixture model inspected the saved plan and proposed its remaining tasks. Review the action card; native approval and authorization still apply."}
        payload = json.dumps({"choices": [{"index": 0, "finish_reason": "tool_calls" if tool else "stop", "message": message}]}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)


def start(base):
    server = ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
    server.daemon_threads = True
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    config = base / "model-fixture-config.json"
    config.write_text(json.dumps({"enabled": True, "provider": "ollama", "model": "deterministic-controller-fixture",
        "base_url": f"http://127.0.0.1:{PORT}/v1", "timeout_seconds": 5, "max_tokens": 256}))
    config.chmod(0o600)
    return server, config
