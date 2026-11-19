import unittest

from network_harness.claude import prepare_args, with_guard_hook
from network_harness.claude_hook import failure_kind
from network_harness.codex import is_stdio_app_server, network_error


class ClaudePolicyTests(unittest.TestCase):
    def event(self, error, message):
        return {
            "hook_event_name": "StopFailure",
            "error": error,
            "last_assistant_message": message,
        }

    def test_network_failures_and_server_errors_trip_the_guard(self):
        for error, message in [
            ("server_error", "Request timed out"),
            ("server_error", "API Error: Connection refused (ECONNREFUSED)"),
            ("unknown", "Stream ended without receiving any events"),
            ("unknown", "TLS error"),
            ("unknown", "DNS error (ENOTFOUND)"),
        ]:
            with self.subTest(message=message):
                self.assertEqual(failure_kind(self.event(error, message)), b"network")
        self.assertEqual(failure_kind(self.event("server_error", "503")), b"server")

    def test_failed_tools_and_rejected_requests_are_not_network_faults(self):
        self.assertIsNone(failure_kind({"hook_event_name": "PostToolUseFailure"}))
        self.assertIsNone(failure_kind(self.event("authentication_failed", "401")))
        self.assertIsNone(failure_kind(self.event("rate_limit", "429")))
        self.assertIsNone(failure_kind(self.event("unknown", "Unexpected content block type")))

    def test_settings_merge_preserves_hooks_and_excludes_fallback(self):
        stop_hook = {"hooks": [{"type": "command", "command": "custom-notification"}]}
        settings = {
            "permissions": {"deny": ["Bash"]},
            "fallbackModel": ["haiku"],
            "env": {"CLAUDE_CODE_MAX_RETRIES": "9", "CUSTOM": "yes"},
            "hooks": {"StopFailure": [stop_hook], "Stop": [stop_hook]},
        }
        merged = with_guard_hook(settings, "/tmp/example socket", {"CLAUDE_CODE_MAX_RETRIES": "0"})
        self.assertEqual(merged["permissions"], settings["permissions"])
        self.assertEqual(merged["env"], {"CLAUDE_CODE_MAX_RETRIES": "0", "CUSTOM": "yes"})
        self.assertEqual(merged["fallbackModel"], [])
        self.assertEqual(merged["hooks"]["Stop"], [stop_hook])
        self.assertEqual(merged["hooks"]["StopFailure"][1:], [stop_hook])
        self.assertIn("'/tmp/example socket'", merged["hooks"]["StopFailure"][0]["hooks"][0]["command"])

    def test_cli_options_cannot_invoke_unguarded_modes(self):
        for option in ("--background", "--bare", "--safe-mode", "--fallback-model=haiku", "attach"):
            with self.subTest(option=option), self.assertRaises(ValueError):
                prepare_args([option])
        self.assertEqual(prepare_args(["--settings", "file.json", "-p", "--", "--bare"]),
                         (["-p", "--", "--bare"], "file.json"))


class CodexPolicyTests(unittest.TestCase):
    def test_stdio_adapter_does_not_wrap_daemon_or_help(self):
        self.assertTrue(is_stdio_app_server(["-c", "features.code_mode_host=true", "app-server"]))
        self.assertTrue(is_stdio_app_server(["app-server", "--listen=stdio://"]))
        self.assertFalse(is_stdio_app_server(["app-server", "daemon"]))
        self.assertFalse(is_stdio_app_server(["app-server", "--listen", "unix:///tmp/demo"]))
        self.assertFalse(is_stdio_app_server(["app-server", "--help"]))

    def test_classifies_native_errors_and_excludes_permission_failures(self):
        self.assertTrue(network_error({"codexErrorInfo": {"responseStreamDisconnected": {}}}))
        self.assertTrue(network_error({"codexErrorInfo": "other", "message": "error sending request"}))
        self.assertFalse(network_error({"codexErrorInfo": "unauthorized", "message": "connection closed"}))
