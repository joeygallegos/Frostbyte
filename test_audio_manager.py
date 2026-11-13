import http.client
import json
import os
import tempfile
import threading
import unittest
from pathlib import Path, PurePosixPath
from types import SimpleNamespace
from unittest.mock import Mock, patch

import audio_manager


class AudioManagerTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.directory = Path(self.tempdir.name)
        self.server = audio_manager.ThreadingHTTPServer(("127.0.0.1", 0), audio_manager.AudioManagerHandler)
        self.server.audio_directory = self.directory
        self.server.activity_log_path = self.directory / audio_manager.ACTIVITY_LOG_NAME
        self.server.payload_history_path = self.directory / audio_manager.PAYLOAD_HISTORY_NAME
        self.server.mqtt_topic = "home/ambient-audio"
        self.published_payloads = []
        self.server.publish_payload = self.published_payloads.append
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

    def test_page_displays_configured_listening_topic(self):
        for topic in ("home/ambient-audio", "speakers/+/requests/#", 'audio/<script>&"'):
            with self.subTest(topic=topic):
                self.server.mqtt_topic = topic
                status, page = self.request("GET", "/")
                self.assertEqual(status, 200)
                self.assertIn("Configured listening topic:", page)
                self.assertIn(audio_manager.escape(topic) + "</code>", page)
                self.assertNotIn("__MQTT_TOPIC__", page)
                self.assertNotIn("audio/<script>", page)

    def test_server_uses_same_topic_for_display_and_publishing(self):
        for config, topic in (({}, "home/ambient-audio"), ({"mqtt_topic": "speakers/requests"}, "speakers/requests")):
            with self.subTest(config=config), patch.object(audio_manager, "load_config", return_value=config), patch.object(
                audio_manager, "ThreadingHTTPServer",
            ) as server_class, patch.object(audio_manager, "mqtt_publisher") as publisher:
                audio_manager.run_server(SimpleNamespace(host="127.0.0.1", port=8000))
                self.assertEqual(server_class.return_value.mqtt_topic, topic)
                publisher.assert_called_once_with("localhost", 1883, topic)

    def test_only_allowed_files_can_be_uploaded_listed_and_deleted(self):
        self.assertEqual(self.upload("notice.mp3")[0], 201)
        self.assertEqual(self.upload("notes.txt")[0], 400)
        status, payload = self.request("GET", "/api/files")
        self.assertEqual((status, payload), (200, '["notice.mp3"]'))
        self.assertEqual(self.request("DELETE", "/api/files/notice.mp3")[0], 204)
        self.assertFalse((self.directory / "notice.mp3").exists())

    def test_allowed_audio_can_be_previewed_but_paths_cannot_be_read(self):
        (self.directory / "preview.wav").write_bytes(b"wave-data")
        status, payload = self.request("GET", "/api/files/preview.wav")
        self.assertEqual((status, payload), (200, "wave-data"))
        self.assertEqual(self.request("GET", "/api/files/%2E%2E%2Fconfig.json")[0], 404)

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
        self.server.activity_log_path.write_text(
            '{"time":"2026-01-01T00:00:00+00:00","event":"triggered","clip":"first.wav"}\n'
            'not-json\n'
            '{"time":"2026-01-01T00:01:00+00:00","event":"finished","clip":"second.mp3"}\n',
            encoding="utf-8",
        )
        status, payload = self.request("GET", "/api/activity")
        self.assertEqual(status, 200)
        self.assertEqual([event["clip"] for event in json.loads(payload)], ["second.mp3", "first.wav"])

    def test_payload_history_endpoint_returns_newest_payload_first(self):
        self.server.payload_history_path.write_text(
            '[{"received_at":"2026-01-01T00:00:00+00:00","payload":"first.wav"},'
            '{"received_at":"2026-01-01T00:01:00+00:00","payload":{"clip":"second.mp3"}}]',
            encoding="utf-8",
        )
        status, payload = self.request("GET", "/api/payloads")
        self.assertEqual(status, 200)
        self.assertEqual([entry["payload"] for entry in json.loads(payload)], [{"clip": "second.mp3"}, "first.wav"])

    def test_existing_clip_can_be_queued_as_a_payload(self):
        (self.directory / "notice.mp3").write_bytes(b"audio")
        request_payload = {"project": "defrost", "clip": "notice.mp3", "gain_db": 3}
        body = json.dumps(request_payload)
        status, _ = self.request(
            "POST",
            "/api/queue",
            body,
            {"Content-Type": "application/json", "Content-Length": str(len(body))},
        )
        self.assertEqual(status, 202)
        self.assertEqual(json.loads(self.published_payloads[0]), request_payload)

    def test_queue_rejects_missing_clip_and_nonexistent_file(self):
        body = '{"volume":100}'
        self.assertEqual(
            self.request("POST", "/api/queue", body, {"Content-Type": "application/json", "Content-Length": str(len(body))})[0],
            400,
        )
        body = '"missing.wav"'
        self.assertEqual(
            self.request("POST", "/api/queue", body, {"Content-Type": "application/json", "Content-Length": str(len(body))})[0],
            404,
        )
        self.assertEqual(self.published_payloads, [])

    def test_missing_paho_reports_environment_fix(self):
        (self.directory / "notice.mp3").write_bytes(b"audio")
        self.server.publish_payload = audio_manager.mqtt_publisher("localhost", 1883, "audio")
        # Model the system interpreter used by an incorrectly installed service.
        with patch.dict("sys.modules", {"paho.mqtt.publish": None}):
            status, payload = self.request(
                "POST", "/api/queue", '"notice.mp3"', {"Content-Type": "application/json"},
            )
        self.assertEqual(status, 503)
        self.assertIn("paho-mqtt", payload)
        self.assertIn("reinstall its service", payload)

    def test_broker_failure_does_not_expose_connection_details(self):
        (self.directory / "notice.mp3").write_bytes(b"audio")
        self.server.publish_payload = Mock(side_effect=ConnectionRefusedError("private connection details"))
        with patch.object(audio_manager.AudioManagerHandler, "log_message") as log:
            status, payload = self.request(
                "POST", "/api/queue", '"notice.mp3"', {"Content-Type": "application/json"},
            )
        self.assertEqual(status, 502)
        self.assertNotIn("private connection details", payload)
        log.assert_any_call("MQTT publish failed (%s).", "ConnectionRefusedError")

    def test_mqtt_publisher_forwards_payload_and_broker_settings(self):
        with patch("paho.mqtt.publish.single") as publish:
            audio_manager.mqtt_publisher("broker", 1884, "audio/queue")('{"clip":"notice.mp3"}')
        publish.assert_called_once_with(
            "audio/queue", payload='{"clip":"notice.mp3"}', hostname="broker", port=1884,
        )

    def test_service_install_preserves_virtual_environment_python_path(self):
        python = self.directory / ".venv" / "bin" / "python"
        original_resolve = Path.resolve

        def resolve(path):
            # Simulate Linux resolving a venv symlink to the system interpreter.
            return Path("/usr/bin/python3") if path == python else original_resolve(path)

        with patch.object(audio_manager.platform, "system", return_value="Linux"), patch.dict(
            os.environ, {"SUDO_USER": "player"},
        ), patch.dict("sys.modules", {"pwd": SimpleNamespace(getpwnam=Mock())}), patch.object(
            audio_manager.sys, "executable", str(python),
        ), patch.object(Path, "resolve", autospec=True, side_effect=resolve), patch.object(
            audio_manager, "systemd_path", return_value="/srv/audio",
        ), patch.object(audio_manager.subprocess, "run") as run:
            audio_manager.install_service(SimpleNamespace(host="127.0.0.1", port=8000))

        unit = run.call_args_list[0].kwargs["input"]
        self.assertIn(f"ExecStart={audio_manager.systemd_quote(str(python.absolute()))} ", unit)

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
