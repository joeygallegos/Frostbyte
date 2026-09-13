#!/usr/bin/env python3
import json
import logging
import math
import os
import queue
import subprocess
import threading
from pathlib import Path
from typing import Optional

import paho.mqtt.client as mqtt

# === Load config.json ===
CONFIG_PATH = Path(__file__).resolve().parent / "config.json"

if not CONFIG_PATH.exists():
    raise FileNotFoundError(f"Missing config.json at: {CONFIG_PATH}")

with CONFIG_PATH.open() as f:
    CONFIG = json.load(f)

MQTT_HOST = CONFIG.get("mqtt_host", "localhost")
MQTT_PORT = CONFIG.get("mqtt_port", 1883)
MQTT_TOPIC = CONFIG.get("mqtt_topic", "home/ambient-audio")
CLIENT_ID = CONFIG.get("client_id", "frostbyte-defrost-listener")

AUDIO_DIR = Path(CONFIG.get("audio_dir", str(Path(__file__).resolve().parent)))
ALLOWED_EXTS = set(CONFIG.get("allowed_exts", [".wav", ".mp3"]))

ALSA_DEVICE = CONFIG.get("alsa_device", "default")
VOLUME_CONTROL = CONFIG.get("volume_control", "PCM")

LOG_LEVEL = CONFIG.get("log_level", "INFO").upper()
DEFAULT_VOLUME = CONFIG.get("default_volume", 80)  # 0-100
DEFAULT_GAIN_DB = CONFIG.get("default_gain_db", 0)
MAX_GAIN_DB = CONFIG.get("max_gain_db", 12)
CLIP_GAINS_DB = CONFIG.get("clip_gains_db", {})

MIN_GAIN_DB = -60

logging.basicConfig(
    level=LOG_LEVEL,
    format="%(asctime)s [%(levelname)s] %(message)s"
)

play_queue: "queue.Queue[dict]" = queue.Queue()


def clamp(value: float, minimum: float, maximum: float) -> float:
    return max(minimum, min(value, maximum))


def number_setting(value, default: float, name: str) -> float:
    """Return a finite numeric setting, or its default when invalid."""
    try:
        number = float(value)
    except (TypeError, ValueError):
        logging.warning("Invalid %s %r; using %s", name, value, default)
        return float(default)

    if not math.isfinite(number):
        logging.warning("Invalid %s %r; using %s", name, value, default)
        return float(default)

    return number


def set_volume(percent):
    """Set ALSA output volume (0-100)."""
    percent = round(clamp(number_setting(percent, 80, "volume"), 0, 100))
    subprocess.run(
        ["amixer", "sset", VOLUME_CONTROL, f"{percent}%"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        check=False,
    )


def sanitize_clip_name(clip: str) -> str:
    return os.path.basename(clip.strip())


def resolve_clip_path(clip_name: str) -> Optional[Path]:
    clip_name = sanitize_clip_name(clip_name)
    clip_path = AUDIO_DIR / clip_name

    if not clip_path.exists():
        logging.warning("Requested clip does not exist: %s", clip_name)
        return None

    if clip_path.suffix.lower() not in ALLOWED_EXTS:
        logging.warning("Unsupported extension for: %s", clip_name)
        return None

    return clip_path


def play_clip_with_gain(path: Path, gain_db: float) -> bool:
    """Decode a clip through FFmpeg, apply gain and limiting, then send it to ALSA."""
    audio_filter = f"volume={gain_db:g}dB,alimiter=limit=0.95"
    decoder_cmd = [
        "ffmpeg",
        "-nostdin",
        "-hide_banner",
        "-loglevel",
        "error",
        "-i",
        str(path),
        "-af",
        audio_filter,
        "-f",
        "wav",
        "pipe:1",
    ]
    player_cmd = ["aplay", "-q", "-D", ALSA_DEVICE]

    logging.debug("Running: %s | %s", " ".join(decoder_cmd), " ".join(player_cmd))

    try:
        decoder = subprocess.Popen(
            decoder_cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
    except FileNotFoundError:
        logging.warning(
            "FFmpeg is not installed; playing %s without the requested gain",
            path.name,
        )
        return False

    try:
        player = subprocess.Popen(
            player_cmd,
            stdin=decoder.stdout,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
        )
    except (FileNotFoundError, OSError):
        decoder.kill()
        decoder.wait()
        raise

    # Allow SIGPIPE to reach FFmpeg if aplay exits early.
    if decoder.stdout:
        decoder.stdout.close()

    _, player_stderr = player.communicate()
    decoder_stderr = decoder.stderr.read() if decoder.stderr else b""
    decoder_returncode = decoder.wait()

    if decoder_returncode != 0:
        logging.error("FFmpeg stderr: %s", decoder_stderr.decode(errors="replace").strip())
    if player.returncode != 0:
        logging.error("aplay stderr: %s", player_stderr.decode(errors="replace").strip())

    return True


def play_clip(path: Path, gain_db: float = 0):
    logging.info("Playing clip: %s on device: %s", path.name, ALSA_DEVICE)

    if gain_db:
        logging.info("Applying %.1f dB digital gain", gain_db)
        try:
            if play_clip_with_gain(path, gain_db):
                return
        except Exception as exc:
            logging.error("Error while playing with gain: %s", exc)
            return

    if path.suffix.lower() == ".wav":
        cmd = ["aplay", "-q", "-D", ALSA_DEVICE, str(path)]
    elif path.suffix.lower() == ".mp3":
        cmd = ["mpg123", "-q", "-a", ALSA_DEVICE, str(path)]
    else:
        logging.error("No handler for extension: %s", path.suffix)
        return

    logging.debug("Running: %s", " ".join(cmd))

    try:
        result = subprocess.run(cmd, capture_output=True, text=True)
        if result.returncode != 0:
            logging.error("Player stderr: %s", result.stderr)
    except Exception as exc:
        logging.error("Error while playing: %s", exc)


def get_gain_db(path: Path, meta: dict) -> float:
    """Resolve message, per-clip, and default gain in descending priority."""
    configured_max = number_setting(MAX_GAIN_DB, 12, "max_gain_db")
    max_gain_db = clamp(configured_max, 0, 30)

    if "gain_db" in meta:
        raw_gain = meta["gain_db"]
    elif isinstance(CLIP_GAINS_DB, dict) and path.name in CLIP_GAINS_DB:
        raw_gain = CLIP_GAINS_DB[path.name]
    else:
        raw_gain = DEFAULT_GAIN_DB

    gain_db = number_setting(raw_gain, 0, "gain_db")
    bounded_gain_db = clamp(gain_db, MIN_GAIN_DB, max_gain_db)
    if bounded_gain_db != gain_db:
        logging.warning(
            "Gain %.1f dB is outside the allowed range; using %.1f dB",
            gain_db,
            bounded_gain_db,
        )

    return bounded_gain_db


def playback_worker():
    logging.info("Playback worker ready.")
    while True:
        item = play_queue.get()
        if item is None:
            break

        clip_path = item.get("path")
        meta = item.get("meta", {})

        volume = meta.get("volume", DEFAULT_VOLUME)
        set_volume(volume)

        gain_db = get_gain_db(clip_path, meta)
        play_clip(clip_path, gain_db)
        play_queue.task_done()


def handle_message(payload_raw: bytes):
    text = payload_raw.decode("utf-8", errors="replace").strip()
    logging.info(f"Received payload: {text}")

    try:
        data = json.loads(text)
        clip_name = data.get("clip")
        meta = data
    except json.JSONDecodeError:
        clip_name = text
        meta = {}

    if not clip_name:
        logging.warning("No 'clip' in message - ignoring.")
        return

    clip_path = resolve_clip_path(clip_name)
    if not clip_path:
        return

    play_queue.put({"path": clip_path, "meta": meta})


def on_connect(client, userdata, flags, reason_code, properties=None):
    if reason_code == 0:
        logging.info("Connected to MQTT broker.")
        client.subscribe(MQTT_TOPIC)
        logging.info("Subscribed → %s", MQTT_TOPIC)
    else:
        logging.error(f"MQTT connection failed: reason={reason_code}")


def on_message(client, userdata, msg):
    handle_message(msg.payload)


def main():
    worker = threading.Thread(target=playback_worker, daemon=True)
    worker.start()

    client = mqtt.Client(client_id=CLIENT_ID, callback_api_version=mqtt.CallbackAPIVersion.VERSION2)
    client.on_connect = on_connect
    client.on_message = on_message
    client.connect(MQTT_HOST, MQTT_PORT, keepalive=60)

    try:
        client.loop_forever()
    except KeyboardInterrupt:
        logging.info("Shutting down.")
    finally:
        play_queue.put(None)
        worker.join()
        client.disconnect()


if __name__ == "__main__":
    main()
