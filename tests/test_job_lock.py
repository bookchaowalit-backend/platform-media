from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from book_media_platform.graphics import GraphicsError, service
from book_media_platform.graphics.service import _JobLock, _lock_is_stale


def dead_pid() -> int:
    """Return the pid of a process that has already exited."""
    process = subprocess.Popen([sys.executable, "-c", "pass"])
    process.wait()
    return process.pid


class StaleJobLockTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.lock_path = Path(self.temp.name) / "job.lock"
        # Keep the "live lock" cases fast; production stays at 15 s.
        self.timeout = patch.object(service, "LOCK_TIMEOUT_SECONDS", 0.3)
        self.timeout.start()

    def tearDown(self) -> None:
        self.timeout.stop()
        self.temp.cleanup()

    def write_lock(self, **owner: object) -> None:
        self.lock_path.write_text(json.dumps(owner), encoding="utf-8")

    def acquire_quickly(self) -> float:
        started = time.monotonic()
        with _JobLock(self.lock_path):
            owner = json.loads(self.lock_path.read_text(encoding="utf-8"))
            self.assertEqual(owner["pid"], os.getpid())
            self.assertEqual(owner["host"], socket.gethostname())
        self.assertFalse(self.lock_path.exists(), "lock must be released on exit")
        return time.monotonic() - started

    def test_lock_left_by_a_crashed_process_is_reclaimed(self) -> None:
        self.write_lock(pid=dead_pid(), host=socket.gethostname(), created_at=time.time())
        self.assertLess(self.acquire_quickly(), 0.25)

    def test_legacy_bare_pid_lock_from_a_dead_process_is_reclaimed(self) -> None:
        self.lock_path.write_text(str(dead_pid()), encoding="ascii")
        self.assertLess(self.acquire_quickly(), 0.25)

    def test_lock_held_by_a_live_process_still_blocks(self) -> None:
        self.write_lock(pid=os.getpid(), host=socket.gethostname(), created_at=time.time())
        with self.assertRaises(GraphicsError), _JobLock(self.lock_path):
            pass
        self.assertTrue(self.lock_path.exists(), "a live holder's lock must not be removed")

    def test_lock_from_another_host_is_respected_until_it_is_too_old(self) -> None:
        now = time.time()
        fresh = {"pid": dead_pid(), "host": "some-other-host", "created_at": now}
        self.assertFalse(_lock_is_stale(fresh, now, now))
        old = dict(fresh, created_at=now - service.STALE_LOCK_MAX_AGE_SECONDS - 1)
        self.assertTrue(_lock_is_stale(old, now, now))

    def test_very_old_lock_is_stale_even_if_the_pid_was_reused(self) -> None:
        now = time.time()
        owner = {"pid": os.getpid(), "host": socket.gethostname(), "created_at": now - service.STALE_LOCK_MAX_AGE_SECONDS - 1}
        self.assertTrue(_lock_is_stale(owner, now, now))

    def test_empty_lock_is_stale_only_after_the_write_grace_period(self) -> None:
        now = time.time()
        self.assertFalse(_lock_is_stale({}, now, now))
        self.assertTrue(_lock_is_stale({}, now - service.LOCK_WRITE_GRACE_SECONDS - 1, now))

    def test_empty_lock_from_a_crash_is_reclaimed(self) -> None:
        self.lock_path.write_bytes(b"")
        old = time.time() - service.LOCK_WRITE_GRACE_SECONDS - 1
        os.utime(self.lock_path, (old, old))
        self.assertLess(self.acquire_quickly(), 0.25)

    def test_no_stale_tombstones_are_left_behind(self) -> None:
        self.write_lock(pid=dead_pid(), host=socket.gethostname(), created_at=time.time())
        self.acquire_quickly()
        self.assertEqual(list(self.lock_path.parent.iterdir()), [])


class JobLockHeartbeatTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.lock_path = Path(self.temp.name) / "job.lock"
        self.patches = [
            patch.object(service, "LOCK_TIMEOUT_SECONDS", 0.3),
            patch.object(service, "LOCK_HEARTBEAT_SECONDS", 0.02),
        ]
        for p in self.patches:
            p.start()

    def tearDown(self) -> None:
        for p in reversed(self.patches):
            p.stop()
        self.temp.cleanup()

    def test_new_locks_advertise_a_heartbeat(self) -> None:
        with _JobLock(self.lock_path):
            owner = json.loads(self.lock_path.read_text(encoding="utf-8"))
        self.assertGreater(owner["heartbeat_seconds"], 0)

    def test_heartbeat_keeps_a_long_run_from_being_reclaimed(self) -> None:
        with _JobLock(self.lock_path):
            # Simulate a run that started hours ago: backdate the lock.
            long_ago = time.time() - service.STALE_LOCK_MAX_AGE_SECONDS - 3600
            os.utime(self.lock_path, (long_ago, long_ago))
            deadline = time.monotonic() + 2
            while self.lock_path.stat().st_mtime < time.time() - 5 and time.monotonic() < deadline:
                time.sleep(0.01)
            self.assertGreater(self.lock_path.stat().st_mtime, time.time() - 5, "heartbeat refreshed mtime")
            owner = json.loads(self.lock_path.read_text(encoding="utf-8"))
            owner["created_at"] = long_ago
            self.assertFalse(_lock_is_stale(owner, self.lock_path.stat().st_mtime, time.time()))
            with self.assertRaises(GraphicsError), _JobLock(self.lock_path):
                pass
        self.assertFalse(self.lock_path.exists())

    def test_heartbeat_thread_stops_on_release(self) -> None:
        lock = _JobLock(self.lock_path)
        with lock:
            thread = lock._heartbeat
            self.assertIsNotNone(thread)
            self.assertTrue(thread.is_alive())
        self.assertFalse(thread.is_alive())

    def test_silent_heartbeat_lock_is_stale_from_another_host(self) -> None:
        now = time.time()
        owner = {"pid": 4242, "host": "some-other-host", "created_at": now, "heartbeat_seconds": 30}
        self.assertFalse(_lock_is_stale(owner, now - 60, now))
        self.assertTrue(_lock_is_stale(owner, now - service.HEARTBEAT_STALE_AFTER_SECONDS - 1, now))

    def test_long_running_heartbeat_lock_from_another_host_is_respected(self) -> None:
        now = time.time()
        owner = {
            "pid": 4242,
            "host": "some-other-host",
            "created_at": now - service.STALE_LOCK_MAX_AGE_SECONDS * 5,
            "heartbeat_seconds": 30,
        }
        self.assertFalse(_lock_is_stale(owner, now - 10, now))


class ConcurrentReclaimTests(unittest.TestCase):
    """Reclaiming a stale lock with three or more contenders keeps one holder."""

    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.lock_path = Path(self.temp.name) / "job.lock"
        self.timeout = patch.object(service, "LOCK_TIMEOUT_SECONDS", 10)
        self.timeout.start()

    def tearDown(self) -> None:
        self.timeout.stop()
        self.temp.cleanup()

    def write_stale_lock(self) -> bytes:
        raw = json.dumps({"pid": dead_pid(), "host": socket.gethostname(), "created_at": time.time()}).encode()
        self.lock_path.write_bytes(raw)
        return raw

    def test_reclaimer_rechecks_under_the_guard_and_spares_a_new_live_lock(self) -> None:
        # Contender A judged the old lock stale; meanwhile B reclaimed it and
        # C created a fresh live lock. A must leave C's lock alone even if a
        # third run D would grab the path the moment it is free.
        stale_raw = self.write_stale_lock()
        stale_view = service._read_lock(self.lock_path)
        live = _JobLock(self.lock_path)
        self.lock_path.unlink()
        live.__enter__()
        try:
            live_raw = self.lock_path.read_bytes()
            self.assertNotEqual(live_raw, stale_raw)
            reads = iter([stale_view])
            real_read = service._read_lock
            real_replace = os.replace

            def first_read_is_stale(path: Path):
                return next(reads, None) or real_read(path)

            def replace_then_third_run_creates(src, dst):
                real_replace(src, dst)
                if Path(src) == self.lock_path:
                    Path(src).write_bytes(b'{"pid": 1, "note": "third run"}')

            with patch.object(service, "_read_lock", side_effect=first_read_is_stale), \
                    patch.object(service.os, "replace", side_effect=replace_then_third_run_creates):
                _JobLock(self.lock_path)._break_if_stale()
            self.assertEqual(self.lock_path.read_bytes(), live_raw, "a live lock was stolen")
            self.assertTrue(live.owns_lock())
        finally:
            live.__exit__(None, None, None)
        self.assertEqual(list(self.lock_path.parent.iterdir()), [])

    def test_displaced_holder_refuses_to_commit_and_keeps_the_new_lock(self) -> None:
        lock = _JobLock(self.lock_path)
        with lock:
            lock.ensure_held()
            # Simulate a lost restore race: the path now names another run's lock.
            other = self.lock_path.with_name("other")
            other.write_bytes(b'{"pid": 1}')
            os.replace(other, self.lock_path)
            self.assertFalse(lock.owns_lock())
            with self.assertRaises(GraphicsError):
                lock.ensure_held()
        self.assertEqual(self.lock_path.read_bytes(), b'{"pid": 1}', "exit must not delete another run's lock")

    def test_many_contenders_on_a_stale_lock_never_overlap(self) -> None:
        self.write_stale_lock()
        contenders = 8
        barrier = threading.Barrier(contenders)
        guard = threading.Lock()
        state = {"holding": 0, "max": 0, "acquired": 0}
        errors: list[BaseException] = []

        def contend() -> None:
            try:
                barrier.wait()
                lock = _JobLock(self.lock_path)
                with lock:
                    with guard:
                        state["holding"] += 1
                        state["max"] = max(state["max"], state["holding"])
                        state["acquired"] += 1
                    time.sleep(0.01)
                    lock.ensure_held()
                    with guard:
                        state["holding"] -= 1
            except BaseException as exc:  # noqa: BLE001 - surface in the main thread
                errors.append(exc)

        threads = [threading.Thread(target=contend) for _ in range(contenders)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(30)
        self.assertEqual(errors, [])
        self.assertEqual(state["acquired"], contenders)
        self.assertEqual(state["max"], 1, "two runs held the lock at once")
        self.assertEqual(list(self.lock_path.parent.iterdir()), [])

    def test_fresh_reclaim_guard_defers_to_the_running_reclaimer(self) -> None:
        self.write_stale_lock()
        guard = self.lock_path.with_name("job.lock.reclaim")
        guard.write_bytes(b"")
        self.assertFalse(_JobLock(self.lock_path)._break_if_stale())
        self.assertTrue(self.lock_path.exists())

    def test_reclaim_guard_left_by_a_crash_is_cleared(self) -> None:
        self.write_stale_lock()
        guard = self.lock_path.with_name("job.lock.reclaim")
        guard.write_bytes(b"")
        old = time.time() - service.RECLAIM_GUARD_STALE_SECONDS - 1
        os.utime(guard, (old, old))
        with _JobLock(self.lock_path):
            pass
        self.assertEqual(list(self.lock_path.parent.iterdir()), [])


if __name__ == "__main__":
    unittest.main()
