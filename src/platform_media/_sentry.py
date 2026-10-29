"""Error reporting to Sentry.

A no-op unless SENTRY_DSN is set, so tests and local runs never send anything. Imported by the
package ``__init__``, so every platform_media entry point reports errors. Standard library only at import
time: sentry_sdk is imported lazily, inside init_sentry/capture.

Nothing sensitive leaves the process: no stack-frame local variables (they hold JWTs and database
URLs), no request bodies, headers, cookies, URLs or query strings, no user context, and every
message is scrubbed of URL passwords, bearer tokens, JWTs, email addresses, phone numbers and
recipient/address values before it is sent.
"""

from __future__ import annotations

import os
import re
import sys
from typing import Any

# scheme://user:password@host -> scheme://user:***@host
_URL_PASSWORD = re.compile(r"(?P<head>[a-zA-Z][a-zA-Z0-9+.-]*://[^\s:/@]+:)[^\s@]+(?=@)")
# key=value / key: value secrets in libpq-style conninfo strings and similar
_KV_SECRET = re.compile(r"(?i)\b(password|passwd|pwd|secret|token|api[_-]?key)(\s*[=:]\s*)\S+")
# key=value / "key": "value" personal data (delivery recipients, postal or email addresses)
_KV_PII = re.compile(
    r"(?i)\b(recipients?|to|address(?:es)?|e-?mail|phone|msisdn|destination)"
    r"(\"?\s*[=:]\s*)(\"[^\"]*\"|\S+)"
)
_BEARER = re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._~+/=-]+")
_JWT = re.compile(r"\beyJ[A-Za-z0-9_-]{5,}\.[A-Za-z0-9_-]{5,}\.[A-Za-z0-9_-]*")
_EMAIL = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)*\.[A-Za-z]{2,}")
# E.164-style phone numbers (+ and 7-15 digits, optional separators)
_PHONE = re.compile(r"\+\d(?:[ -]?\d){6,14}\b")


def scrub(text: str) -> str:
    text = _URL_PASSWORD.sub(r"\g<head>***", text)
    text = _KV_SECRET.sub(r"\1\2***", text)
    text = _KV_PII.sub(r"\1\2***", text)
    text = _BEARER.sub("Bearer ***", text)
    text = _JWT.sub("***jwt***", text)
    text = _EMAIL.sub("***email***", text)
    return _PHONE.sub("***phone***", text)


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
    event.pop("user", None)
    # Incoming baggage headers end up here and in the envelope's trace header (user ids,
    # transaction names); errors don't need them.
    trace = (event.get("contexts") or {}).get("trace")
    if isinstance(trace, dict):
        trace.pop("dynamic_sampling_context", None)
    return event


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
