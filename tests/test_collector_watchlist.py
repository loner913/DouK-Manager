from __future__ import annotations

import http.client
import json
import os
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import uuid
from pathlib import Path
from unittest.mock import patch
from urllib.request import urlopen

from openpyxl import Workbook

from douk_manager.core.watchlist import WatchlistService
from douk_manager.core.watchlist_session import ManagerInstanceLease
from douk_manager.vendor import collector_server
from tests.helpers import make_test_paths


class CollectorWatchlistHttpTests(unittest.TestCase):
    @staticmethod
    def _payload() -> dict[str, object]:
        return {
            "request_id": str(uuid.uuid4()),
            "url": "https://www.douyin.com/user/42",
            "captured_nickname": "合成昵称",
            "display_name": "合成名称",
            "douyin_id": "synthetic_42",
            "nickname_blank": False,
            "reasons": ["few_works"],
            "note": "synthetic",
            "review_after_days": 7,
            "client_version": collector_server.VERSION,
            "page_visible": True,
        }

    def test_observe_requires_session_but_formal_server_shell_stays_available(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            paths = make_test_paths(Path(directory), account_count=2)
            WatchlistService(paths).initialize(initialization_evidence=True)
            WatchlistService(paths).mark_ready()
            old_values = {
                "SETTINGS_PATH": collector_server.SETTINGS_PATH,
                "WATCHLIST_PATH": collector_server.WATCHLIST_PATH,
                "WATCHLIST_WATERMARK_PATH": collector_server.WATCHLIST_WATERMARK_PATH,
                "WATCHLIST_CONTROL_PATH": collector_server.WATCHLIST_CONTROL_PATH,
                "GLOBAL_LOCK_PATH": collector_server.GLOBAL_LOCK_PATH,
                "MANAGER_INSTANCE_LOCK_PATH": collector_server.MANAGER_INSTANCE_LOCK_PATH,
            }
            collector_server.SETTINGS_PATH = paths.master_settings
            collector_server.WATCHLIST_PATH = paths.watchlist
            collector_server.WATCHLIST_WATERMARK_PATH = paths.watchlist_w_watermark
            collector_server.WATCHLIST_CONTROL_PATH = paths.watchlist_control
            collector_server.GLOBAL_LOCK_PATH = paths.lock_file
            collector_server.MANAGER_INSTANCE_LOCK_PATH = paths.instance_lock_file
            server = collector_server.ThreadingHTTPServer(("127.0.0.1", 0), collector_server.Handler)
            worker = threading.Thread(target=server.serve_forever, daemon=True)
            worker.start()
            try:
                body = json.dumps(self._payload(), ensure_ascii=False).encode("utf-8")
                connection = http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=3)
                connection.request(
                    "POST",
                    "/watchlist/observe",
                    body=body,
                    headers={
                        "Content-Type": "application/json",
                        "Content-Length": str(len(body)),
                        "X-DouK-Token": collector_server.ACCESS_TOKEN,
                    },
                )
                response = connection.getresponse()
                blocked = json.loads(response.read().decode("utf-8"))
                connection.close()
                self.assertEqual(response.status, 503)
                self.assertEqual(blocked["code"], "MANAGER_SESSION_REQUIRED")
                self.assertEqual(read_records(paths.watchlist), [])

                lease = ManagerInstanceLease(paths.instance_lock_file)
                lease.acquire()
                try:
                    with patch.object(collector_server, "manager_session_active", return_value=True), patch.object(
                        collector_server, "manager_instance_owner_present", return_value=True
                    ):
                        body = json.dumps(self._payload(), ensure_ascii=False).encode("utf-8")
                        connection = http.client.HTTPConnection(
                            "127.0.0.1", server.server_port, timeout=3
                        )
                        connection.request(
                            "POST",
                            "/watchlist/observe",
                            body=body,
                            headers={
                                "Content-Type": "application/json",
                                "Content-Length": str(len(body)),
                                "X-DouK-Token": collector_server.ACCESS_TOKEN,
                            },
                        )
                        response = connection.getresponse()
                        created = json.loads(response.read().decode("utf-8"))
                        connection.close()
                    self.assertEqual(response.status, 201)
                    self.assertEqual(created["status"], "CREATED")
                    self.assertEqual(created["w_id"], 1)
                    self.assertNotIn("url", created)
                finally:
                    lease.release()
            finally:
                server.shutdown()
                server.server_close()
                worker.join(timeout=2)
                for name, value in old_values.items():
                    setattr(collector_server, name, value)

    def test_real_worker_pipe_eof_revokes_observation_but_keeps_health_process_alive(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            paths = make_test_paths(root, account_count=2)
            WatchlistService(paths).initialize(initialization_evidence=True)
            WatchlistService(paths).mark_ready()
            create_synthetic_collector_workbook(paths.collector_excel)
            port = free_port()
            environment = dict(os.environ)
            environment.update(
                {
                    "DOUK_COLLECTOR_DATA_DIR": str(paths.collector_data),
                    "DOUK_MASTER_PATH": str(paths.master_settings),
                    "DOUK_COLLECTOR_EXCEL": str(paths.collector_excel),
                    "DOUK_SCREENSHOT_DIR": str(paths.screenshot_inbox),
                    "DOUK_COLLECTOR_PORT": str(port),
                    "DOUK_COLLECTOR_TOKEN": "synthetic-token",
                    "DOUK_GLOBAL_LOCK_PATH": str(paths.lock_file),
                    "DOUK_MANAGER_SESSION_CHANNEL": "1",
                    "DOUK_WATCHLIST_PATH": str(paths.watchlist),
                    "DOUK_WATCHLIST_WATERMARK_PATH": str(paths.watchlist_w_watermark),
                    "DOUK_WATCHLIST_CONTROL_PATH": str(paths.watchlist_control),
                    "DOUK_MANAGER_INSTANCE_LOCK_PATH": str(paths.instance_lock_file),
                    "PYTHONUNBUFFERED": "1",
                    "PYTHONIOENCODING": "utf-8",
                    "PYTHONUTF8": "1",
                }
            )
            lease = ManagerInstanceLease(paths.instance_lock_file)
            lease.acquire()
            process = None
            try:
                process = subprocess.Popen(
                    [sys.executable, str(Path(__file__).resolve().parents[1] / "main.py"), "--collector-worker"],
                    cwd=str(Path(__file__).resolve().parents[1]),
                    env=environment,
                    stdin=subprocess.PIPE,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                )
                assert process.stdin is not None
                process.stdin.write(b"synthetic-manager-session\n")
                process.stdin.flush()
                wait_for_health(port, "synthetic-token")
                created = post_observe(port, "synthetic-token", self._payload())
                self.assertEqual(created["status"], "CREATED")
                self.assertEqual(created["w_id"], 1)

                # This explicit close simulates the manager process exiting;
                # production CollectorService never closes its write end
                # during normal operation.
                process.stdin.close()
                deadline = time.monotonic() + 3
                while time.monotonic() < deadline:
                    try:
                        blocked = post_observe(port, "synthetic-token", self._payload())
                    except (ConnectionError, OSError):
                        time.sleep(0.05)
                        continue
                    if blocked.get("code") == "MANAGER_SESSION_REQUIRED":
                        break
                    time.sleep(0.05)
                else:
                    self.fail("worker did not revoke observation after stdin EOF")
                self.assertEqual(blocked["code"], "MANAGER_SESSION_REQUIRED")
                self.assertIsNone(process.poll())
            finally:
                try:
                    if process is not None and process.stdin is not None:
                        try:
                            process.stdin.close()
                        except (OSError, ValueError):
                            pass
                    if process is not None and process.poll() is None:
                        process.terminate()
                    if process is not None:
                        try:
                            process.communicate(timeout=5)
                        except subprocess.TimeoutExpired:
                            process.kill()
                            process.communicate(timeout=5)
                finally:
                    lease.release()


def create_synthetic_collector_workbook(path: Path) -> None:
    workbook = Workbook()
    sheet = workbook.active
    sheet.title = collector_server.EXCEL_SHEET_NAME
    sheet["A2"] = "1.A1"
    sheet["B2"] = "account1"
    sheet["D2"] = "https://www.douyin.com/user/1"
    sheet["A3"] = "2.A2"
    sheet["B3"] = "account2"
    sheet["D3"] = "https://www.douyin.com/user/2"
    sheet["A4"] = "3.A3"
    sheet.merge_cells("B4:C4")
    sheet.merge_cells("D4:J4")
    workbook.save(path)
    workbook.close()


def free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def wait_for_health(port: int, token: str) -> None:
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        try:
            with urlopen(
                f"http://127.0.0.1:{port}/health",
                timeout=0.25,
            ) as response:
                if response.status == 200:
                    return
        except OSError:
            time.sleep(0.05)
    raise AssertionError("synthetic collector worker did not become healthy")


def post_observe(port: int, token: str, payload: dict[str, object]) -> dict[str, object]:
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=2)
    try:
        connection.request(
            "POST",
            "/watchlist/observe",
            body=body,
            headers={
                "Content-Type": "application/json",
                "Content-Length": str(len(body)),
                "X-DouK-Token": token,
            },
        )
        response = connection.getresponse()
        return json.loads(response.read().decode("utf-8"))
    finally:
        connection.close()


def read_records(path: Path) -> list[dict[str, object]]:
    return json.loads(path.read_text(encoding="utf-8"))["records"]


if __name__ == "__main__":
    unittest.main()
