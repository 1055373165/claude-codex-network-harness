"""Loopback tests for the installed Claude Code binary."""

import errno
import json
import os
import select
import signal
import socket
import struct
import subprocess
import sys
import threading
import time
import unittest
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from tempfile import TemporaryDirectory


ROOT = Path(__file__).resolve().parents[1]
CLAUDE = os.getenv("CLAUDE_HARNESS_TEST_BIN")
MODEL = "claude-sonnet-4-6"


class ModelFixture(BaseHTTPRequestHandler):
    def log_message(self, *_):
        pass

    def do_GET(self):
        self.send_error(404)

    def do_POST(self):
        self.rfile.read(int(self.headers.get("Content-Length", 0)))
        self.server.requests.append(self.path.partition("?")[0])
        mode = self.server.mode
        if mode == "timeout":
            time.sleep(2)
            return
        if mode == "reset":
            self.connection.shutdown(socket.SHUT_RDWR)
            return
        if mode in ("503", "429"):
            body = json.dumps({
                "type": "error",
                "error": {
                    "type": "api_error" if mode == "503" else "rate_limit_error",
                    "message": "fixture response",
                },
            }).encode()
            self.send_response(int(mode))
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.end_headers()
        events = [
            {"type": "message_start", "message": {
                "id": "msg_fixture", "type": "message", "role": "assistant",
                "model": MODEL, "content": [], "stop_reason": None,
                "stop_sequence": None, "usage": {"input_tokens": 1, "output_tokens": 0},
            }},
            {"type": "content_block_start", "index": 0, "content_block": {"type": "text", "text": ""}},
            {"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": "fixture-ok"}},
        ]
        if mode == "ok":
            events.extend([
                {"type": "content_block_stop", "index": 0},
                {"type": "message_delta", "delta": {"stop_reason": "end_turn", "stop_sequence": None},
                 "usage": {"output_tokens": 1}},
                {"type": "message_stop"},
            ])
        for event in events:
            self.wfile.write(f"event: {event['type']}\ndata: {json.dumps(event)}\n\n".encode())
        self.wfile.flush()


@contextmanager
def serve(mode):
    with ThreadingHTTPServer(("127.0.0.1", 0), ModelFixture) as server:
        server.mode = mode
        server.requests = []
        worker = threading.Thread(target=server.serve_forever, daemon=True)
        worker.start()
        try:
            yield server
        finally:
            server.shutdown()
            worker.join()


@unittest.skipUnless(CLAUDE, "set CLAUDE_HARNESS_TEST_BIN for the native integration tests")
class NativeClaudeTests(unittest.TestCase):
    def environment(self, path, port):
        env = {key: os.environ[key] for key in ("PATH", "HOME", "USER", "SHELL") if key in os.environ}
        env.update(
            PYTHONPATH=str(ROOT),
            CLAUDE_CONFIG_DIR=str(path),
            CLAUDE_HARNESS_BIN=CLAUDE,
            CLAUDE_HARNESS_API_TIMEOUT_MS="450",
            CLAUDE_CODE_MAX_RETRIES="10",
            CLAUDE_CODE_RETRY_WATCHDOG="1",
            CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC="1",
            CLAUDE_CODE_DISABLE_OFFICIAL_MARKETPLACE_AUTOINSTALL="1",
            DISABLE_AUTOUPDATER="1",
            ANTHROPIC_BASE_URL=f"http://127.0.0.1:{port}",
            ANTHROPIC_API_KEY="fixture-key",
            NO_PROXY="127.0.0.1,localhost",
        )
        return env

    def invoke(self, port):
        with TemporaryDirectory() as tmp:
            return subprocess.run(
                [sys.executable, "-m", "network_harness.claude", "-p", "--model", MODEL, "--tools", "",
                 "--no-session-persistence", "Local fixture"],
                cwd=tmp, env=self.environment(tmp, port), capture_output=True, text=True, timeout=12,
            )

    def test_first_failure_exits_and_does_not_retry(self):
        for mode in ("reset", "timeout", "eof", "503"):
            with self.subTest(mode=mode), serve(mode) as fixture:
                result = self.invoke(fixture.server_port)
                self.assertEqual(result.returncode, 69, (result.stdout, result.stderr))
                self.assertIn("session disconnected", result.stderr)
                self.assertEqual(fixture.requests, ["/v1/messages"])

    def test_connection_refusal_disconnects(self):
        with socket.socket() as unlistening:
            unlistening.bind(("127.0.0.1", 0))
            result = self.invoke(unlistening.getsockname()[1])
            self.assertEqual(result.returncode, 69, (result.stdout, result.stderr))

    def test_success_and_rate_limit_keep_the_native_exit_status(self):
        for mode, status in (("ok", 0), ("429", 1)):
            with self.subTest(mode=mode), serve(mode) as fixture:
                result = self.invoke(fixture.server_port)
                self.assertEqual(result.returncode, status, (result.stdout, result.stderr))
                self.assertNotIn("session disconnected", result.stderr)
                self.assertEqual(fixture.requests, ["/v1/messages"])
                if mode == "ok":
                    self.assertIn("fixture-ok", result.stdout)

    @unittest.skipUnless(os.name == "posix", "requires a POSIX terminal")
    def test_interactive_failure_exits_with_one_model_request(self):
        import fcntl
        import pty
        import termios

        with TemporaryDirectory() as tmp, serve("reset") as fixture:
            root = Path(tmp).resolve()
            project = root / "project"
            project.mkdir()
            config = root / "config"
            config.mkdir()
            (config / ".claude.json").write_text(json.dumps({
                "hasCompletedOnboarding": True,
                "lastOnboardingVersion": "2.1.299",
                "theme": "dark",
                "customApiKeyResponses": {"approved": ["fixture-key"], "rejected": []},
                "projects": {str(project): {"hasTrustDialogAccepted": True}},
            }))
            env = self.environment(config, fixture.server_port) | {"TERM": "xterm-256color"}
            pid, terminal = pty.fork()
            if pid == 0:
                os.chdir(project)
                os.execve(sys.executable, [
                    sys.executable, "-m", "network_harness.claude",
                    "--model", MODEL, "--tools", "", "--", "Local fixture",
                ], env)
            fcntl.ioctl(terminal, termios.TIOCSWINSZ, struct.pack("HHHH", 35, 120, 0, 0))
            output = bytearray()
            status = None
            try:
                deadline = time.monotonic() + 12
                while time.monotonic() < deadline:
                    if select.select([terminal], [], [], 0.1)[0]:
                        try:
                            output.extend(os.read(terminal, 8192))
                        except OSError as exc:
                            if exc.errno != errno.EIO:  # The terminal child has closed its side.
                                raise
                    ended, raw_status = os.waitpid(pid, os.WNOHANG)
                    if ended:
                        status = os.waitstatus_to_exitcode(raw_status)
                        break
                self.assertEqual(status, 69, output[-2400:].decode(errors="replace"))
                self.assertIn(b"session disconnected", output)
                self.assertEqual(fixture.requests, ["/v1/messages"])
            finally:
                os.close(terminal)
                if status is None:
                    try:
                        os.killpg(pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass
                    os.waitpid(pid, 0)
