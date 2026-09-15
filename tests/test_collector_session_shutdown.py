from __future__ import annotations

import http.client
import threading
import unittest
from http.server import BaseHTTPRequestHandler
from unittest.mock import patch

from douk_manager.vendor import collector_server as collector


class CollectorSessionShutdownTests(unittest.TestCase):
    def test_managed_eof_stops_accepting_but_drains_accepted_request(self):
        started = threading.Event()
        finish = threading.Event()
        stopped = threading.Event()
        closed = threading.Event()
        active = threading.Event()
        active.set()
        responses = []

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                started.set()
                finish.wait(5)
                self.send_response(200)
                self.end_headers()
                self.wfile.write(b"committed")

            def log_message(self, *_args):
                pass

        server = collector.CollectorHTTPServer(("127.0.0.1", 0), Handler)
        server.manager_bound = True

        def serve():
            try:
                server.serve_forever(poll_interval=0.01)
            except collector._ManagerSessionEnded:
                stopped.set()
            finally:
                server.server_close()
                closed.set()

        def request():
            connection = http.client.HTTPConnection(*server.server_address, timeout=5)
            try:
                connection.request("GET", "/")
                response = connection.getresponse()
                responses.append((response.status, response.read()))
            finally:
                connection.close()

        with patch.object(collector, "manager_session_active", active.is_set):
            worker = threading.Thread(target=serve)
            client = threading.Thread(target=request)
            worker.start()
            client.start()
            try:
                self.assertTrue(started.wait(2))
                active.clear()
                self.assertTrue(stopped.wait(2))
                self.assertFalse(closed.is_set())
                finish.set()
                self.assertTrue(closed.wait(2))
            finally:
                active.clear()
                finish.set()
                client.join(5)
                worker.join(5)
            self.assertFalse(worker.is_alive())
            self.assertEqual(responses, [(200, b"committed")])

    def test_standalone_serves_without_manager_session(self):
        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                self.send_response(200)
                self.end_headers()

            def log_message(self, *_args):
                pass

        with patch.object(collector, "manager_session_active", return_value=False):
            server = collector.CollectorHTTPServer(("127.0.0.1", 0), Handler)
            worker = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.01})
            worker.start()
            connection = http.client.HTTPConnection(*server.server_address, timeout=2)
            try:
                for _ in range(2):
                    connection.request("GET", "/")
                    response = connection.getresponse()
                    self.assertEqual(response.status, 200)
                    response.read()
                self.assertTrue(worker.is_alive())
            finally:
                connection.close()
                server.shutdown()
                server.server_close()
                worker.join(2)


if __name__ == "__main__":
    unittest.main()
