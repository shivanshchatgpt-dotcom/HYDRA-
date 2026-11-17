"""Tests for the MCP stdio adapter (stdlib unittest only).

Run from the repo root:
    python3 -m unittest tests.test_mcp -v

Covers: tool schema integrity (unique names, hydra_ prefix, required
fields present, payload keys declared), the JSON-RPC surface
(initialize / tools/list / unknown tool / unknown method), and that a
tools/call reaches the daemon client with exactly the declared payload
keys. No daemon and no network are needed.
"""

from __future__ import annotations

import unittest

from hydra import mcp_server


class FakeClient:
    def __init__(self):
        self.calls = []

    def call(self, tool, args):
        self.calls.append((tool, args))
        return 200, {"ok": True, "tool": tool}


class ToolSchemaTests(unittest.TestCase):
    def test_names_unique_prefixed_and_documented(self):
        seen = set()
        for name, (endpoint, desc, schema, keys) in mcp_server.TOOLS.items():
            self.assertTrue(name.startswith("hydra_"), name)
            self.assertNotIn(name, seen)
            seen.add(name)
            self.assertTrue(endpoint.startswith("/"), name)
            self.assertTrue(desc.strip(), name)
            self.assertEqual(schema.get("type"), "object", name)
            self.assertIn("properties", schema, name)

    def test_required_and_payload_keys_declared(self):
        for name, (_ep, _desc, schema, keys) in mcp_server.TOOLS.items():
            props = schema["properties"]
            for req in schema.get("required", []):
                self.assertIn(req, props, f"{name}: required {req}")
            for key in keys:
                if key == "spec":
                    continue
                self.assertIn(key, props, f"{name}: payload key {key}")

    def test_selfcheck_passes_offline(self):
        # selfcheck returns 0 when the schemas are sound even if no
        # daemon is running (it only prints the probe result).
        self.assertEqual(mcp_server.selfcheck(), 0)


class PayloadTests(unittest.TestCase):
    def _client(self, capture):
        client = mcp_server.HydraClient("http://example.invalid", "t")
        def fake(method, path, payload):
            capture.update(method=method, path=path, payload=payload)
            return 200, {"ok": True}
        client._request = fake
        return client

    def test_only_declared_keys_are_sent(self):
        capture = {}
        self._client(capture).call("hydra_read_file",
                                   {"path": "a.txt", "bogus": 1})
        self.assertEqual(capture["payload"], {"path": "a.txt"})
        self.assertEqual(capture["method"], "POST")
        self.assertEqual(capture["path"], "/read")

    def test_health_uses_get_without_body(self):
        capture = {}
        self._client(capture).call("hydra_health", {})
        self.assertEqual(capture["method"], "GET")
        self.assertEqual(capture["path"], "/health")

    def test_video_spec_passes_through_object(self):
        capture = {}
        self._client(capture).call(
            "hydra_video_create_project",
            {"name": "p", "tracks": [], "overwrite": True})
        self.assertEqual(capture["payload"],
                         {"name": "p", "tracks": [], "overwrite": True})


class JsonRpcTests(unittest.TestCase):
    def test_initialize(self):
        reply = mcp_server.handle_message(FakeClient(), {
            "jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}})
        self.assertEqual(reply["result"]["serverInfo"]["name"], "hydra")
        self.assertEqual(reply["result"]["protocolVersion"],
                         mcp_server.PROTOCOL_VERSION)

    def test_tools_list_matches_registry(self):
        reply = mcp_server.handle_message(
            FakeClient(), {"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
        names = {t["name"] for t in reply["result"]["tools"]}
        self.assertEqual(names, set(mcp_server.TOOLS))

    def test_unknown_tool_is_jsonrpc_error(self):
        reply = mcp_server.handle_message(FakeClient(), {
            "jsonrpc": "2.0", "id": 3, "method": "tools/call",
            "params": {"name": "hydra_nope", "arguments": {}}})
        self.assertEqual(reply["error"]["code"], -32602)

    def test_unknown_method_is_jsonrpc_error(self):
        reply = mcp_server.handle_message(
            FakeClient(), {"jsonrpc": "2.0", "id": 4, "method": "nope"})
        self.assertEqual(reply["error"]["code"], -32601)

    def test_notification_has_no_reply(self):
        self.assertIsNone(mcp_server.handle_message(
            FakeClient(), {"jsonrpc": "2.0",
                           "method": "notifications/initialized"}))

    def test_call_reaches_client(self):
        client = FakeClient()
        reply = mcp_server.handle_message(client, {
            "jsonrpc": "2.0", "id": 5, "method": "tools/call",
            "params": {"name": "hydra_read_file",
                       "arguments": {"path": "a.txt"}}})
        self.assertFalse(reply["result"]["isError"])
        self.assertEqual(client.calls, [("hydra_read_file", {"path": "a.txt"})])

    def test_daemon_error_sets_is_error(self):
        class Down:
            def call(self, tool, args):
                return 0, {"ok": False, "error": "daemon unreachable"}
        reply = mcp_server.handle_message(Down(), {
            "jsonrpc": "2.0", "id": 6, "method": "tools/call",
            "params": {"name": "hydra_health", "arguments": {}}})
        self.assertTrue(reply["result"]["isError"])


if __name__ == "__main__":
    unittest.main()
