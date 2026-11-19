"""Classify native Claude StopFailure events."""

import re


NETWORK_ERROR = re.compile(
    r"\b(?:E(?:AI_AGAIN|CONN(?:REFUSED|RESET|ABORTED)|PIPE|TIMEDOUT|"
    r"NETUNREACH|HOSTUNREACH|NOTFOUND)|ENETDOWN)\b|"
    r"\b(?:network|dns|tls|ssl|socket|certificate) (?:error|failure|failed|closed|unreachable)\b|"
    r"\b(?:request|connection|stream|operation) (?:has )?timed out\b|"
    r"\b(?:request|stream|connection|read|connect|idle) timeout\b|"
    r"\bconnection (?:error|failed|refused|reset|closed|dropped|aborted|lost)\b|"
    r"\b(?:connection to the api was lost|streamtruncated|premature close)\b|"
    r"\b(?:stream|response) (?:closed|disconnected|ended|terminated|aborted)\b|"
    r"\b(?:broken pipe|socket hang up|unexpected eof|unable to connect|failed to connect)\b",
    re.IGNORECASE,
)


def failure_kind(event):
    if not isinstance(event, dict) or event.get("hook_event_name") != "StopFailure":
        return None
    error = event.get("error")
    if error not in ("server_error", "unknown"):
        return None
    details = (event.get("error_details"), event.get("last_assistant_message"))
    if any(isinstance(text, str) and NETWORK_ERROR.search(text) for text in details):
        return b"network"
    if error == "server_error":
        return b"server"
    return None
