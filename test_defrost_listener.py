import importlib
import subprocess
import unittest
from pathlib import Path
from unittest.mock import mock_open, patch

import paho.mqtt.client  # Load package metadata before the narrow Path config mocks.


with patch.object(Path, "exists", return_value=True), patch.object(
    Path, "open", mock_open(read_data="{}")
):
    defrost_listener = importlib.import_module("defrost_listener")


class GainedPlaybackTests(unittest.TestCase):
    def test_gain_renders_explicit_pcm_rate_before_playback(self):
        completed = subprocess.CompletedProcess([], 0, "", "")
        with patch.object(defrost_listener.subprocess, "run", side_effect=[completed, completed]) as run:
            self.assertTrue(defrost_listener.play_clip_with_gain(Path("alert.mp3"), 3))

        decoder_command = run.call_args_list[0].args[0]
        player_command = run.call_args_list[1].args[0]
        self.assertEqual(decoder_command[decoder_command.index("-ar") + 1], "48000")
        self.assertEqual(decoder_command[decoder_command.index("-c:a") + 1], "pcm_s16le")
        self.assertEqual(player_command[-1], decoder_command[-1])

    def test_failed_gain_render_falls_back_before_playback_starts(self):
        failed = subprocess.CompletedProcess([], 1, "", "decode failed")
        with patch.object(defrost_listener.subprocess, "run", return_value=failed) as run:
            self.assertFalse(defrost_listener.play_clip_with_gain(Path("alert.mp3"), 3))
        run.assert_called_once()


if __name__ == "__main__":
    unittest.main()
