"""Phone access for any local project: Buddy's MCP tools and Termux:API.

Buddy (app.buddy.assistant) runs an MCP server on http://127.0.0.1:8765/mcp with
tools such as read_screen, tap, type_text, open_app. Its bearer token is read from
Claude Code's config (~/.claude.json, server "phone") so it never has to be copied.

    from phone_bridge import Phone
    phone = Phone()
    print(phone.call("read_screen"))
    phone.call("open_app", app="Calculator")
    print(phone.battery())
"""
import json
import os
import subprocess

import requests

BUDDY_URL = "http://127.0.0.1:8765/mcp"
CLAUDE_CONFIG = os.path.expanduser("~/.claude.json")

# Tools that change something outside the phone or can't be undone. Callers should
# get the user's OK before using them (Buddy also asks for send/post/buy/delete).
RISKY_TOOLS = {"install_apk", "update_buddy"}


def _buddy_config():
    try:
        with open(CLAUDE_CONFIG) as f:
            config = json.load(f)
    except (OSError, ValueError):
        return None

    def find(node):
        if isinstance(node, dict):
            server = node.get("phone")
            if isinstance(server, dict) and server.get("url"):
                return server
            for value in node.values():
                found = find(value)
                if found:
                    return found
        return None
    return find(config)


def _parse(response):
    # Streamable HTTP may answer with plain JSON or with server-sent events.
    if "text/event-stream" in response.headers.get("Content-Type", ""):
        for line in response.text.splitlines():
            if line.startswith("data:"):
                data = json.loads(line[5:].strip())
                if "result" in data or "error" in data:
                    return data
        return {}
    return response.json() if response.text.strip() else {}


class Phone:
    def __init__(self, timeout=60):
        server = _buddy_config() or {}
        self.url = server.get("url", BUDDY_URL)
        self.headers = {"Content-Type": "application/json",
                        "Accept": "application/json, text/event-stream",
                        **server.get("headers", {})}
        self.timeout = timeout
        self.session = None
        self._id = 0

    # --- Buddy (screen control) ---
    def _rpc(self, method, params=None, notify=False):
        body = {"jsonrpc": "2.0", "method": method}
        if params is not None:
            body["params"] = params
        if not notify:
            self._id += 1
            body["id"] = self._id
        headers = dict(self.headers)
        if self.session:
            headers["Mcp-Session-Id"] = self.session
        r = requests.post(self.url, json=body, headers=headers, timeout=self.timeout)
        r.raise_for_status()
        self.session = r.headers.get("Mcp-Session-Id", self.session)
        return None if notify else _parse(r)

    def _connect(self):
        if self.session is None:
            self._rpc("initialize", {"protocolVersion": "2025-03-26", "capabilities": {},
                                     "clientInfo": {"name": "phone_bridge", "version": "1.0"}})
            self._rpc("notifications/initialized", notify=True)

    def available(self):
        try:
            self._connect()
            return True
        except (requests.RequestException, ValueError):
            return False

    def tools(self):
        self._connect()
        return [t["name"] for t in self._rpc("tools/list").get("result", {}).get("tools", [])]

    def call(self, tool, **args):
        """Run a Buddy tool and return its text output (or an error string)."""
        try:
            self._connect()
            data = self._rpc("tools/call", {"name": tool, "arguments": args})
        except (requests.RequestException, ValueError) as e:
            return f"FAILED: Buddy isn't reachable ({e}). Is the Buddy app running?"
        if "error" in data:
            return f"FAILED: {data['error'].get('message', data['error'])}"
        result = data.get("result", {})
        text = "\n".join(c.get("text", "") for c in result.get("content", []) if c.get("type") == "text")
        return ("FAILED: " + text) if result.get("isError") else text

    # --- Termux:API (device data) ---
    @staticmethod
    def termux(command, *args, timeout=20):
        try:
            out = subprocess.run([command, *args], capture_output=True, text=True, timeout=timeout)
            return out.stdout.strip() or out.stderr.strip()
        except (OSError, subprocess.TimeoutExpired) as e:
            return f"FAILED: {e}"

    def battery(self):
        return self.termux("termux-battery-status")

    def notifications(self):
        return self.termux("termux-notification-list")

    def toast(self, text):
        return self.termux("termux-toast", text)

    def notify(self, title, text):
        return self.termux("termux-notification", "--title", title, "--content", text)

    def online(self):
        try:
            requests.head("https://api.anthropic.com", timeout=4)
            return True
        except requests.RequestException:
            return False


if __name__ == "__main__":
    phone = Phone()
    print("Buddy reachable:", phone.available())
    if phone.available():
        print("Tools:", ", ".join(phone.tools()))
    print("Online:", phone.online())
    print("Battery:", phone.battery()[:120])
