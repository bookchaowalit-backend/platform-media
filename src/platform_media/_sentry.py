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
# A secret key: key=value / key: value in libpq conninfo and similar, including prefixed keys
# (client_secret, access_token, DB_PASSWORD) and quoted dict/JSON keys. Its value is found by
# _value_end; nothing here consumes the value, so no match can swallow the next key.
_KV_KEY = re.compile(r"""(?i)[A-Za-z0-9_-]{0,64}(?:password|passwd|pwd|secret|token|api[_-]?key)["']?\s*[=:]\s*""")
# A personal-data key (delivery recipients, postal or email addresses); its value is found the
# same way as a secret's.
_KV_PII_KEY = re.compile(r"""(?i)\b(?:recipients?|to|address(?:es)?|e-?mail|phone|msisdn|destination)["']?\s*[=:]\s*""")
# Value shapes, each tried at the value's start and bounded at _VALUE_CAP characters:
# a quoted value to its closing quote (escapes included), a structured value to a field boundary,
# and a libpq value to the next unescaped whitespace (libpq allows , } and quotes in a password).
_VALUE_CAP = 512
_QUOTED_AT = {
    '"': re.compile(r'"(?:[^"\\]|\\[\s\S]){0,512}"'),
    "'": re.compile(r"'(?:[^'\\]|\\[\s\S]){0,512}'"),
}
_FIELD_AT = re.compile(r"""[^\s&"',}\\]{1,512}""")
_LIBPQ_AT = re.compile(r"""(?:\\[\s\S]|[^\s\\]){1,512}""")
# scheme://host/path?query -> scheme://host/path?*** (OAuth codes, signed-URL tokens)
_URL_QUERY = re.compile(r"(?P<head>[a-zA-Z][a-zA-Z0-9+.-]{0,31}://[^\s?#]{0,512})\?[^\s#]*")
_BEARER = re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._~+/=-]+")
# Unbounded on purpose: a bounded token would leave its tail behind the "***". Still linear: matches
# start only at a "bearer" label and never overlap.
_BEARER_TOKEN = re.compile(r"(?i)\bbearer\s+(?P<token>[A-Za-z0-9._~+/=-]+)")
_JWT = re.compile(r"\beyJ[A-Za-z0-9_-]{5,4096}\.[A-Za-z0-9_-]{5,4096}\.[A-Za-z0-9_-]{0,4096}")
_EMAIL = re.compile(r"[A-Za-z0-9._%+-]{1,64}@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)*\.[A-Za-z]{2,}")
# E.164-style phone numbers (+ and 7-15 digits, optional separators)
_PHONE = re.compile(r"\+\d(?:[ -]?\d){6,14}\b")
_IPV4 = re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}\b")
_IPV6 = re.compile(r"(?i)\b(?:[0-9a-f]{1,4}:){3,7}[0-9a-f]{1,4}\b")
# Compressed forms (2001:db8::1, fe80::1, ::1), also right after a colon (peer:2001:db8::1);
# not Rust/C++ paths like std::vec (a letter or digit directly before the address).
_IPV6_COMPRESSED = re.compile(
    r"(?i)(?<![0-9a-z])(?:[0-9a-f]{1,4}(?::[0-9a-f]{1,4}){0,6})?::(?:[0-9a-f]{1,4}(?::[0-9a-f]{1,4}){0,6})?(?![0-9a-z])"
)


# Every pattern above is bounded and the input is pre-capped, so a huge or hostile message cannot
# stall error reporting. The pre-cap drops the trailing partial token (which could be half a
# secret), and the output is truncated only after scrubbing.
MAX_SCRUB_CHARS = 8192
MAX_INPUT_CHARS = 65536
TRUNCATED = "...[truncated]"
_TOKENS = re.compile(r"(\s+)")
_NON_SPACE = re.compile(r"\S+")


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


def _next_unescaped_whitespace(text: str) -> list[int]:
    # nxt[i]: index of the first whitespace at or after i not escaped by a backslash (len(text) if
    # none). One linear pass, so overlong values cost the same as short ones.
    unescaped = [False] * len(text)
    run = 0
    for i, ch in enumerate(text):
        unescaped[i] = ch.isspace() and run % 2 == 0
        run = run + 1 if ch == "\\" else 0
    nxt = [len(text)] * (len(text) + 1)
    for i in range(len(text) - 1, -1, -1):
        nxt[i] = i if unescaped[i] else nxt[i + 1]
    return nxt


def _last_quote_in_line(text: str, quote: str) -> list[int]:
    # last[i]: index of the last `quote` on i's line at or after i (-1 if none). One linear pass.
    last = [-1] * len(text)
    seen = -1
    for i in range(len(text) - 1, -1, -1):
        if text[i] == "\n":
            seen = -1
        elif text[i] == quote and seen == -1:
            seen = i
        last[i] = seen
    return last


def _value_end(text: str, start: int, nxt: list[int], last_quote: dict[str, list[int]]) -> int:
    # The furthest end any value shape reaches. A shape that hits its bound, or a quote that does
    # not close within it, runs on (to the next unescaped whitespace, or the end): fail closed.
    # A quoted value also runs to the last same quote on its line: once a DSN is serialised
    # (repr, a dict, a multi-argument exception) its backslashes are doubled, and an escaped quote
    # inside the password looks like the closing one.
    ends = [start]
    quote = text[start : start + 1]
    if quote == "\\" and text[start + 1 : start + 2] in _QUOTED_AT:
        # An escaped quote opens the value (\' in a repr'd string): quoted from there on.
        quote = text[start + 1]
        ends.append(last_quote[quote][start + 1] + 1)
    elif quote in _QUOTED_AT:
        m = _QUOTED_AT[quote].match(text, start)
        ends.append(m.end() if m else len(text))
        ends.append(last_quote[quote][start] + 1)
    for shape in (_FIELD_AT, _LIBPQ_AT):
        m = shape.match(text, start)
        if m:
            ends.append(nxt[start] if m.end() - start >= _VALUE_CAP else m.end())
    return max(ends)


def _secret_spans(text: str) -> list[tuple[int, int]]:
    # Every pattern reads the original text and only reports what to redact; the spans are merged
    # at the end. So no pattern can rewrite or consume a key another one needs (a URL query that
    # runs into the next JSON field, a value that runs into the next key): at worst a span
    # over-redacts, it never leaves part of a secret behind.
    spans = [(m.end("head"), m.end()) for m in _URL_PASSWORD.finditer(text)]
    spans += [(m.end("head") + 1, m.end()) for m in _URL_QUERY.finditer(text)]
    # A libpq value can run through a neighbouring field's "Bearer" label, so tokens are found here.
    spans += [(m.start("token"), m.end()) for m in _BEARER_TOKEN.finditer(text)]
    # A URL past the pattern bounds can still carry a password or a query. Decide that here, on the
    # original token: once another span redacts part of it (token=x@h), its "@" or "?" is gone.
    for tok in _NON_SPACE.finditer(text):
        url = tok.group()
        if "://" in url and (
            url.count("@") != len(_URL_PASSWORD.findall(url)) or url.count("?") != len(_URL_QUERY.findall(url))
        ):
            spans.append(tok.span())
    keys = list(_KV_KEY.finditer(text)) + list(_KV_PII_KEY.finditer(text))
    if keys:
        nxt = _next_unescaped_whitespace(text)
        last_quote = {q: _last_quote_in_line(text, q) for q in _QUOTED_AT}
        spans += [(m.end(), _value_end(text, m.end(), nxt, last_quote)) for m in keys]
    return [(a, b) for a, b in spans if b > a]


def _redact(text: str, spans: list[tuple[int, int]]) -> str:
    out: list[str] = []
    pos = 0
    for a, b in sorted(spans):
        if b <= pos:
            continue
        a = max(a, pos)
        if a == pos and out and out[-1] == "***":
            pos = b  # touching the previous span: extend it
            continue
        out += [text[pos:a], "***"]
        pos = b
    out.append(text[pos:])
    return "".join(out)


def scrub(text: str) -> str:
    if len(text) > MAX_INPUT_CHARS:
        head = text[:MAX_INPUT_CHARS]
        cut = max(head.rfind(" "), head.rfind("\n"), head.rfind("\t"))
        text = head[:cut] if cut > 0 else ""
    text = _redact(text, _secret_spans(text))
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
