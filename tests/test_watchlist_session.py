from __future__ import annotations

import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from douk_manager.core.locks import LockBusyError
from douk_manager.core.watchlist_session import ManagerInstanceLease
from douk_manager.controller import ManagerController
from douk_manager.vendor import collector_server


class _SessionStream:
    def __init__(self) -> None:
        self.released = threading.Event()
        self._first = True

    def readline(self) -> bytes:
        if self._first:
            self._first = False
            return b"synthetic-nonce\n"
        self.released.wait(timeout=2)
        return b""


class WatchlistSessionTests(unittest.TestCase):
    def test_manager_controller_owns_session_and_second_instance_is_read_only(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with patch("douk_manager.controller.application_root", return_value=root):
                with patch(
                    "douk_manager.controller.setup_logging",
                    return_value=(Mock(), root / "manager.log"),
                ):
                    first = ManagerController()
                try:
                    self.assertTrue(first.instance_lease.active)
                    with patch(
                        "douk_manager.controller.setup_logging",
                        return_value=(Mock(), root / "manager.log"),
                    ):
                        second = ManagerController()
                    try:
                        self.assertFalse(second.instance_lease.active)
                        self.assertEqual(second.startup_state.value, "DEGRADED_READ_ONLY")
                        self.assertFalse(second.begin_startup_check(1))
                        self.assertIn("另一个管理器会话", second.read_only_reason)
                    finally:
                        second.begin_closing()
                finally:
                    first.begin_closing()

    def test_dedicated_instance_lease_is_exclusive_and_releasable(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "Data" / ".douk_manager.instance.lock"
            first = ManagerInstanceLease(path)
            second = ManagerInstanceLease(path)
            first.acquire()
            with self.assertRaises(LockBusyError):
                second.acquire()
            self.assertTrue(first.active)
            self.assertTrue(first.manager_is_still_owner())
            first.release()
            self.assertFalse(first.manager_is_still_owner())
            second.acquire()
            self.assertTrue(second.active)
            second.release()

    def test_stdin_eof_revokes_session_without_logging_nonce(self) -> None:
        stream = _SessionStream()
        with patch.dict(collector_server.os.environ, {"DOUK_MANAGER_SESSION_CHANNEL": "1"}), patch.object(
            collector_server, "MANAGER_SESSION_ACTIVE", False
        ):
            self.assertTrue(collector_server.configure_manager_session_channel(stream))
            self.assertTrue(collector_server.manager_session_active())
            stream.released.set()
            deadline = time.monotonic() + 2
            while collector_server.manager_session_active() and time.monotonic() < deadline:
                time.sleep(0.01)
            self.assertFalse(collector_server.manager_session_active())


if __name__ == "__main__":
    unittest.main()
