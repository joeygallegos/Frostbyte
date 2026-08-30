#!/usr/bin/env python3
import json
import logging
import os
import queue
import subprocess
import threading
from pathlib import Path

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

logging.basicConfig(
    level=LOG_LEVEL,
    format="%(asctime)s [%(levelname)s] %(message)s"
)

play_queue: "queue.Queue[dict]" = queue.Queue()


def set_volume(percent: int):
    """Set ALSA output volume (0-100)."""
    subprocess.run(
        ["amixer", "sset", "PCM", f"{percent}%"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        check=False,
    )


def sanitize_clip_name(clip: str) -> str:
    return os.path.basename(clip.strip())


def resolve_clip_path(clip_name: str) -> Path:
    clip_name = sanitize_clip_name(clip_name)
    clip_path = AUDIO_DIR / clip_name

    if not clip_path.exists():
        logging.warning("Requested clip does not exist: %s", clip_name)
        return None

    if clip_path.suffix.lower() not in ALLOWED_EXTS:
        logging.warning("Unsupported extension for: %s", clip_name)
        return None

    return clip_path


def play_clip(path: Path):
    logging.info("Playing clip: %s on device: %s", path.name, ALSA_DEVICE)

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
    except Exception as e:
        logging.error("Error while playing: %s", e)


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

        play_clip(clip_path)
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
