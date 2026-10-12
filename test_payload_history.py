import json
import tempfile
import unittest
from pathlib import Path

import payload_history


class PayloadHistoryTests(unittest.TestCase):
    def test_records_json_and_plain_payloads_in_a_bounded_json_file(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            path = Path(temporary_directory) / "payload_history.json"
            for index in range(payload_history.HISTORY_LIMIT + 2):
                payload_history.record_payload(
                    path,
                    json.dumps({"clip": f"clip-{index}.mp3"}),
                    f"2026-01-01T00:00:{index:02d}+00:00",
                )
            payload_history.record_payload(path, "plain.wav", "2026-01-02T00:00:00+00:00")

            entries = payload_history.read_payload_history(path)
            self.assertEqual(len(entries), payload_history.HISTORY_LIMIT)
            self.assertEqual(entries[-1]["payload"], "plain.wav")
            self.assertEqual(entries[0]["payload"]["clip"], "clip-3.mp3")

    def test_marks_oversized_payload_as_not_replayable(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            path = Path(temporary_directory) / "payload_history.json"
            payload_history.record_payload(path, "x" * (payload_history.MAX_PAYLOAD_CHARACTERS + 1))

            entry = payload_history.read_payload_history(path)[0]
            self.assertTrue(entry["truncated"])
            self.assertEqual(len(entry["payload"]), payload_history.MAX_PAYLOAD_CHARACTERS)


if __name__ == "__main__":
    unittest.main()
