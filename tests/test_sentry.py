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
        # No tracing also means no forwarding of incoming sentry-trace/baggage (user ids).
        self.assertEqual(kw["trace_propagation_targets"], [])
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

    def test_capture_errors_reports_unhandled_and_reraises(self):
        captured: list[tuple[Any, Any]] = []
        mechanisms: list[dict[str, Any]] = []

        def event_from_exception(exc, client_options=None, mechanism=None):
            mechanisms.append(mechanism)
            return {"exception": str(exc)}, {"exc": exc}

        client = type("Client", (), {"options": {}, "transport": object(), "is_active": lambda self: True})()
        sdk = type("Sdk", (), {
            "get_client": staticmethod(lambda: client),
            "capture_event": staticmethod(lambda event, hint=None: captured.append((event, hint))),
        })
        utils = type("Utils", (), {"event_from_exception": staticmethod(event_from_exception)})

        async def broken(scope, receive, send):
            raise RuntimeError("boom")

        import asyncio

        with (
            mock.patch.dict(sys.modules, {"sentry_sdk": sdk, "sentry_sdk.utils": utils}),
            self.assertRaises(RuntimeError),
        ):
            asyncio.run(_sentry.CaptureErrors(broken)({}, None, None))
        self.assertEqual([event for event, _ in captured], [{"exception": "boom"}])
        self.assertEqual(mechanisms, [{"type": "asgi", "handled": False}])

    def test_capture_errors_skips_building_an_event_without_a_transport(self):
        # Building the event reads traceback locals; with nothing to send it must not happen at all.
        built: list[Any] = []
        for client in (
            type("Off", (), {"options": {}, "transport": None, "is_active": lambda self: False})(),
            type("NoTransport", (), {"options": {}, "transport": None, "is_active": lambda self: True})(),
        ):
            sdk = type("Sdk", (), {
                "get_client": staticmethod(lambda client=client: client),
                "capture_event": staticmethod(lambda event, hint=None: built.append(event)),
            })
            def event_from_exception(*args: Any, **kwargs: Any) -> tuple[dict[str, Any], dict[str, Any]]:
                built.append(args)
                return {}, {}

            utils = type("Utils", (), {"event_from_exception": staticmethod(event_from_exception)})

            async def broken(scope, receive, send):
                raise RuntimeError("boom")

            import asyncio

            with (
                mock.patch.dict(sys.modules, {"sentry_sdk": sdk, "sentry_sdk.utils": utils}),
                self.assertRaises(RuntimeError),
            ):
                asyncio.run(_sentry.CaptureErrors(broken)({}, None, None))
        self.assertEqual(built, [])

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
            "to=" * (n // 3), 'to: "' * (n // 5), "token=" * (n // 6), "pwd:" * (n // 4),
            'token="' * (n // 7), 'recipient="' * (n // 11), "a" * 200_000,
        ):
            start = time.perf_counter()
            out = _sentry.scrub(hostile + " password")
            # ~0.3 s locally, up to 2x on a slow CI runner; the quadratic patterns this guards
            # against took 3 s to over 120 s on the same input.
            self.assertLess(time.perf_counter() - start, 2.0)
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


class SpanScrubberTests(unittest.TestCase):
    """Ported from platform-security fbf5eef, plus bearer and recipient spans found on the original text."""

    def _assert_scrubbed(self, cases):
        for text, secret in cases:
            with self.subTest(text=text):
                self.assertNotIn(secret, _sentry.scrub(text))

    def test_scrub_escaped_newlines_and_colon_prefixed_ipv6(self):
        self._assert_scrubbed([
            ("cfg password='abc\\\nSECRET_NL tail' end", "SECRET_NL"),
            ('cfg {"token": "x\\\ny SECRET_NL2"}', "SECRET_NL2"),
            ("peer:2001:db8::1 closed", "2001:db8"),
            ("addr=fe80::1:2: refused", "fe80::1"),
            ("dial [::1]:8080 failed", "::1"),
        ])

    def test_scrub_complete_unquoted_libpq_values(self):
        for text, secret in [
            ("connect host=db password=,SENSITIVE_VALUE dbname=x", "SENSITIVE_VALUE"),
            ("connect host=db password=prefix,SECRET_SUFFIX dbname=x", "SECRET_SUFFIX"),
            ("connect password=a\\ SECRET_SPACE dbname=x", "SECRET_SPACE"),
            ("connect password=pre}SECRET_BRACE x", "SECRET_BRACE"),
            ("connect password=pre'SECRET_QUOTE x", "SECRET_QUOTE"),
        ]:
            with self.subTest(text=text):
                out = _sentry.scrub(text)
                self.assertNotIn(secret, out)
                self.assertTrue("dbname=x" in out or "dbname" not in text)

    def test_scrub_adjacent_structured_fields(self):
        self._assert_scrubbed([
            ('{"token":null,"password":"prefix SECRET_SUFFIX"}', "SECRET_SUFFIX"),
            ("{'token': 1, 'client_secret': 'a b SECRET_TWO'}", "SECRET_TWO"),
            ('password=x,api_key="k SECRET_THREE"', "SECRET_THREE"),
            ('{"pwd":"","secret":"s SECRET_FOUR"}', "SECRET_FOUR"),
        ])

    def test_scrub_patterns_cannot_consume_each_others_keys(self):
        for text, secret in [
            ("password=foo,token= sslpassword='prefix SECRET_SUFFIX' dbname=x", "SECRET_SUFFIX"),
            ('{"url":"https://example.test/?x=1","password":"prefix SECRET_SUFFIX"}', "SECRET_SUFFIX"),
            ('see https://h/p?token=t-AAA&next=1 and {"secret":"s b SECRET_B"}', "SECRET_B"),
            ('{"token":"a b SECRET_C","url":"https://h/?q=1"}', "SECRET_C"),
            ('token=token=token= password="x y SECRET_D"', "SECRET_D"),
        ]:
            with self.subTest(text=text):
                out = _sentry.scrub(text)
                self.assertNotIn(secret, out)
                self.assertNotIn("t-AAA", out)

    @unittest.skipIf(importlib.util.find_spec("psycopg") is None, "psycopg not installed")
    def test_scrub_serialised_libpq_dsn_with_escaped_quote(self):
        # repr()/str() of a libpq DSN doubles its backslashes, so the escaped quote looks like the
        # closing one; the redaction must still cover the whole password.
        make_conninfo = importlib.import_module("psycopg.conninfo").make_conninfo
        dsn = make_conninfo(password="a' b " + "SECRET_DSN")
        for text in (repr(dsn), repr({"dsn": dsn}), str(ValueError("connect", dsn)), dsn):
            self.assertNotIn("SECRET_DSN", _sentry.scrub(text))

    def test_scrub_value_starting_with_an_escaped_quote(self):
        self._assert_scrubbed([
            ("cfg password=\\'a \"b\" c SECRET_EQ\\' end", "SECRET_EQ"),
            ("cfg password=\\\"a 'b' c SECRET_EQ\\\" end", "SECRET_EQ"),
        ])

    def test_scrub_bearer_token_behind_a_consumed_label(self):
        # A libpq value can run through an adjacent field's "Bearer" label; the token must still go.
        self._assert_scrubbed([
            ("password=p,Authorization=Bearer OPAQUE_CRED", "OPAQUE_CRED"),
            ('{"password":null,"authorization":"Bearer SENSITIVE_VALUE"}', "SENSITIVE_VALUE"),
            ('{"password":"pw","authorization":"Bearer TOP_SECRET_TOKEN"}', "TOP_SECRET_TOKEN"),
            ("password=prefix&private-suffix host=db", "private-suffix"),
            ("password=p,api_key= \\OPAQUE_BS", "OPAQUE_BS"),
        ])

    def test_scrub_whole_bearer_token_past_any_length_bound(self):
        # Built from parts; a tail past a span bound must not survive behind the "***".
        out = _sentry.scrub("auth Bearer " + "A" * 5000 + "TAIL_" + "SECRET x")
        self.assertNotIn("TAIL_SECRET", out)
        self.assertNotIn("AAAA", out)

    def test_scrub_overlong_url_password_decided_on_the_original_text(self):
        # An overlong URL password holding a key=value: the key's value span must not erase the
        # "@" that tells the fail-closed pass this token is still a credential URL.
        for text in (
            "dial postgresql://u:" + "P" * 600 + "token=x@h/db failed",
            "dial https://u:" + "P" * 600 + "secret=x@h failed",
        ):
            with self.subTest(text=text[:30]):
                self.assertNotIn("PPPPPPPPPP", _sentry.scrub(text))

    def test_scrub_overlapping_bearer_labels_and_trailing_backslash(self):
        self._assert_scrubbed([
            # one label's token can be the next label ("Bearer Bearer X"); every token must still go
            ("auth Bearer Bearer SECRET_X done", "SECRET_X"),
            ("password=Bearer Authorization=Bearer OPAQUE_CRED", "OPAQUE_CRED"),
            # an unquoted value ending in a lone backslash keeps nothing
            ("password=abc\\", "\\"),
            ("password=\\", "\\"),
        ])

    def test_scrub_oversized_jwt_decided_on_the_original_text(self):
        # an oversized JWT with a dotted numeric tail: IP redaction must not erase the dots that mark it
        for text in (
            "token eyJhbGciOiJSUzI1NiJ9." + "Q" * 5000 + "-192.0.2.1 rejected",
            "token eyJhbGciOiJSUzI1NiJ9." + "Q" * 5000 + ".2001:db8::1 rejected",
        ):
            with self.subTest(text=text[-25:]):
                self.assertNotIn("QQQQQQQQ", _sentry.scrub(text))

    def test_scrub_nested_url_cannot_vouch_for_the_outer_one(self):
        # a nested URL's match must not vouch for the overlong outer credential URL around it
        self._assert_scrubbed([
            ("dial postgresql://u:" + "S" * 600 + "https://v:p@h/db failed", "SSSSSSSSSS"),
            ("dial postgresql://u:" + "S" * 600 + "x://a:b@c/d@h/db failed", "SSSSSSSSSS"),
        ])

    def test_scrub_email_behind_a_bearer_label(self):
        # a bearer span must not take an email's local part and leave its domain behind
        self._assert_scrubbed([
            ("password=Bearer user=alice@sensitive-customer.example", "sensitive-customer"),
            ("Authorization: Bearer alice@example.com", "example.com"),
            ("auth Bearer " + "A" * 5000 + "@tail-domain.example x", "tail-domain"),
        ])

    def test_scrub_bearer_token_with_an_at_suffix(self):
        # a bearer token followed by "@" that is not a full email must still go
        self._assert_scrubbed([
            ("https://h/?q=Bearer OPAQUE@internal", "OPAQUE"),
            ("auth Bearer TOKEN_Z@internal-host x", "internal-host"),
        ])

    def test_scrub_recipient_next_to_a_secret(self):
        # Recipient keys are found on the original text too, so a credential value cannot hide them.
        self._assert_scrubbed([
            ('{"password":null,"recipient":"Jane Q, 1 Main Rd"}', "1 Main Rd"),
            ('{"recipient":null,"password":"x y SECRET_PII"}', "SECRET_PII"),
            ("password=p,to=bob-handle x", "bob-handle"),
        ])


if __name__ == "__main__":
    unittest.main()
