#!/usr/bin/env python3
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


if __name__ == "__main__":
    unittest.main()
