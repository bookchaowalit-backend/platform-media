"""Sentry is off without SENTRY_DSN; with it: errors only, no PII, locals, bodies or secrets."""

from __future__ import annotations

import importlib
import importlib.util
import io
import os
import sys
import unittest
from contextlib import redirect_stderr
from pathlib import Path
from typing import Any
from unittest import mock

# Loaded by path: the module is standard library only (sentry_sdk is imported lazily), so these
# tests run in the offline CI job too, where the package's runtime dependencies are not installed.
_PATH = Path(__file__).resolve().parents[1] / "src" / "platform_media" / "_sentry.py"
_spec = importlib.util.spec_from_file_location("platform_media_sentry_under_test", _PATH)
assert _spec is not None and _spec.loader is not None
_sentry = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_sentry)

_HAS_SDK = importlib.util.find_spec("sentry_sdk") is not None


def _env_without(*names: str) -> dict[str, str]:
    return {k: v for k, v in os.environ.items() if k not in names}


class SentryTests(unittest.TestCase):
    def test_package_init_calls_init_sentry(self):
        init = (_PATH.parent / "__init__.py").read_text(encoding="utf-8")
        self.assertIn("from ._sentry import init_sentry as _init_sentry", init)
        self.assertIn("\n_init_sentry()\n", init)

    def test_no_dsn_is_a_noop(self):
        calls: list[dict[str, Any]] = []
        fake = type("Fake", (), {"init": staticmethod(lambda **kw: calls.append(kw))})
        with (
            mock.patch.dict(os.environ, _env_without("SENTRY_DSN"), clear=True),
            mock.patch.dict(sys.modules, {"sentry_sdk": fake}),
        ):
            _sentry.init_sentry()
        self.assertEqual(calls, [])

    def test_blank_dsn_is_a_noop(self):
        with (
            mock.patch.dict(os.environ, {"SENTRY_DSN": "  "}, clear=True),
            mock.patch.dict(sys.modules, {"sentry_sdk": None}),
        ):
            _sentry.init_sentry()  # importing sentry_sdk here would raise

    def test_capture_is_a_noop_without_dsn(self):
        with (
            mock.patch.dict(os.environ, _env_without("SENTRY_DSN"), clear=True),
            mock.patch.dict(sys.modules, {"sentry_sdk": None}),
        ):
            _sentry.capture(RuntimeError("x"))  # importing sentry_sdk here would raise

    @unittest.skipIf(not _HAS_SDK, "sentry-sdk not installed")
    def test_dsn_initialises_errors_only_without_pii(self):
        sentry_sdk = importlib.import_module("sentry_sdk")
        calls: list[dict[str, Any]] = []
        tags: dict[str, str] = {}
        env = _env_without("SENTRY_ENVIRONMENT", "SENTRY_RELEASE")
        env.update(
            SENTRY_DSN="https://public@o0.ingest.sentry.io/0",
            RAILWAY_SERVICE_NAME="media-api",
            RAILWAY_GIT_COMMIT_SHA="abc123",
        )
        err = io.StringIO()
        with (
            mock.patch.dict(os.environ, env, clear=True),
            mock.patch.object(sentry_sdk, "init", lambda **kw: calls.append(kw)),
            mock.patch.object(sentry_sdk, "set_tag", lambda k, v: tags.__setitem__(k, v)),
            redirect_stderr(err),
        ):
            _sentry.init_sentry()
        kw = calls[0]
        self.assertEqual(kw["environment"], "production")
        self.assertEqual(kw["release"], "abc123")
        self.assertIsNone(kw["traces_sample_rate"])
        self.assertIs(kw["send_default_pii"], False)
        self.assertIs(kw["include_local_variables"], False)
        self.assertEqual(kw["max_request_body_size"], "never")
        self.assertIs(kw["before_send"], _sentry._scrub_event)
        (logging_integration,) = kw["integrations"]
        self.assertIsNone(logging_integration._handler)
        self.assertIsNone(logging_integration._breadcrumb_handler)
        self.assertIsNone(logging_integration._sentry_logs_handler)
        self.assertEqual(tags, {"service": "media-api"})
        self.assertIn("sentry: enabled for media-api", err.getvalue())

    def test_scrub_removes_url_passwords_tokens_and_jwts(self):
        text = (
            "connect postgresql://svc:"
            "s3cr%40t"  # split so secret scanners do not read a synthetic URL as a credential
            "@ep-x.neon.tech/db failed; "
            "password=hunter2 Authorization: Bearer abc.def-ghi "
            "eyJhbGciOiJSUzI1NiJ9" "." "eyJzdWIiOiJvd25lciJ9" "." "c2lnbmF0dXJl"
        )
        out = _sentry.scrub(text)
        for secret in ("s3cr%40t", "hunter2", "abc.def-ghi", "eyJhbGciOiJSUzI1NiJ9"):
            self.assertNotIn(secret, out)
        self.assertIn("postgresql://svc:***@ep-x.neon.tech/db", out)

    def test_scrub_removes_emails_phones_and_recipient_values(self):
        text = (
            "deliver to alice.smith+tag@example.co.th failed; sms +66812345678 rejected; "
            "recipient=bob-handle address: 12/3-Sukhumvit "
            'payload {"to": "carol@example.org", "email": "dave@x.io"}'
        )
        out = _sentry.scrub(text)
        for pii in (
            "alice.smith",
            "example.co.th",
            "66812345678",
            "bob-handle",
            "Sukhumvit",
            "carol@",
            "dave@",
        ):
            self.assertNotIn(pii, out)

    def test_scrub_event_drops_request_user_extra_and_breadcrumb_data(self):
        event = {
            "exception": {
                "values": [{"value": "send to eve@example.com via postgres://u:pw@h/db"}]
            },
            "request": {
                "method": "POST",
                "url": "https://x/api?token=t",
                "data": {"recipient": "eve@example.com"},
                "headers": {"authorization": "Bearer t", "cookie": "s=1"},
                "cookies": {"s": "1"},
                "query_string": "token=t",
            },
            "user": {"email": "eve@example.com", "ip_address": "203.0.113.9"},
            "breadcrumbs": {
                "values": [
                    {"message": "GET with Bearer abc", "data": {"url": "https://x?token=t"}}
                ]
            },
            "extra": {"sys.argv": ["--token", "t"]},
        }
        out = _sentry._scrub_event(event, {})
        value = out["exception"]["values"][0]["value"]
        self.assertNotIn("eve@example.com", value)
        self.assertIn("postgres://u:***@h/db", value)
        self.assertEqual(out["request"], {"method": "POST"})
        self.assertNotIn("user", out)
        self.assertNotIn("data", out["breadcrumbs"]["values"][0])
        self.assertNotIn("abc", out["breadcrumbs"]["values"][0]["message"])
        self.assertNotIn("extra", out)
        self.assertNotIn("eve@example.com", str(out))

    def test_scrub_event_keeps_only_scrubbed_formatted_log_text(self):
        token = "opaque" + "-token-value"
        event = {
            "logentry": {
                "message": "Authorization: Bearer %s",
                "params": [token],
                "formatted": "Authorization: Bearer " + token,
            }
        }
        out = _sentry._scrub_event(event, {})
        self.assertEqual(out["logentry"], {"message": "Authorization: Bearer ***"})
        self.assertNotIn(token, str(out))

    def test_scrub_event_drops_dynamic_sampling_context(self):
        event = {
            "contexts": {
                "trace": {"trace_id": "t", "dynamic_sampling_context": {"user_id": "a"}}
            }
        }
        out = _sentry._scrub_event(event, {})
        self.assertEqual(out["contexts"]["trace"], {"trace_id": "t"})


class HardeningTests(unittest.TestCase):
    """Ported from platform-security 7deddff: bounded patterns, fail-closed, quoted values, IPs."""

    def test_app_reports_errors_inside_the_core_boundary(self):
        # The shared core's boundary turns unexpected exceptions into a redacted 500 before an
        # outer ASGI layer sees them; the runtime test proves the event, this pins the wiring.
        app = (_PATH.parent / "app.py").read_text(encoding="utf-8")
        self.assertIn("app.user_middleware.append(Middleware(CaptureErrors))", app)

    def test_capture_errors_reraises_and_reports(self):
        captured: list[BaseException] = []
        fake = type("Fake", (), {"capture_exception": staticmethod(captured.append)})

        async def broken(scope, receive, send):
            raise RuntimeError("boom")

        import asyncio

        with mock.patch.dict(sys.modules, {"sentry_sdk": fake}), self.assertRaises(RuntimeError):
            asyncio.run(_sentry.CaptureErrors(broken)({}, None, None))
        self.assertEqual([str(e) for e in captured], ["boom"])

    def test_scrub_removes_ips(self):
        out = _sentry.scrub("from 203.0.113.77 and 2001:db8:85a3:0:0:8a2e:370:7334")
        for ip in ("203.0.113.77", "8a2e:370:7334"):
            self.assertNotIn(ip, out)

    def test_scrub_catches_prefixed_quoted_and_query_string_secrets(self):
        for text, secret in [
            ("refresh failed: access_token=tok-AAA111 expired", "tok-AAA111"),
            ("client_secret=cs-BBB222&grant_type=client_credentials", "cs-BBB222"),
            ("bad config {'password': 'pw-CCC333', 'name': 'x'}", "pw-CCC333"),
            ('bad body {"api_key": "ak-DDD444"}', "ak-DDD444"),
            ('DB_PASSWORD="pw-EEE555" rejected', "pw-EEE555"),
            ("GET https://h.example/cb?code=c-FFF666&state=s-GGG777 failed", "c-FFF666"),
        ]:
            with self.subTest(text=text):
                out = _sentry.scrub(text)
                self.assertNotIn(secret, out)
                self.assertNotIn("s-GGG777", out)

    def test_scrub_complete_quoted_values_and_compressed_ipv6(self):
        for text, secret in [
            ("login password='correct horse battery staple' failed", "battery staple"),
            ("cfg {'password': 'prefix,SECRET_SUFFIX'}", "SECRET_SUFFIX"),
            ('cfg {"client_secret": "a\\"b SECRET_ESC"}', "SECRET_ESC"),
            ('payload {"recipient": "Jane Q Public, 1 Main Rd"}', "1 Main Rd"),
            ("dial from 2001:db8::7334 refused", "2001:db8::7334"),
            ("peer fe80::1ff:fe23:4567:890a closed", "fe80::1ff"),
            ("bind ::1 ok", "::1"),
        ]:
            with self.subTest(text=text):
                self.assertNotIn(secret, _sentry.scrub(text))

    def test_scrub_keeps_rust_paths_and_times(self):
        for text in ("std::vec::Vec failed", "at 12:30:45 UTC", "Foo::bar() raised"):
            with self.subTest(text=text):
                self.assertEqual(_sentry.scrub(text), text)

    def test_scrub_event_tolerates_missing_or_odd_fields(self):
        for value in (None, 42):
            event = {"exception": {"values": [{"value": value}]}, "request": "x", "logentry": None}
            out = _sentry._scrub_event(event, {})
            self.assertEqual(out["exception"]["values"][0]["value"], value)

    def test_scrub_is_bounded_on_long_hostile_input(self):
        import time

        n = _sentry.MAX_INPUT_CHARS - 64  # under the pre-cap, so every pattern really runs
        for hostile in (
            "a" * n, "x_" * (n // 2), "a://" * (n // 4), "a." * (n // 2), "%" * n,
            "a://b:" * (n // 6), "x@" + "a-" * (n // 2), "eyJ-" * (n // 4), "+1" * (n // 2),
            "to=" * (n // 3), 'to: "' * (n // 5), "a" * 200_000,
        ):
            start = time.perf_counter()
            out = _sentry.scrub(hostile + " password")
            # Linear work takes about 0.3 s on a laptop and up to 2x that on a slow CI runner; a
            # super-linear pattern on 64 KiB would take far longer.
            self.assertLess(time.perf_counter() - start, 1.5)
            self.assertLessEqual(len(out), _sentry.MAX_SCRUB_CHARS + len(_sentry.TRUNCATED))

    def test_scrub_fails_closed_at_bounds_and_cap(self):
        for text, secret in [
            # a credential straddling the length cap must not survive as a partial token
            ("x " * 4080 + "postgresql://u:" + "S" * 40 + "@h/db", "SSSSSSSSSS"),
            # past the pattern bounds the whole URL token is redacted (fail closed)
            ("dial postgresql://" + "u" * 300 + ":pw-HHH888@h/db", "pw-HHH888"),
            ("GET https://h.example/" + "p" * 3000 + "?code=c-III999 failed", "c-III999"),
            ("dial postgresql://u:" + "P" * 600 + "@h/db", "PPPPPPPPPP"),
            ("from " + "L" * 100 + "@example.com", "LLLLLLLLLL"),
            # one scrubbed URL must not vouch for an unscrubbed one in the same token
            ("a://x:pw-OK1@h/b://" + "u" * 300 + ":pw-ZZZ000@h2", "pw-ZZZ000"),
            ("https://h/?a=1&r=https://" + "h" * 600 + "/p?code=c-YYY000", "c-YYY000"),
            ("token eyJhbGciOiJSUzI1NiJ9." + "Q" * 5000 + ".sig-XXX000 rejected", "sig-XXX000"),
            (
                "token eyJhbGciOiJSUzI1NiJ9" "." "eyJzdWIiOiJ4In0" "." + "S" * 4096
                + "TAIL-WWW000 rejected",
                "TAIL-WWW000",
            ),
        ]:
            with self.subTest(secret=secret):
                out = _sentry.scrub(text)
                self.assertNotIn(secret, out)
                self.assertLessEqual(len(out), _sentry.MAX_SCRUB_CHARS + len(_sentry.TRUNCATED))


if __name__ == "__main__":
    unittest.main()
