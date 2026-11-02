"""Error reporting to Sentry.

A no-op unless SENTRY_DSN is set, so tests and local runs never send anything. Imported by the
package ``__init__``, so every platform_media entry point reports errors. Standard library only at import
time: sentry_sdk is imported lazily, inside init_sentry/capture/CaptureErrors.

Nothing sensitive leaves the process: no performance tracing, no stack-frame local variables
(they hold JWTs and database URLs), no request bodies, headers, cookies, query strings or user
context, and every message is scrubbed of URL passwords, bearer tokens, JWTs, email addresses,
IP addresses, phone numbers and recipient/address values before it is sent.
"""

from __future__ import annotations

import os
import re
import sys
from typing import Any

# scheme://user:password@host -> scheme://user:***@host
_URL_PASSWORD = re.compile(r"(?P<head>[a-zA-Z][a-zA-Z0-9+.-]{0,31}://[^\s:/@]{1,256}:)[^\s@]{1,512}(?=@)")
# key=value / key: value secrets in libpq-style conninfo strings and similar, including prefixed
# keys (client_secret, access_token, DB_PASSWORD) and quoted dict/JSON keys and values
_KV_SECRET = re.compile(
    r"""(?i)([A-Za-z0-9_-]{0,64}(?:password|passwd|pwd|secret|token|api[_-]?key)["']?)(\s*[=:]\s*)"""
    # A quoted value runs to its closing quote (escapes included), or to the end if unterminated.
    r"""(?:"(?:[^"\\]|\\.)*"?|'(?:[^'\\]|\\.)*'?|[^\s&"',}]+)"""
)
# key=value / "key": "value" personal data (delivery recipients, postal or email addresses)
_KV_PII = re.compile(
    r"""(?i)\b(recipients?|to|address(?:es)?|e-?mail|phone|msisdn|destination)(["']?\s*[=:]\s*)"""
    r"""(?:"(?:[^"\\]|\\.)*"?|'(?:[^'\\]|\\.)*'?|\S+)"""
)
_URL_QUERY = re.compile(r"(?P<head>[a-zA-Z][a-zA-Z0-9+.-]{0,31}://[^\s?#]{0,512})\?[^\s#]*")
_BEARER = re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._~+/=-]+")
_JWT = re.compile(r"\beyJ[A-Za-z0-9_-]{5,4096}\.[A-Za-z0-9_-]{5,4096}\.[A-Za-z0-9_-]{0,4096}")
_EMAIL = re.compile(r"[A-Za-z0-9._%+-]{1,64}@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)*\.[A-Za-z]{2,}")
# E.164-style phone numbers (+ and 7-15 digits, optional separators)
_PHONE = re.compile(r"\+\d(?:[ -]?\d){6,14}\b")
_IPV4 = re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}\b")
_IPV6 = re.compile(r"(?i)\b(?:[0-9a-f]{1,4}:){3,7}[0-9a-f]{1,4}\b")
# Compressed forms (2001:db8::1, fe80::1, ::1); not Rust/C++ paths like std::vec (letter before ::).
_IPV6_COMPRESSED = re.compile(
    r"(?i)(?<![0-9a-z:])(?:[0-9a-f]{1,4}(?::[0-9a-f]{1,4}){0,6})?::(?:[0-9a-f]{1,4}(?::[0-9a-f]{1,4}){0,6})?(?![0-9a-z:])"
)


# Every pattern above is bounded and the input is pre-capped, so a huge or hostile message cannot
# stall error reporting. The pre-cap drops the trailing partial token (which could be half a
# secret), and the output is truncated only after scrubbing.
MAX_SCRUB_CHARS = 8192
MAX_INPUT_CHARS = 65536
TRUNCATED = "...[truncated]"
_TOKENS = re.compile(r"(\s+)")


def _fail_closed(text: str) -> str:
    # Past a pattern bound a URL can still carry credentials or a query, a JWT can be too long to
    # match, and an email can keep the start of a long local part: drop the whole token.
    parts = _TOKENS.split(text)
    for i, tok in enumerate(parts):
        # Count, not presence: one scrubbed URL must not vouch for another in the same token.
        if "://" in tok and (tok.count("@") != tok.count(":***@") or tok.count("?") != tok.count("?***")):
            parts[i] = "***url***"
        elif "***jwt***" in tok or ("eyJ" in tok and tok.count(".") >= 2):
            parts[i] = "***jwt***"
        elif "***email***" in tok:
            parts[i] = "***email***"
    return "".join(parts)


def scrub(text: str) -> str:
    if len(text) > MAX_INPUT_CHARS:
        head = text[:MAX_INPUT_CHARS]
        cut = max(head.rfind(" "), head.rfind("\n"), head.rfind("\t"))
        text = head[:cut] if cut > 0 else ""
    text = _URL_PASSWORD.sub(r"\g<head>***", text)
    text = _URL_QUERY.sub(r"\g<head>?***", text)
    text = _KV_SECRET.sub(r"\1\2***", text)
    text = _KV_PII.sub(r"\1\2***", text)
    text = _BEARER.sub("Bearer ***", text)
    text = _JWT.sub("***jwt***", text)
    text = _EMAIL.sub("***email***", text)
    text = _PHONE.sub("***phone***", text)
    text = _IPV4.sub("***ip***", text)
    text = _IPV6.sub("***ip***", text)
    text = _fail_closed(_IPV6_COMPRESSED.sub("***ip***", text))
    if len(text) > MAX_SCRUB_CHARS:
        cut = text.rfind(" ", 0, MAX_SCRUB_CHARS)
        text = text[: cut if cut > 0 else MAX_SCRUB_CHARS] + TRUNCATED
    return text


def _scrub_event(event: dict[str, Any], _hint: dict[str, Any]) -> dict[str, Any]:
    for exc in (event.get("exception") or {}).get("values") or []:
        if isinstance(exc.get("value"), str):
            exc["value"] = scrub(exc["value"])
    if isinstance(event.get("message"), str):
        event["message"] = scrub(event["message"])
    logentry = event.get("logentry")
    if isinstance(logentry, dict):
        # Logging events carry the template, the raw params and the formatted text; keep only
        # the scrubbed formatted text (a param like a token is no longer next to "Bearer").
        formatted = logentry.get("formatted") or logentry.get("message")
        event["logentry"] = {"message": scrub(str(formatted))} if formatted else {}
    request = event.get("request")
    if isinstance(request, dict):
        # Keep only the method. URLs can carry personal data (ids, recipients, query strings);
        # the route template is already in event["transaction"].
        event["request"] = {"method": request["method"]} if "method" in request else {}
    for crumb in (event.get("breadcrumbs") or {}).get("values") or []:
        if isinstance(crumb.get("message"), str):
            crumb["message"] = scrub(crumb["message"])
        crumb.pop("data", None)
    event.pop("extra", None)
    # User context carries IP and email addresses; errors don't need it.
    event.pop("user", None)
    # Incoming baggage headers end up here and in the envelope's trace header (user ids,
    # transaction names); errors don't need them.
    trace = (event.get("contexts") or {}).get("trace")
    if isinstance(trace, dict):
        trace.pop("dynamic_sampling_context", None)
    return event


class CaptureErrors:
    """Innermost ASGI layer that reports unhandled exceptions, then re-raises them.

    The shared core's boundary turns an unexpected exception into a redacted 500, so Sentry's
    outer ASGI integration never sees it. A no-op while Sentry is not initialised.
    """

    def __init__(self, app: Any) -> None:
        self.app = app

    async def __call__(self, scope: Any, receive: Any, send: Any) -> None:
        try:
            await self.app(scope, receive, send)
        except Exception as exc:
            import sentry_sdk

            sentry_sdk.capture_exception(exc)
            raise


def init_sentry() -> None:
    dsn = os.environ.get("SENTRY_DSN", "").strip()
    if not dsn:
        return
    import sentry_sdk
    from sentry_sdk.integrations.logging import LoggingIntegration

    sentry_sdk.init(
        dsn=dsn,
        environment=os.environ.get("SENTRY_ENVIRONMENT", "production"),
        release=os.environ.get("RAILWAY_GIT_COMMIT_SHA") or os.environ.get("SENTRY_RELEASE") or None,
        # None (not 0.0) disables tracing completely: with 0.0 an incoming sentry-trace header
        # can still force a sampled transaction that carries the URL, query and headers.
        traces_sample_rate=None,
        send_default_pii=False,
        include_local_variables=False,  # frame locals hold JWTs, database URLs and recipients
        max_request_body_size="never",
        # sentry-sdk types events as a TypedDict; the scrubber treats them as plain dicts on purpose.
        before_send=_scrub_event,  # type: ignore[arg-type]
        # Log records are neither events nor breadcrumbs: free-form log text can carry secrets
        # that no pattern can recognise. Unhandled exceptions (FastAPI, excepthook) and capture()
        # still report.
        integrations=[LoggingIntegration(level=None, event_level=None, sentry_logs_level=None)],
    )
    service = os.environ.get("RAILWAY_SERVICE_NAME", "").strip()
    if service:
        sentry_sdk.set_tag("service", service)
    print(f"sentry: enabled for {service or 'unknown service'}", file=sys.stderr, flush=True)


def capture(exc: BaseException) -> None:
    """Report an exception a CLI/worker handler catches and turns into an exit code."""
    if not os.environ.get("SENTRY_DSN", "").strip():
        return
    import sentry_sdk

    sentry_sdk.capture_exception(exc)
    sentry_sdk.flush(5)
