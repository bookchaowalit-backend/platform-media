"""Error reporting to Sentry.

A no-op unless SENTRY_DSN is set, so tests and local runs never send anything. Imported by the
package ``__init__``, so every platform_media entry point reports errors. Standard library only at import
time: sentry_sdk is imported lazily, inside init_sentry/capture/CaptureErrors.

Nothing sensitive leaves the process: no performance tracing or trace propagation, no stack-frame
local variables (they hold JWTs and database URLs), no request bodies, headers, cookies, query
strings or user context, and no free text at all. An event carries the exception type, the stack
(file, function, line), the route template and tags; exception messages, log messages and
breadcrumbs are dropped. Review rounds on PRs #4-#13 showed that pattern scrubbing of free text
can always be bypassed by one more serialisation (repr, nested quotes, escapes); this service
handles recipients, addresses and phone numbers, so it fails closed instead.
"""

from __future__ import annotations

import os
import sys
from typing import Any


def _scrub_event(event: dict[str, Any], _hint: dict[str, Any]) -> dict[str, Any]:
    # Free text: an exception's message, a captured message, log text and breadcrumbs. None of it
    # is sent; the type and stack are enough to find the failing line.
    for exc in (event.get("exception") or {}).get("values") or []:
        if isinstance(exc, dict):
            exc.pop("value", None)
    event.pop("message", None)
    event.pop("logentry", None)
    event.pop("breadcrumbs", None)
    request = event.get("request")
    if isinstance(request, dict):
        # Keep only the method. URLs can carry personal data (ids, recipients, query strings);
        # the route template is already in event["transaction"].
        event["request"] = {"method": request["method"]} if "method" in request else {}
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
    outer ASGI integration never sees it. Reported with handled=False, so unhandled-error filters
    and alerts still count it. A no-op while Sentry is not initialised.
    """

    def __init__(self, app: Any) -> None:
        self.app = app

    async def __call__(self, scope: Any, receive: Any, send: Any) -> None:
        try:
            await self.app(scope, receive, send)
        except Exception as exc:
            import sentry_sdk
            from sentry_sdk.utils import event_from_exception

            client = sentry_sdk.get_client()
            # Building the event reads traceback locals; skip it when nothing would be sent.
            if client.is_active() and client.transport is not None:
                event, hint = event_from_exception(
                    exc, client_options=client.options, mechanism={"type": "asgi", "handled": False}
                )
                sentry_sdk.capture_event(event, hint=hint)
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
        # ...and never forward an incoming sentry-trace/baggage header (it can carry user ids) on
        # this service's own outbound requests, such as the JWKS fetch.
        trace_propagation_targets=[],
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
