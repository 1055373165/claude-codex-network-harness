# Claude Code / Codex Network Harness

Two local launchers for stopping a coding-agent session at the first model network failure. They disable the available native retries and fallbacks before the session starts, listen for the client's failure event, and close the affected session without replaying the request.

## Install

Requires Python 3.9 or newer. Install the two commands with `pipx install .` from a checkout, or use `python3 -m network_harness.claude` and `python3 -m network_harness.codex` from the repository root.

## Claude Code CLI

Requires Claude Code 2.1.285 or newer on macOS, Linux, or WSL. Tested on 2.1.299 against a local Anthropic-compatible endpoint.

```sh
claude-network-harness
claude-network-harness --resume SESSION_ID
claude-network-harness -p 'Explain the change'
```

Set `CLAUDE_HARNESS_BIN` to select a particular Claude installation. The launcher inherits the terminal and authentication environment. It preserves any hook definitions supplied with `--settings` when building the private per-process settings file.

- The launcher sets API retries and non-streaming timeout retries to zero. It disables the retry watchdog, streaming fallback, model fallback, and fast mode for that invocation. Automatic terminal titles and prompt suggestions are also disabled to avoid their extra model requests.
- The API timeout and the byte-level streaming idle timeout default to 60 seconds. Override them with `CLAUDE_HARNESS_API_TIMEOUT_MS` and `CLAUDE_HARNESS_IDLE_TIMEOUT_MS`. Claude's native idle watchdog accepts 10,000 to 1,800,000 milliseconds for the byte-level timeout.
- A native `StopFailure` hook sends a local event through a private FIFO. The launcher classifies it in memory and prints only a generic failure reason. Network failures and model server errors terminate the child and return exit code `69`. Other errors and normal exits retain Claude's own behavior and exit status.
- The guard covers foreground interactive and print sessions. Hooks must be enabled by the effective Claude settings and organization policy. Use the native command for authentication and management. Background, cloud, desktop, and hook-disabled modes fall outside this launcher.

[Claude Code hook reference](https://code.claude.com/docs/en/hooks#stopfailure) · [Claude Code environment variables](https://code.claude.com/docs/en/env-vars)

## Codex Desktop

Tested with the macOS desktop app's bundled Codex CLI 0.169.0-alpha.11.1. The default native binary is under `/Applications/ChatGPT.app`. Set `CODEX_HARNESS_BIN` before launching the app to use a different executable.

```sh
launchctl setenv CODEX_CLI_PATH "$(command -v codex-network-harness)"
```

Quit the desktop app completely and reopen it. To turn off the override, run `launchctl unsetenv CODEX_CLI_PATH` and reopen the app. These commands affect the current macOS login session.

- This adapter wraps the local stdio app-server. It gives new OpenAI chats a zero-retry HTTP/SSE provider, with a 60-second stream idle timeout. Resumed chats keep their saved provider and its native retry settings. The interruption guard also watches the failures reported by resumed chats.
- When the app-server announces that it will reconnect after a network failure, the adapter immediately interrupts the turn and reports that it will not retry. It unsubscribes the affected chat after the turn ends and rejects further inference or resume requests for that chat until the app-server restarts. A new chat can start normally.
- Saved chat history remains available. Remote app-servers, other provider configurations, and separate tool or app traffic remain outside this launcher.

[Codex retry implementation](https://github.com/openai/codex/blob/rust-v0.169.0-alpha.11.1/codex-rs/core/src/responses_retry.rs)

## Verify

Run `python3 -m unittest discover -s tests -v`. Set `CLAUDE_HARNESS_TEST_BIN` to a native Claude Code executable to also run the loopback integration tests. These use mock credentials and do not send prompts to a real model.
