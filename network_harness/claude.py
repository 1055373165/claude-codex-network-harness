"""Launch a foreground Claude Code session with fail-fast model transport."""

import json
import os
import re
import select
import shlex
import signal
import subprocess
import sys
import tempfile
from contextlib import ExitStack, suppress
from pathlib import Path

from .claude_hook import failure_kind
from .runtime import CONFIGURATION_FAILURE, NETWORK_FAILURE, child_exit_status, executable, terminate_child


MINIMUM_VERSION = (2, 1, 285)
RETRY_SETTINGS = {
    "CLAUDE_CODE_MAX_RETRIES": "0",
    "CLAUDE_CODE_RETRY_WATCHDOG": "0",
    "CLAUDE_CODE_NONSTREAMING_TIMEOUT_RETRIES": "0",
    "CLAUDE_CODE_DISABLE_NONSTREAMING_FALLBACK": "1",
    "CLAUDE_CODE_DISABLE_MODEL_ACCESS_FALLBACK": "1",
    "CLAUDE_CODE_DISABLE_FAST_MODE": "1",
    "CLAUDE_CODE_DISABLE_TERMINAL_TITLE": "1",
    "CLAUDE_CODE_ENABLE_PROMPT_SUGGESTION": "0",
    "CLAUDE_CODE_RESUME_INTERRUPTED_TURN": "0",
    "CLAUDE_ENABLE_BYTE_WATCHDOG": "1",
}
UNSUPPORTED_OPTIONS = {
    "--allow-remote-control", "--background", "--bare", "--bg", "--cloud", "--desktop",
    "--environment", "--fallback-model", "--remote-control", "--safe-mode", "--teleport",
}
MANAGEMENT_COMMANDS = {
    "agents", "attach", "auth", "auto-mode", "doctor", "gateway", "import", "install", "logs",
    "mcp", "plugin", "plugins", "purge", "respawn", "rm", "setup-token", "stop", "kill",
    "ultrareview", "update", "upgrade",
}
HOOK_SUPPRESSING_ENV = ("CLAUDE_CODE_SAFE_MODE", "CLAUDE_CODE_SIMPLE")


def hooks_suppressed(env):
    return any(str(env.get(key, "")).lower() in ("1", "true", "yes", "on")
               for key in HOOK_SUPPRESSING_ENV)


def milliseconds(name, default, minimum, maximum):
    value = os.environ.get(name, str(default))
    if not re.fullmatch(r"[0-9]{1,10}", value) or not minimum <= int(value) <= maximum:
        raise ValueError(f"{name} must be an integer between {minimum} and {maximum}.")
    return str(int(value))


def native_settings():
    return {
        **RETRY_SETTINGS,
        "API_TIMEOUT_MS": milliseconds("CLAUDE_HARNESS_API_TIMEOUT_MS", 60000, 1, 2147483647),
        "CLAUDE_BYTE_STREAM_IDLE_TIMEOUT_MS": milliseconds(
            "CLAUDE_HARNESS_IDLE_TIMEOUT_MS", 60000, 10000, 1800000
        ),
    }


def prepare_args(args):
    if args and args[0] in MANAGEMENT_COMMANDS:
        raise ValueError(f"Use the native claude command for {args[0]!r}.")
    forwarded = []
    supplied = None
    it = iter(args)
    for arg in it:
        if arg == "--":
            forwarded.extend([arg, *it])
            break
        option, equal, value = arg.partition("=")
        if option in UNSUPPORTED_OPTIONS:
            raise ValueError(f"{option} is incompatible with a guarded foreground session.")
        if option != "--settings":
            forwarded.append(arg)
            continue
        if supplied is not None:
            raise ValueError("Supply --settings only once.")
        supplied = value if equal else next(it, "")
        if not supplied:
            raise ValueError("--settings requires a JSON object or a path.")
    return forwarded, supplied


def read_settings(supplied):
    if supplied is None:
        return {}
    try:
        content = supplied if supplied.lstrip().startswith("{") else Path(supplied).read_text()
        settings = json.loads(content)
    except (OSError, ValueError):
        raise ValueError("Unable to read --settings as a JSON object.") from None
    if not isinstance(settings, dict):
        raise ValueError("--settings must contain a JSON object.")
    if settings.get("disableAllHooks") or settings.get("allowManagedHooksOnly"):
        raise ValueError("The supplied settings disable the required StopFailure hook.")
    return settings


def session_settings(settings, native_env, failure_channel=None):
    hooks = settings.get("hooks", {})
    env = settings.get("env", {})
    if not isinstance(hooks, dict) or not isinstance(env, dict):
        raise ValueError("--settings hooks and env must be JSON objects.")
    if hooks_suppressed(env):
        raise ValueError("The supplied settings disable the required StopFailure hook.")
    merged = {**settings, "fallbackModel": [], "env": {**env, **native_env}}
    if failure_channel is None:
        return merged
    stop_failure = hooks.get("StopFailure", [])
    if not isinstance(stop_failure, list):
        raise ValueError("--settings hooks.StopFailure must be an array.")
    # Claude supplies one JSON object on stdin. Use shell builtins so the hook
    # can deliver it before a non-interactive Claude invocation exits.
    command = shlex.join([
        "/bin/sh", "-c", 'IFS= read -r event; printf "%s\\n" "$event" > "$1"',
        "network-harness", str(failure_channel),
    ])
    guard = {"matcher": "server_error|unknown", "hooks": [{"type": "command", "command": command}]}
    return {
        **merged,
        "hooks": {**hooks, "StopFailure": [guard, *stop_failure]},
    }


def check_version(binary):
    try:
        version = subprocess.check_output([binary, "--version"], text=True, timeout=8).strip()
    except (subprocess.SubprocessError, UnicodeError):
        version = ""
    match = re.match(r"(\d+)\.(\d+)\.(\d+)\b", version)
    if not match or tuple(map(int, match.groups())) < MINIMUM_VERSION:
        raise ValueError("Claude Code 2.1.285 or newer is required.")


def supervise(child, channel):
    pending = b""
    while True:
        status = child.poll()
        readable, _, _ = select.select([channel], [], [], 0.35 if status else 0.1)
        if not readable:
            if status is not None:
                return child_exit_status(status)
            continue
        pending += os.read(channel, 65536)
        while b"\n" in pending:
            line, pending = pending.split(b"\n", 1)
            try:
                kind = failure_kind(json.loads(line))
            except (ValueError, UnicodeError):
                continue
            if kind:
                terminate_child(child)
                reason = "a model network failure" if kind == b"network" else "a model server error"
                print(f"Claude session disconnected after {reason}.", file=sys.stderr)
                return NETWORK_FAILURE


def run(binary, args, settings, native_env):
    # Import only on POSIX so unsupported platforms receive a configuration error.
    import termios

    terminal_fd = sys.stdin.fileno()
    exit_on_failure = not sys.stdout.isatty()
    for arg in args:
        if arg == "--":
            break
        if arg in ("-p", "--print"):
            exit_on_failure = True
            break
    terminal = None
    if os.isatty(terminal_fd):
        with suppress(OSError, termios.error):
            terminal = termios.tcgetattr(terminal_fd)
    with tempfile.TemporaryDirectory(prefix="claude-guard-", dir="/tmp") as tmp:
        channel_path = Path(tmp) / "failure" if exit_on_failure else None
        settings_path = Path(tmp) / "settings.json"
        if channel_path is not None:
            os.mkfifo(channel_path, 0o600)
        fd = os.open(settings_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w") as stream:
            json.dump(session_settings(settings, native_env, channel_path), stream)
        child = None

        def forward(signum, _):
            if child and child.poll() is None:
                child.send_signal(signum)

        old_handlers = {}
        # An interactive terminal sends SIGINT to the foreground group, including Claude.
        for sig, handler in ((signal.SIGINT, lambda *_: None), (signal.SIGTERM, forward),
                             (signal.SIGHUP, forward)):
            old_handlers[sig] = signal.signal(sig, handler)
        try:
            with ExitStack() as resources:
                reader = None
                if channel_path is not None:
                    # Keep a writer open so an idle FIFO doesn't appear permanently readable.
                    reader = resources.enter_context(
                        os.fdopen(os.open(channel_path, os.O_RDONLY | os.O_NONBLOCK), "rb")
                    )
                    resources.enter_context(
                        os.fdopen(os.open(channel_path, os.O_WRONLY | os.O_NONBLOCK), "wb")
                    )
                with subprocess.Popen(
                    [binary, "--settings", str(settings_path), *args],
                    env={**os.environ, **native_env},
                ) as child:
                    try:
                        if reader is None:
                            # Claude ends the failed turn itself and keeps the composer open.
                            return child_exit_status(child.wait())
                        return supervise(child, reader.fileno())
                    finally:
                        if child.poll() is None:
                            terminate_child(child)
        finally:
            for sig, handler in old_handlers.items():
                signal.signal(sig, handler)
            if terminal is not None:
                with suppress(OSError, termios.error):
                    if os.tcgetpgrp(terminal_fd) == os.getpgrp():
                        termios.tcsetattr(terminal_fd, termios.TCSANOW, terminal)


def main():
    try:
        if os.name != "posix":
            raise ValueError("The Claude harness requires macOS, Linux or WSL.")
        binary = executable("CLAUDE_HARNESS_BIN", "claude")
        if Path(binary).resolve() == Path(sys.argv[0]).resolve():
            raise ValueError("CLAUDE_HARNESS_BIN must identify the native Claude executable.")
        args = sys.argv[1:]
        if args in (["-h"], ["--help"], ["-v"], ["--version"]):
            os.execv(binary, [binary, *args])
        if hooks_suppressed(os.environ):
            raise ValueError("Unset Claude Code safe or simple mode to use StopFailure.")
        args, supplied = prepare_args(args)
        settings = read_settings(supplied)
        native_env = native_settings()
        check_version(binary)
        return run(binary, args, settings, native_env)
    except (OSError, ValueError) as exc:
        print(f"claude-network-harness: {exc}", file=sys.stderr)
        return CONFIGURATION_FAILURE


if __name__ == "__main__":
    sys.exit(main())
