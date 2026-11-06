"""Pending notifications survive failed delivery and agent process restarts."""

from contextlib import redirect_stderr, redirect_stdout
from concurrent.futures import ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import io
import json
import os
from pathlib import Path
import runpy
import sqlite3
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest.mock import Mock, patch
from urllib.error import HTTPError, URLError


ROOT = Path(__file__).resolve().parents[1]
KIND = "rpm" if (ROOT / "scripts/send-rpm-event").exists() else "srpm"
COMMAND = ROOT / f"scripts/send-{KIND}-event"


class EventOutboxTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.workdir = Path(directory.name)
        self.env = {
            "JENKINS_AGENT_WORKDIR": directory.name,
            "SCAN_RESULTS_PORTAL_URL": "http://portal:8080",
            "SCAN_SERVICE_INGEST_TOKEN": "do-not-persist-this-token",
            "JOB_NAME": "packages/curl", "BUILD_NUMBER": "7",
        }

    def invoke(self, opener, event="completed", timestamp="2026-11-06T10:00:00+08:00"):
        # Reload the executable for every call; recovery cannot depend on globals.
        main = runpy.run_path(str(COMMAND))["main"]
        argv = [str(COMMAND), event]
        if event != "replay":
            argv += [f"--{KIND}-name", f"package-1-1.{KIND}.rpm", "--event-time", timestamp]
            if KIND == "rpm":
                argv += ["--scan-key", "a" * 64, "--rpm-sha256", "a" * 64]
        output, errors = io.StringIO(), io.StringIO()
        with patch.dict(os.environ, self.env, clear=True), patch.object(sys, "argv", argv), \
                patch.dict(main.__globals__, urlopen=opener), patch("time.sleep"), \
                redirect_stdout(output), redirect_stderr(errors):
            return main(), output.getvalue(), errors.getvalue()

    def pending(self):
        path = self.workdir / ".scan-event-outbox" / f"{KIND}.sqlite3"
        self.assertTrue(path.is_file(), "event must be durable before delivery")
        with sqlite3.connect(path) as connection:
            return connection.execute("SELECT id, endpoint, body FROM pending_events ORDER BY id").fetchall()

    def fail(self, event="completed", timestamp="2026-11-06T10:00:00+08:00"):
        opener = Mock(side_effect=URLError("offline"))
        status, _, _ = self.invoke(opener, event, timestamp)
        self.assertEqual(status, 1)
        return opener

    def test_failed_delivery_can_replay_with_original_identity_and_rotated_token(self):
        initial = self.fail()
        original = initial.call_args.args[0].data
        self.assertEqual(self.pending()[0][2], original)
        self.env.pop("JOB_NAME")
        self.env.pop("BUILD_NUMBER")
        self.env["SCAN_SERVICE_INGEST_TOKEN"] = "rotated-token"
        opener = Mock(return_value=io.BytesIO(b'{"id":7}'))
        status, _, _ = self.invoke(opener, "replay")
        self.assertEqual(status, 0)
        request = opener.call_args.args[0]
        self.assertEqual(request.data, original)
        self.assertEqual(request.get_header("Authorization"), "Bearer rotated-token")
        self.assertEqual(self.pending(), [])

    def test_success_removes_only_the_confirmed_event(self):
        self.fail("started")
        status, _, _ = self.invoke(Mock(return_value=io.BytesIO(b'{"id":7}')))
        self.assertEqual(status, 0)
        rows = self.pending()
        self.assertEqual(len(rows), 1)
        self.assertEqual(json.loads(rows[0][2])["event"], "started")

    def test_interruption_keeps_the_previously_committed_event(self):
        with self.assertRaises(KeyboardInterrupt):
            self.invoke(Mock(side_effect=KeyboardInterrupt))
        self.assertEqual(len(self.pending()), 1)

    def test_credentials_are_not_written_and_storage_is_private(self):
        self.fail()
        directory = self.workdir / ".scan-event-outbox"
        path = directory / f"{KIND}.sqlite3"
        self.assertEqual(directory.stat().st_mode & 0o777, 0o700)
        self.assertEqual(path.stat().st_mode & 0o777, 0o600)
        self.assertNotIn(self.env["SCAN_SERVICE_INGEST_TOKEN"].encode(), path.read_bytes())

    def test_storage_failure_does_not_send_an_unrecoverable_event(self):
        obstruction = self.workdir / ".scan-event-outbox"
        obstruction.write_text("not a directory")
        opener = Mock(return_value=io.BytesIO(b'{"id":7}'))
        status, _, errors = self.invoke(opener)
        self.assertEqual(status, 1)
        opener.assert_not_called()
        self.assertIn("outbox", errors.lower())

    def test_portal_change_does_not_send_old_events_to_the_new_destination(self):
        self.fail()
        self.env["SCAN_RESULTS_PORTAL_URL"] = "http://another-portal:8080"
        opener = Mock(return_value=io.BytesIO(b'{"id":7}'))
        status, _, _ = self.invoke(opener, "replay")
        self.assertEqual(status, 0)
        opener.assert_not_called()
        self.assertEqual(len(self.pending()), 1)

    def test_permanently_rejected_event_is_retained_without_blocking_other_events(self):
        self.fail("started")
        self.fail("completed")
        rejection = HTTPError("http://portal", 422, "invalid", {}, io.BytesIO(b"invalid"))
        opener = Mock(side_effect=[rejection, io.BytesIO(b'{"id":7}')])
        status, _, _ = self.invoke(opener, "replay")
        self.assertEqual(status, 22)
        self.assertEqual(opener.call_count, 2)
        self.assertEqual(len(self.pending()), 1)
        self.assertEqual(json.loads(self.pending()[0][2])["event"], "started")

    def outbox_type(self):
        return runpy.run_path(str(COMMAND))["EventOutbox"]

    def test_concurrent_writers_do_not_lose_or_duplicate_identical_events(self):
        outbox_type = self.outbox_type()
        endpoint = f"http://portal:8080/api/v1/{KIND}/events"

        def store(number):
            with outbox_type(self.workdir, KIND) as outbox:
                return outbox.store(endpoint, str(number % 10).encode())

        with ThreadPoolExecutor(max_workers=8) as workers:
            ids = list(workers.map(store, range(40)))
        self.assertEqual(len(set(ids)), 10)
        self.assertEqual(len(self.pending()), 10)
        # Deleted IDs cannot be reused: a late duplicate ack must remain harmless.
        with outbox_type(self.workdir, KIND) as outbox:
            outbox.acknowledge(ids[0])
            newer = outbox.store(endpoint, b"new event")
            self.assertGreater(newer, max(ids))
            outbox.acknowledge(ids[0])
        self.assertIn(newer, [row[0] for row in self.pending()])

    def test_replay_batch_is_bounded_and_rotates_away_from_rejected_events(self):
        endpoint = f"http://portal:8080/api/v1/{KIND}/events"
        with self.outbox_type()(self.workdir, KIND) as outbox:
            for number in range(21):
                outbox.store(endpoint, json.dumps({"test": number}).encode())

        def reject(*args, **kwargs):
            raise HTTPError(endpoint, 422, "invalid", {}, io.BytesIO(b"invalid"))

        opener = Mock(side_effect=reject)
        status, _, _ = self.invoke(opener, "replay")
        self.assertEqual(status, 22)
        self.assertEqual(opener.call_count, 20)
        opener.reset_mock()
        status, _, _ = self.invoke(opener, "replay")
        self.assertEqual(status, 22)
        self.assertEqual(opener.call_count, 20)
        self.assertEqual(json.loads(opener.call_args_list[0].args[0].data)["test"], 20)
        self.assertEqual(len(self.pending()), 21)

    def test_lost_local_ack_keeps_event_for_idempotent_redelivery(self):
        outbox_type = self.outbox_type()
        with patch.object(outbox_type, "acknowledge", side_effect=sqlite3.OperationalError("disk error")):
            status, _, _ = self.invoke(Mock(return_value=io.BytesIO(b'{"id":7}')))
        self.assertEqual(status, 1)
        original = self.pending()[0][2]
        opener = Mock(return_value=io.BytesIO(b'{"id":7}'))
        status, _, _ = self.invoke(opener, "replay")
        self.assertEqual(status, 0)
        self.assertEqual(opener.call_args.args[0].data, original)
        self.assertEqual(self.pending(), [])

    def test_new_registrations_do_not_jump_ahead_of_an_older_retry(self):
        endpoint = f"http://portal:8080/api/v1/{KIND}/events"
        with self.outbox_type()(self.workdir, KIND) as outbox:
            older = outbox.store(endpoint, b"older event")
            outbox.attempted(older)
            outbox.store(endpoint, b"new event")
            self.assertEqual(outbox.pending(endpoint)[0][0], older)

    def test_embedded_url_credentials_are_never_persisted_or_sent(self):
        self.env["SCAN_RESULTS_PORTAL_URL"] = "http://secret:password@portal:8080"
        opener = Mock(return_value=io.BytesIO(b'{"id":7}'))
        status, _, errors = self.invoke(opener)
        self.assertEqual(status, 1)
        opener.assert_not_called()
        self.assertNotIn("password", errors)
        self.assertFalse((self.workdir / ".scan-event-outbox").exists())

    def test_process_killed_during_http_request_can_replay_from_disk(self):
        received, release = threading.Event(), threading.Event()
        bodies = []

        class Portal(BaseHTTPRequestHandler):
            def do_POST(self):
                bodies.append(self.rfile.read(int(self.headers["Content-Length"])))
                if len(bodies) == 1:
                    received.set()
                    release.wait(5)
                body = b'{"id":7}'
                self.send_response(200)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                try:
                    self.wfile.write(body)
                except (BrokenPipeError, ConnectionResetError):
                    pass

            def log_message(self, *_args):
                pass

        with ThreadingHTTPServer(("127.0.0.1", 0), Portal) as server:
            worker = threading.Thread(target=server.serve_forever, daemon=True)
            worker.start()
            env = {**os.environ, **self.env, "NO_PROXY": "127.0.0.1",
                   "SCAN_RESULTS_PORTAL_URL": f"http://127.0.0.1:{server.server_port}"}
            command = [sys.executable, str(COMMAND), "completed",
                       f"--{KIND}-name", f"package-1-1.{KIND}.rpm"]
            if KIND == "rpm":
                command += ["--scan-key", "a" * 64, "--rpm-sha256", "a" * 64]
            process = subprocess.Popen(command, env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            try:
                self.assertTrue(received.wait(5))
                self.assertEqual(len(self.pending()), 1)
                process.kill()
                process.communicate(timeout=3)
                release.set()
                # A fresh interpreter, no current build variables and no mocked HTTP.
                env.pop("JOB_NAME", None)
                env.pop("BUILD_NUMBER", None)
                result = subprocess.run([sys.executable, str(COMMAND), "replay"], env=env,
                                        capture_output=True, text=True, timeout=5)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertIn("0 pending", result.stdout)
                self.assertEqual(self.pending(), [])
                self.assertEqual(len(bodies), 2)
                self.assertEqual(bodies[0], bodies[1])
            finally:
                if process.poll() is None:
                    process.kill()
                process.communicate(timeout=3)
                release.set()
                server.shutdown()
                worker.join(timeout=3)

    def test_http_success_without_a_valid_task_acknowledgement_keeps_the_event(self):
        for body in (b"", b"<html>proxy error</html>", b"null", b"[]", b"{}",
                     b'{"id":true}', b'{"id":"7"}', b'{"id":0}', b'{"id":-1}'):
            with self.subTest(body=body):
                opener = Mock(return_value=io.BytesIO(body))
                status, _, errors = self.invoke(opener)
                self.assertEqual(status, 1)
                self.assertIn("acknowledgement", errors)
                self.assertEqual(len(self.pending()), 1)
                opener.assert_called_once()
        status, _, _ = self.invoke(Mock(return_value=io.BytesIO(b'{"id":7}')), "replay")
        self.assertEqual(status, 0)
        self.assertEqual(self.pending(), [])


if __name__ == "__main__":
    unittest.main()
