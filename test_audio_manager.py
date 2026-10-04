import http.client
import json
import os
import tempfile
import threading
import unittest
from pathlib import Path, PurePosixPath
from unittest.mock import patch

import audio_manager


class AudioManagerTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.directory = Path(self.tempdir.name)
        self.server = audio_manager.ThreadingHTTPServer(("127.0.0.1", 0), audio_manager.AudioManagerHandler)
        self.server.audio_directory = self.directory
        self.thread = threading.Thread(target=self.server.serve_forever)
        self.thread.start()

    def tearDown(self):
        self.server.shutdown()
        self.thread.join()
        self.server.server_close()
        self.tempdir.cleanup()

    def request(self, method, path, body=None, headers=None):
        connection = http.client.HTTPConnection("127.0.0.1", self.server.server_port)
        connection.request(method, path, body, headers or {})
        response = connection.getresponse()
        payload = response.read().decode()
        connection.close()
        return response.status, payload

    def upload(self, name, content=b"audio"):
        boundary = "test-boundary"
        body = (f"--{boundary}\r\nContent-Disposition: form-data; name=\"audio\"; filename=\"{name}\"\r\n"
                "Content-Type: application/octet-stream\r\n\r\n").encode() + content + f"\r\n--{boundary}--\r\n".encode()
        return self.request("POST", "/api/files", body, {"Content-Type": f"multipart/form-data; boundary={boundary}", "Content-Length": str(len(body))})

    def test_only_allowed_files_can_be_uploaded_listed_and_deleted(self):
        self.assertEqual(self.upload("notice.mp3")[0], 201)
        self.assertEqual(self.upload("notes.txt")[0], 400)
        status, payload = self.request("GET", "/api/files")
        self.assertEqual((status, payload), (200, '["notice.mp3"]'))
        self.assertEqual(self.request("DELETE", "/api/files/notice.mp3")[0], 204)
        self.assertFalse((self.directory / "notice.mp3").exists())

    def test_path_traversal_and_existing_files_are_rejected(self):
        self.assertEqual(self.upload("../outside.wav")[0], 400)
        self.assertEqual(self.request("DELETE", "/api/files/%2E%2E%2Foutside.wav")[0], 400)
        (self.directory / "exists.wav").write_bytes(b"original")
        self.assertEqual(self.upload("exists.wav")[0], 409)
        self.assertEqual((self.directory / "exists.wav").read_bytes(), b"original")

    def test_symlinks_are_neither_listed_nor_deleted(self):
        target = self.directory / "target.wav"
        target.write_bytes(b"audio")
        link = self.directory / "linked.mp3"
        try:
            link.symlink_to(target)
        except OSError as error:
            self.skipTest(f"Symlinks are unavailable: {error}")
        self.assertNotIn("linked.mp3", self.request("GET", "/api/files")[1])
        self.assertEqual(self.request("DELETE", "/api/files/linked.mp3")[0], 404)
        self.assertTrue(link.is_symlink())

    def test_activity_endpoint_returns_newest_valid_events_first(self):
        (self.directory / audio_manager.ACTIVITY_LOG_NAME).write_text(
            '{"time":"2026-01-01T00:00:00+00:00","event":"triggered","clip":"first.wav"}\n'
            'not-json\n'
            '{"time":"2026-01-01T00:01:00+00:00","event":"finished","clip":"second.mp3"}\n',
            encoding="utf-8",
        )
        status, payload = self.request("GET", "/api/activity")
        self.assertEqual(status, 200)
        self.assertEqual([event["clip"] for event in json.loads(payload)], ["second.mp3", "first.wav"])

    def test_unit_keeps_the_requested_current_directory_and_bind(self):
        directory = PurePosixPath("/srv/audio")
        unit = audio_manager.service_unit(PurePosixPath("/srv/app/audio_manager.py"), directory, "player", "/usr/bin/python3", "127.0.0.1", 8000)
        self.assertIn(f"WorkingDirectory={audio_manager.systemd_path(directory)}", unit)
        self.assertIn("User=player", unit)
        self.assertIn("--host \"127.0.0.1\" --port 8000", unit)

    def test_default_bind_address_is_all_interfaces(self):
        with patch("sys.argv", ["audio_manager.py"]):
            self.assertEqual(audio_manager.parse_args().host, "0.0.0.0")


if __name__ == "__main__":
    unittest.main()
