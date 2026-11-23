"""Run the bundled Codex app-server with fail-fast inference sessions."""

import json
import os
import queue
import re
import subprocess
import sys
import threading
from dataclasses import dataclass, field
from functools import partial
from pathlib import Path
from typing import Optional
from uuid import uuid4

from .runtime import CONFIGURATION_FAILURE, child_exit_status, executable, terminate_child


BUNDLED_CODEX = "/Applications/ChatGPT.app/Contents/Resources/codex-cli/bin/codex"
PROVIDER = "openai_fail_fast"
PROVIDER_CONFIG = (
    '{name="OpenAI", wire_api="responses", requires_openai_auth=true, '
    'supports_websockets=false, supports_standalone_web_search=true, '
    'request_max_retries=0, stream_max_retries=0, stream_idle_timeout_ms=60000}'
)
# Resume must retain the saved provider; start and fork create new threads.
NEW_THREAD_METHODS = {"thread/start", "thread/fork"}
USER_TURN_METHODS = {"turn/start", "turn/addUserMessage"}
AUTOMATIC_CONTINUATION = "reconnect"
TURN_METHODS = USER_TURN_METHODS | {
    "turn/steer", "review/start",
    "thread/compact/start", "thread/goal/set", "thread/prediction/request",
    "thread/queue/add", "thread/queue/start", "thread/sample",
    "thread/realtime/start", "thread/realtime/appendAudio",
    "thread/realtime/appendSpeech", "thread/realtime/appendText",
}
NETWORK_CODES = {
    "httpConnectionFailed",
    "responseStreamConnectionFailed",
    "responseStreamDisconnected",
    "responseTooManyFailedAttempts",
}
NETWORK_TEXT = re.compile(
    r"timed? out|time[ -]?out|connection (?:failed|refused|reset|closed|aborted)|"
    r"network (?:is )?unreachable|error sending request|dns error|"
    r"stream disconnected|broken pipe|unexpected eof",
    re.IGNORECASE,
)
DISCONNECTED = "Network connection interrupted. Retry in this chat when your connection is back."


@dataclass
class DisconnectedSession:
    turn_id: Optional[str]
    phase: str = "closing"
    interrupted: bool = False
    history_requests: list = field(default_factory=list)
    next_submission: Optional[tuple] = None


def is_user_submission(method, params):
    if method not in USER_TURN_METHODS or params.get("parentTurnId") or params.get("toolOutput"):
        return False
    trigger = params.get("turnTrigger")
    if trigger == "resume_interrupted_task":
        return True
    user_input = params.get("input")
    return trigger in (None, "edit_user_message") and isinstance(user_input, list) and bool(user_input)


def network_error(error):
    if not isinstance(error, dict):
        return False
    code = error.get("codexErrorInfo")
    if isinstance(code, dict):
        return not NETWORK_CODES.isdisjoint(code)
    if isinstance(code, str) and code in NETWORK_CODES:
        return True
    if code is None or code == "other":
        return any(
            NETWORK_TEXT.search(str(error.get(field) or ""))
            for field in ("message", "additionalDetails")
        )
    return False


def write_message(stream, message):
    stream.write(json.dumps(message, separators=(",", ":"), ensure_ascii=False).encode() + b"\n")
    stream.flush()


def relay_lines(stream, source, events):
    try:
        for line in stream:
            events.put((source, line))
    finally:
        events.put((source, None))


def fail_fast_session(server, app_input, app_output):
    events = queue.Queue()
    for stream, source in ((app_input, "app"), (server.stdout, "server")):
        threading.Thread(target=relay_lines, args=(stream, source, events), daemon=True).start()

    disconnected = {}
    control_requests = {}

    def send_control(method, params, on_response=None):
        request_id = f"network-harness:{uuid4()}"
        control_requests[request_id] = on_response
        write_message(server.stdin, {"id": request_id, "method": method, "params": params})

    def forward_request(line):
        server.stdin.write(line)
        server.stdin.flush()

    def finish_reconnect(thread_id, session, response):
        request_id, new_turn = session.next_submission
        session.next_submission = None
        if "result" in response:
            del disconnected[thread_id]
            forward_request(new_turn)
        else:
            session.phase = "detached"
            write_message(app_output, {"id": request_id, "error": response["error"]})

    def reconnect_for_submission(thread_id, session):
        if session.phase == "detached" and session.next_submission is not None:
            session.phase = "reattaching"
            send_control("thread/resume", {"threadId": thread_id, "excludeTurns": True},
                         partial(finish_reconnect, thread_id, session))

    def finish_unsubscribe(thread_id, session, _response):
        session.phase = "detached"
        for request in session.history_requests:
            forward_request(request)
        session.history_requests.clear()
        reconnect_for_submission(thread_id, session)

    while True:
        source, line = events.get()
        if line is None:
            if source == "server":
                return
            server.stdin.close()
            continue

        destination = server.stdin if source == "app" else app_output
        try:
            message = json.loads(line)
        except (ValueError, UnicodeError):
            message = None
        if not isinstance(message, dict):
            destination.write(line)
            destination.flush()
            continue

        method = message.get("method")
        if not isinstance(method, str):
            method = None
        request_id = message.get("id")
        params = message.get("params")
        if not isinstance(params, dict):
            params = {}
        thread_id = params.get("threadId")
        if not isinstance(thread_id, str):
            thread_id = None

        if source == "app":
            session = disconnected.get(thread_id)
            if session and method == "thread/resume":
                # Opening a chat can load its history without authorizing a new model turn.
                if session.phase in {"closing", "detaching"}:
                    session.history_requests.append(line)
                else:
                    forward_request(line)
                continue
            # Desktop uses this trigger for its own delayed continuation of the failed turn.
            automatic = method in USER_TURN_METHODS and params.get("turnTrigger") == AUTOMATIC_CONTINUATION
            if automatic or (session and method in TURN_METHODS):
                if (session and "id" in message and is_user_submission(method, params)
                        and session.next_submission is None):
                    session.next_submission = (request_id, line)
                    reconnect_for_submission(thread_id, session)
                    continue
                if "id" in message:
                    write_message(app_output, {
                        "id": request_id,
                        "error": {"code": -32000, "message": DISCONNECTED},
                    })
                continue
            if method in NEW_THREAD_METHODS and params.get("modelProvider") in (None, "openai"):
                message["params"] = {**params, "modelProvider": PROVIDER}
                write_message(destination, message)
                continue
        else:
            if isinstance(request_id, str) and request_id in control_requests and method is None:
                on_response = control_requests.pop(request_id)
                if on_response:
                    on_response(message)
                continue
            if thread_id and method == "error" and network_error(params.get("error")):
                turn_id = params.get("turnId")
                session = disconnected.setdefault(thread_id, DisconnectedSession(turn_id))
                if params.get("willRetry"):
                    # The core's connection-loss timer is separate from the provider retry limits.
                    if not session.interrupted and isinstance(turn_id, str):
                        session.interrupted = True
                        send_control("turn/interrupt", {"threadId": thread_id, "turnId": turn_id})
                    message["params"] = {
                        **params,
                        "willRetry": False,
                        "error": {**params["error"], "message": DISCONNECTED},
                    }
                    write_message(destination, message)
                    continue
            if thread_id and method == "turn/completed":
                turn = params.get("turn")
                if isinstance(turn, dict):
                    turn_id = turn.get("id")
                    session = disconnected.get(thread_id)
                    if session is None and network_error(turn.get("error")):
                        session = disconnected[thread_id] = DisconnectedSession(turn_id)
                    if session and session.phase == "closing" and session.turn_id in (None, turn_id):
                        session.phase = "detaching"
                        send_control("thread/unsubscribe", {"threadId": thread_id},
                                     partial(finish_unsubscribe, thread_id, session))

        destination.write(line)
        destination.flush()


def is_stdio_app_server(args):
    if "app-server" not in args:
        return False
    server_args = args[args.index("app-server") + 1:]
    passthrough = {"--help", "-h", "--print-startup-config", "daemon", "proxy",
                   "generate-ts", "generate-json-schema"}
    if any(arg in passthrough for arg in server_args):
        return False
    for i, arg in enumerate(server_args):
        if arg == "--listen" and server_args[i + 1:i + 2] != ["stdio://"]:
            return False
        if arg.startswith("--listen=") and arg != "--listen=stdio://":
            return False
    return True


def main():
    try:
        binary = executable("CODEX_HARNESS_BIN", BUNDLED_CODEX)
        if Path(binary).resolve() == Path(sys.argv[0]).resolve():
            raise ValueError("CODEX_HARNESS_BIN must identify the native Codex executable.")
        return run(binary, sys.argv[1:])
    except (OSError, ValueError) as exc:
        print(f"codex-network-harness: {exc}", file=sys.stderr)
        return CONFIGURATION_FAILURE


def run(binary, args):
    if not is_stdio_app_server(args):
        os.execv(binary, [binary, *args])

    defaults = ["-c", f"model_provider={json.dumps(PROVIDER)}",
                "-c", f"model_providers.{PROVIDER}={PROVIDER_CONFIG}"]
    with subprocess.Popen([binary, *args, *defaults], stdin=subprocess.PIPE,
                          stdout=subprocess.PIPE) as server:
        try:
            fail_fast_session(server, sys.stdin.buffer, sys.stdout.buffer)
        except BrokenPipeError:
            pass
        finally:
            if server.poll() is None:
                if not server.stdin.closed:
                    server.stdin.close()
                try:
                    server.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    terminate_child(server)
        return child_exit_status(server.wait())


if __name__ == "__main__":
    sys.exit(main())
