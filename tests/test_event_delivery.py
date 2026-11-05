"""Event delivery contract shared by the two independently built agents."""

from contextlib import redirect_stderr, redirect_stdout
from datetime import datetime, timedelta, timezone
from email.utils import format_datetime
from http.client import IncompleteRead
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import io
import json
import os
from pathlib import Path
import runpy
import socket
import sys
import threading
import unittest
from unittest.mock import Mock, call, patch
from urllib.error import HTTPError, URLError


ROOT = Path(__file__).resolve().parents[1]
KIND = "rpm" if (ROOT / "scripts/send-rpm-event").exists() else "srpm"
COMMAND = ROOT / f"scripts/send-{KIND}-event"


class FailedRead(io.BytesIO):
    def __init__(self, error=None):
        super().__init__()
        self.error = error or TimeoutError("response read timed out")

    def read(self, *_args):
        raise self.error


class EventDeliveryTests(unittest.TestCase):
    def setUp(self):
        self.main = runpy.run_path(str(COMMAND))["main"]
        self.env = {
            "SCAN_RESULTS_PORTAL_URL": "http://127.0.0.1:8080/",
            "SCAN_SERVICE_INGEST_TOKEN": "test-only-secret",
            "BUILD_NUMBER": "7", "JOB_NAME": "folder/package",
            "NO_PROXY": "127.0.0.1",
        }

    def argv(self, event="started"):
        args = [str(COMMAND), event, f"--{KIND}-name", f"test-1-1.{KIND}.rpm"]
        if KIND == "rpm":
            args += ["--scan-key", "a" * 64, "--rpm-sha256", "a" * 64]
        if event != "started":
            args += ["--cbt-exit-code" if KIND == "rpm" else "--csmock-exit-code",
                     "2" if event == "failed" else "0"]
        return args

    def run_main(self, opener=None, event="started"):
        stdout, stderr = io.StringIO(), io.StringIO()
        with patch.dict(os.environ, self.env), patch.object(sys, "argv", self.argv(event)), \
                patch("time.sleep") as sleep, redirect_stdout(stdout), redirect_stderr(stderr):
            if opener is None:
                result = self.main()
            else:
                with patch.dict(self.main.__globals__, urlopen=opener):
                    result = self.main()
        return result, stdout.getvalue(), stderr.getvalue(), sleep

    def error(self, code, headers=None):
        body = io.BytesIO(b'{"detail":"test rejection"}')
        return HTTPError(self.env["SCAN_RESULTS_PORTAL_URL"], code, "test error", headers or {}, body)

    def test_success_preserves_endpoint_auth_and_payload(self):
        response = io.BytesIO(b'{"id":1}')
        opener = Mock(return_value=response)
        status, stdout, stderr, sleep = self.run_main(opener)
        self.assertEqual(status, 0)
        self.assertIn('"id":1', stdout)
        self.assertEqual(stderr, "")
        sleep.assert_not_called()
        request = opener.call_args.args[0]
        self.assertEqual(request.full_url, f"http://127.0.0.1:8080/api/v1/{KIND}/events")
        self.assertEqual(request.get_header("Authorization"), "Bearer test-only-secret")
        self.assertEqual(opener.call_args.kwargs["timeout"], 30)
        self.assertEqual(json.loads(request.data)["jenkins_job_full_name"], "folder/package")
        self.assertTrue(response.closed)

    def test_transient_http_failure_retries_the_identical_event(self):
        failure = self.error(503)
        success = io.BytesIO(b"ok")
        opener = Mock(side_effect=[failure, success])
        status, stdout, stderr, sleep = self.run_main(opener, event="completed")
        self.assertEqual(status, 0)
        self.assertEqual(opener.call_count, 2)
        first, second = [item.args[0] for item in opener.call_args_list]
        self.assertEqual(first.data, second.data)
        self.assertEqual(first.full_url, second.full_url)
        self.assertEqual(json.loads(second.data)["event"], "completed")
        self.assertIn("503", stderr)
        self.assertNotIn("test-only-secret", stderr)
        self.assertIn("ok", stdout)
        sleep.assert_called_once_with(1)
        self.assertTrue(failure.fp is None or failure.fp.closed)

    def test_retryable_http_statuses(self):
        for code in (408, 429, 500, 502, 503, 504):
            with self.subTest(code=code):
                opener = Mock(side_effect=[self.error(code), io.BytesIO(b"ok")])
                status, _, _, _ = self.run_main(opener)
                self.assertEqual(status, 0)
                self.assertEqual(opener.call_count, 2)

    def test_permanent_http_errors_are_not_retried(self):
        for code in (400, 401, 403, 404, 409, 422, 501):
            with self.subTest(code=code):
                failure = self.error(code)
                opener = Mock(side_effect=failure)
                status, _, stderr, sleep = self.run_main(opener)
                self.assertEqual(status, 22)
                opener.assert_called_once()
                sleep.assert_not_called()
                self.assertIn(str(code), stderr)
                self.assertIn("test rejection", stderr)
                self.assertNotIn("test-only-secret", stderr)
                self.assertTrue(failure.fp is None or failure.fp.closed)

    def test_network_errors_retry_with_a_finite_attempt_count(self):
        opener = Mock(side_effect=URLError("connection refused"))
        status, _, stderr, sleep = self.run_main(opener)
        self.assertEqual(status, 1)
        self.assertEqual(opener.call_count, 3)
        self.assertEqual(sleep.call_args_list, [call(1), call(2)])
        self.assertIn("connection refused", stderr)
        self.assertNotIn("test-only-secret", stderr)

    def test_http_retry_exhaustion_keeps_http_failure_exit_code(self):
        opener = Mock(side_effect=[self.error(503) for _ in range(3)])
        status, _, stderr, sleep = self.run_main(opener)
        self.assertEqual(status, 22)
        self.assertEqual(opener.call_count, 3)
        self.assertEqual(sleep.call_args_list, [call(1), call(2)])
        self.assertIn("503", stderr)

    def test_direct_socket_timeout_can_recover(self):
        opener = Mock(side_effect=[TimeoutError("socket timeout"), io.BytesIO(b"ok")])
        status, _, _, _ = self.run_main(opener)
        self.assertEqual(status, 0)
        self.assertEqual(opener.call_count, 2)

    def test_response_read_failure_retries_and_closes_the_first_response(self):
        failed = FailedRead()
        opener = Mock(side_effect=[failed, io.BytesIO(b"ok")])
        status, _, _, _ = self.run_main(opener)
        self.assertEqual(status, 0)
        self.assertTrue(failed.closed)
        self.assertEqual(opener.call_count, 2)

    def test_retry_after_seconds_is_respected(self):
        opener = Mock(side_effect=[self.error(429, {"Retry-After": "5"}), io.BytesIO(b"ok")])
        status, _, _, sleep = self.run_main(opener)
        self.assertEqual(status, 0)
        sleep.assert_called_once_with(5)

    def test_truncated_response_is_retried_and_closed(self):
        failed = FailedRead(IncompleteRead(b"partial", 20))
        opener = Mock(side_effect=[failed, io.BytesIO(b"ok")])
        status, _, _, _ = self.run_main(opener)
        self.assertEqual(status, 0)
        self.assertTrue(failed.closed)
        self.assertEqual(opener.call_count, 2)

    def test_unreadable_permanent_error_is_closed_and_not_retried(self):
        failed = FailedRead()
        error = HTTPError("http://127.0.0.1", 403, "Forbidden", {}, failed)
        opener = Mock(side_effect=error)
        status, _, stderr, sleep = self.run_main(opener)
        self.assertEqual(status, 22)
        opener.assert_called_once()
        sleep.assert_not_called()
        self.assertTrue(failed.closed)
        self.assertIn("HTTP 403", stderr)

    def test_failed_event_keeps_scanner_exit_code_and_time_during_retry(self):
        opener = Mock(side_effect=[URLError("connection refused"), io.BytesIO(b"ok")])
        status, _, _, _ = self.run_main(opener, event="failed")
        self.assertEqual(status, 0)
        first, second = [item.args[0].data for item in opener.call_args_list]
        self.assertEqual(first, second)
        payload = json.loads(second)
        self.assertEqual(payload["event"], "failed")
        self.assertEqual(payload["cbt_exit_code" if KIND == "rpm" else "csmock_exit_code"], 2)
        self.assertIn("finished_at", payload)
        self.assertNotIn("created_at", payload)

    def test_retry_after_http_date_is_respected(self):
        future = format_datetime(datetime.now(timezone.utc) + timedelta(seconds=10), usegmt=True)
        opener = Mock(side_effect=[self.error(503, {"Retry-After": future}), io.BytesIO(b"ok")])
        status, _, _, sleep = self.run_main(opener)
        self.assertEqual(status, 0)
        self.assertEqual(sleep.call_count, 1)
        self.assertGreater(sleep.call_args.args[0], 8)
        self.assertLessEqual(sleep.call_args.args[0], 10)

    def test_long_retry_after_does_not_block_or_retry_too_early(self):
        opener = Mock(side_effect=self.error(503, {"Retry-After": "3600"}))
        status, _, stderr, sleep = self.run_main(opener)
        self.assertEqual(status, 22)
        opener.assert_called_once()
        sleep.assert_not_called()
        self.assertIn("Retry-After", stderr)

    def test_invalid_retry_after_uses_bounded_backoff(self):
        for value in ("not a date", "-1"):
            with self.subTest(value=value):
                opener = Mock(side_effect=[self.error(503, {"Retry-After": value}), io.BytesIO(b"ok")])
                status, _, _, sleep = self.run_main(opener)
                self.assertEqual(status, 0)
                sleep.assert_called_once_with(1)

    def test_interruption_is_not_treated_as_network_failure(self):
        opener = Mock(side_effect=KeyboardInterrupt)
        with self.assertRaises(KeyboardInterrupt):
            self.run_main(opener)
        opener.assert_called_once()

    def test_connection_loss_after_acceptance_reuses_exact_payload(self):
        requests = []

        class Portal(BaseHTTPRequestHandler):
            def do_POST(self):
                requests.append((self.path, self.rfile.read(int(self.headers["Content-Length"]))))
                if len(requests) == 1:
                    self.connection.shutdown(socket.SHUT_RDWR)
                    self.connection.close()
                    return
                body = b'{"id":1}'
                self.send_response(200)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *_args):
                pass

        with ThreadingHTTPServer(("127.0.0.1", 0), Portal) as server:
            worker = threading.Thread(target=server.serve_forever, daemon=True)
            worker.start()
            self.env["SCAN_RESULTS_PORTAL_URL"] = f"http://127.0.0.1:{server.server_port}"
            try:
                status, _, _, _ = self.run_main(event="completed")
            finally:
                server.shutdown()
                worker.join(timeout=3)
        self.assertEqual(status, 0)
        self.assertEqual(len(requests), 2)
        self.assertEqual(requests[0], requests[1])


if __name__ == "__main__":
    unittest.main()
