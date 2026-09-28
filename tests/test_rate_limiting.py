"""Rate limiting must persist across processes.

The previous implementation kept counters in a module-level dict. That is
per-worker: running N uvicorn workers multiplies every limit by N, so
"5 signups per day" silently becomes "5 per worker per day". These tests pin
the counter to the database instead, and deliberately verify it through fresh
database sessions rather than the app's request path — a regression to
in-memory counting would pass a same-process test but fails these.
"""
import os
import secrets
import sys
import unittest
from datetime import datetime, timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.api import routes as routes_module
from src.database import SessionLocal, init_db
from src.models import RateLimitEvent


class RateLimitPersistenceTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        init_db()

    def setUp(self):
        routes_module._rate_prune()

    def tearDown(self):
        routes_module._rate_prune()

    def _fresh_session_count(self, bucket):
        """Count rows via a brand-new session (simulating another worker)."""
        session = SessionLocal()
        try:
            return session.query(RateLimitEvent).filter(
                RateLimitEvent.bucket == bucket).count()
        finally:
            session.close()

    def test_events_are_written_to_the_database(self):
        key = f"test-key-{secrets.token_hex(4)}"
        self.assertTrue(routes_module._rate_check(key, "user_burst"))
        # Visible from an independent session => not in process memory.
        self.assertEqual(
            self._fresh_session_count(f"user_burst:{key}"), 1)

    def test_limit_is_enforced_from_persisted_state(self):
        """The limit must count rows already in the DB, not local state."""
        key = f"enforce-{secrets.token_hex(4)}"
        # user_burst is 1 per 10 minutes.
        self.assertTrue(routes_module._rate_check(key, "user_burst"))
        self.assertFalse(routes_module._rate_check(key, "user_burst"))
        # After clearing process-local state entirely, the limit still holds.
        routes_module._RATE_BUCKETS.clear()
        self.assertFalse(
            routes_module._rate_check(key, "user_burst"),
            "limit was forgotten when in-process state was cleared",
        )
        self.assertEqual(self._fresh_session_count(f"user_burst:{key}"), 1)

    def test_rows_written_by_another_process_are_counted(self):
        """Simulate a second worker having already consumed the allowance."""
        key = f"other-worker-{secrets.token_hex(4)}"
        session = SessionLocal()
        try:
            session.add(RateLimitEvent(
                bucket=f"user_daily:{key}", created_at=datetime.utcnow()))
            session.commit()
        finally:
            session.close()
        # user_daily allows 5; one already used, so 4 more should pass.
        allowed = [routes_module._rate_check(key, "user_daily") for _ in range(5)]
        self.assertEqual(allowed, [True, True, True, True, False],
                         f"unexpected allowance pattern: {allowed}")
        self.assertEqual(self._fresh_session_count(f"user_daily:{key}"), 5)

    def test_expired_events_do_not_count(self):
        key = f"expired-{secrets.token_hex(4)}"
        session = SessionLocal()
        try:
            # user_burst window is 10 minutes; this is well outside it.
            session.add(RateLimitEvent(
                bucket=f"user_burst:{key}",
                created_at=datetime.utcnow() - timedelta(hours=2)))
            session.commit()
        finally:
            session.close()
        self.assertTrue(routes_module._rate_check(key, "user_burst"),
                        "an event older than the window still counted")

    def test_separate_keys_have_separate_allowances(self):
        a = f"alpha-{secrets.token_hex(4)}"
        b = f"beta-{secrets.token_hex(4)}"
        self.assertTrue(routes_module._rate_check(a, "user_burst"))
        self.assertFalse(routes_module._rate_check(a, "user_burst"))
        # A different subject is unaffected.
        self.assertTrue(routes_module._rate_check(b, "user_burst"))

    def test_limits_are_scoped_per_limit_name(self):
        """The same key under different limits must not interfere."""
        key = f"scoped-{secrets.token_hex(4)}"
        self.assertTrue(routes_module._rate_check(key, "user_burst"))
        self.assertFalse(routes_module._rate_check(key, "user_burst"))
        # user_daily permits 5, so it is still open for this key.
        self.assertTrue(routes_module._rate_check(key, "user_daily"))

    def test_prune_removes_everything(self):
        for i in range(3):
            routes_module._rate_check(f"prune-{secrets.token_hex(3)}", "user_burst")
        removed = routes_module._rate_prune()
        self.assertGreaterEqual(removed, 3)
        session = SessionLocal()
        try:
            self.assertEqual(session.query(RateLimitEvent).count(), 0)
        finally:
            session.close()

    def test_stale_rows_are_pruned_opportunistically(self):
        """Old rows must not accumulate forever."""
        session = SessionLocal()
        try:
            session.add(RateLimitEvent(
                bucket="ancient:x",
                created_at=datetime.utcnow() - timedelta(days=30)))
            session.commit()
        finally:
            session.close()
        # Any check prunes rows older than the longest window.
        routes_module._rate_check(f"trigger-{secrets.token_hex(4)}", "user_burst")
        session = SessionLocal()
        try:
            self.assertIsNone(
                session.query(RateLimitEvent).filter(
                    RateLimitEvent.bucket == "ancient:x").first(),
                "stale row was not pruned")
        finally:
            session.close()

    def test_retry_after_is_derived_from_stored_event(self):
        key = f"retry-{secrets.token_hex(4)}"
        self.assertEqual(routes_module._rate_retry_after(key, "user_burst"), 0)
        routes_module._rate_check(key, "user_burst")
        retry = routes_module._rate_retry_after(key, "user_burst")
        # Should be close to the 10-minute window, not zero or negative.
        self.assertGreater(retry, 500)
        self.assertLessEqual(retry, 600)

    def test_limiter_failure_fails_open_rather_than_blocking_login(self):
        """A broken counter must not lock everyone out of the app."""
        original = routes_module.SessionLocal
        routes_module.SessionLocal = lambda: (_ for _ in ()).throw(
            RuntimeError("simulated database outage"))
        try:
            allowed = routes_module._rate_check("boom", "user_burst")
        finally:
            routes_module.SessionLocal = original
        self.assertTrue(allowed, "a database error blocked the request")

    def test_no_in_memory_counting_remains_in_the_request_path(self):
        """Guard against silently reverting to a per-worker dict.

        _RATE_BUCKETS is kept only so old test helpers do not explode; if the
        limiter ever consults it again, limits become per-process and this
        fails.
        """
        import inspect
        source = inspect.getsource(routes_module._rate_check)
        body = source.split('"""', 2)[-1]  # strip the docstring
        self.assertNotIn("_RATE_BUCKETS", body,
                         "_rate_check consults in-memory state again")


if __name__ == "__main__":
    unittest.main(verbosity=2)
