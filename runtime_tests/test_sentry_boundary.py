"""A real server 500 reaches Sentry, scrubbed (the core boundary would otherwise hide it).

Offline: the request fails before touching the database; Sentry uses an in-memory transport.
"""
from typing import Any
from uuid import uuid4

import sentry_sdk
from fastapi import HTTPException
from fastapi.testclient import TestClient
from platform_backend_core import Principal

from platform_media import _sentry


class _Database:
    def ready(self):
        return False

    def close(self):
        pass

    def transaction(self, workspace):
        raise AssertionError('database must not be reached')


class _Verifier:
    def verify(self, authorization):
        if authorization != 'Bearer fixture':
            raise HTTPException(401, 'authentication required')
        return Principal(uuid4(), uuid4(), frozenset(['files:read']))


def test_a_server_500_reaches_sentry_scrubbed(monkeypatch):
    # The shared core's boundary turns unexpected exceptions into a redacted 500 before an outer
    # ASGI layer sees them, so the app must capture them itself (inside that boundary).
    from platform_media import create_app

    events: list[dict[str, Any]] = []

    class _Memory(sentry_sdk.transport.Transport):
        def capture_envelope(self, envelope):
            events.extend(item.payload.json for item in envelope.items if item.type == 'event')

    real_init = sentry_sdk.init
    # The production configuration, with only the transport swapped for memory.
    monkeypatch.setattr(sentry_sdk, 'init', lambda **kw: real_init(transport=_Memory, **kw))
    monkeypatch.setenv('SENTRY_DSN', 'https://k@o0.ingest.sentry.io/1')
    _sentry.init_sentry()
    # Built from parts so the source lines Sentry attaches do not contain the values either.
    message = 'db password=' + 'pw-' + 'SRV500' + ' at ' + '203.0' + '.113.9'
    try:
        def broken(*args, **kwargs):
            raise RuntimeError(message)
        monkeypatch.setattr(_Database, 'transaction', broken)
        with TestClient(create_app(_Database(), _Verifier(), object_store=object())) as c:
            r = c.get('/v1/files', headers={'Authorization': 'Bearer fixture'})
        assert r.status_code == 500 and r.json() == {'detail': 'internal server error'}
        sentry_sdk.flush()
        assert len(events) == 1
        # Reported as unhandled, so unhandled-error filters and alerts still see server failures.
        assert events[0]['exception']['values'][-1]['mechanism']['handled'] is False
        text = str(events[0])
        assert 'RuntimeError' in text and 'pw-SRV500' not in text and '203.0' '.113.9' not in text
    finally:
        # dsn=None would read SENTRY_DSN (still set here) and start a real, unscrubbed client.
        real_init(dsn='')
    assert sentry_sdk.get_client().transport is None  # nothing can leave the test process
