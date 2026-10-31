"""Cold-start contention has its own finite budget; it never proves readiness."""
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from agent_learning.runtime import Runtime
from agent_learning.runtime_storage import initialization_guard


class StartupGuard1451Tests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.runtime = SimpleNamespace(home=self.root)

    def clock_and_lock(self, release_at):
        clock = {"now": 0.0, "attempts": 0, "releases": 0}

        @contextmanager
        def locked(path):
            self.assertEqual(path, self.root / ".runtime-initialization.lock")
            clock["attempts"] += 1
            acquired = release_at is not None and clock["now"] >= release_at
            try:
                yield acquired
            finally:
                if acquired:
                    clock["releases"] += 1

        def sleep(seconds):
            self.assertGreater(seconds, 0)
            self.assertLessEqual(seconds, .02)
            clock["now"] += seconds

        return clock, locked, sleep

    def test_default_wait_survives_a_slow_but_finite_cold_initializer(self):
        # The previous 1.5s lock allowance failed before a valid initializer
        # could finish on a busy runner. No actual sleep or machine speed is
        # involved in this regression.
        clock, locked, sleep = self.clock_and_lock(2.5)
        with patch("agent_learning.software_updates._locked", locked), \
                patch("agent_learning.runtime_storage.time.monotonic", side_effect=lambda: clock["now"]), \
                patch("agent_learning.runtime_storage.time.sleep", side_effect=sleep):
            with initialization_guard(self.runtime):
                self.assertGreaterEqual(clock["now"], 2.5)
        self.assertEqual(clock["releases"], 1)

    def test_default_wait_refuses_a_lock_that_never_releases(self):
        clock, locked, sleep = self.clock_and_lock(None)
        with patch("agent_learning.software_updates._locked", locked), \
                patch("agent_learning.runtime_storage.time.monotonic", side_effect=lambda: clock["now"]), \
                patch("agent_learning.runtime_storage.time.sleep", side_effect=sleep):
            with self.assertRaisesRegex(sqlite3.OperationalError, "initialization busy"):
                with initialization_guard(self.runtime):
                    self.fail("a busy lock cannot authorize initialization")
        # Stay below the normal Hook's 10s outer limit. A finite wait is not
        # permission to continue without the lock or to declare storage ready.
        self.assertGreater(clock["now"], 2.5)
        self.assertLess(clock["now"], 10)
        self.assertEqual(clock["releases"], 0)

    def test_runtime_still_initializes_storage_after_the_contended_wait(self):
        clock, locked, sleep = self.clock_and_lock(2.5)
        initialize = Runtime._initialize_storage
        initializers = []

        def tracked_initialize(runtime):
            initializers.append(runtime.home)
            return initialize(runtime)

        with patch("agent_learning.software_updates._locked", locked), \
                patch("agent_learning.runtime_storage.time.monotonic", side_effect=lambda: clock["now"]), \
                patch("agent_learning.runtime_storage.time.sleep", side_effect=sleep), \
                patch.object(Runtime, "_initialize_storage", tracked_initialize):
            runtime = Runtime(self.root)
        try:
            self.assertEqual(initializers, [self.root])
            self.assertEqual(runtime.db.execute("PRAGMA user_version").fetchone()[0], 25)
            self.assertEqual(runtime.db.execute("SELECT count(*) FROM runtime_storage_receipt").fetchone()[0], 1)
            self.assertEqual(clock["releases"], 1)
        finally:
            runtime.close()

    def test_explicit_short_wait_still_has_its_own_deadline(self):
        clock, locked, sleep = self.clock_and_lock(1)
        with patch("agent_learning.software_updates._locked", locked), \
                patch("agent_learning.runtime_storage.time.monotonic", side_effect=lambda: clock["now"]), \
                patch("agent_learning.runtime_storage.time.sleep", side_effect=sleep):
            with self.assertRaisesRegex(sqlite3.OperationalError, "initialization busy"):
                with initialization_guard(self.runtime, timeout=.025):
                    self.fail("an explicit smaller budget was ignored")
        self.assertAlmostEqual(clock["now"], .025)

    def test_acquired_lock_releases_after_initializer_failure(self):
        clock, locked, sleep = self.clock_and_lock(0)
        with patch("agent_learning.software_updates._locked", locked), \
                patch("agent_learning.runtime_storage.time.monotonic", side_effect=lambda: clock["now"]), \
                patch("agent_learning.runtime_storage.time.sleep", side_effect=sleep):
            with self.assertRaisesRegex(RuntimeError, "initializer failed"):
                with initialization_guard(self.runtime):
                    raise RuntimeError("initializer failed")
        self.assertEqual(clock["releases"], 1)
        self.assertEqual(clock["now"], 0)

    def test_ready_warm_open_never_waits_for_initialization_lock(self):
        home = self.root / "warm"
        first = Runtime(home)
        first.close()
        with patch("agent_learning.runtime_storage.initialization_guard",
                   side_effect=AssertionError("warm open acquired initialization lock")):
            for host in ("codex", "antigravity", "dsh"):
                runtime = Runtime(home, host=host)
                runtime.close()


if __name__ == "__main__":
    unittest.main()
