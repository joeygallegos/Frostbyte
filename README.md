# Frostbyte

Frostbyte is a small MQTT-controlled audio player for Linux systems using ALSA. It
listens for a filename and playback settings, validates the requested clip, and
plays requests sequentially through the configured speaker.

## Requirements

- Python 3
- [Paho MQTT](https://pypi.org/project/paho-mqtt/)
- ALSA utilities (`aplay`, `amixer`, and `speaker-test`)
- `mpg123` for MP3 playback
- FFmpeg when using digital gain
- An MQTT broker reachable from the player

On Debian or Raspberry Pi OS, install the system packages with:

```bash
sudo apt update
sudo apt install alsa-utils ffmpeg mpg123 mosquitto-clients python3-venv
```

Then install the Python dependency in a virtual environment:

```bash
python3 -m venv .venv
.venv/bin/pip install paho-mqtt
```

## Configuration

Copy `config.example.json` to `config.json`, then update the broker, audio
directory, and ALSA settings for the host:

```bash
cp config.example.json config.json
```

The main settings are:

| Setting | Purpose |
| --- | --- |
| `mqtt_host`, `mqtt_port`, `mqtt_topic` | MQTT connection and subscribed topic |
| `audio_dir` | Directory containing playable clips |
| `activity_log_path` | Shared playback activity log displayed by the browser manager |
| `payload_history_path` | Bounded JSON cache of the latest received MQTT payloads |
| `allowed_exts` | Accepted filename extensions |
| `alsa_device` | ALSA playback device, such as `default` or `defrost` |
| `volume_control` | Mixer control changed by `amixer`, commonly `PCM` or `Master` |
| `default_volume` | Default hardware output level from 0 to 100 |
| `default_gain_db` | Digital gain applied to every clip unless overridden |
| `max_gain_db` | Upper safety bound for requested gain (capped at 30 dB) |
| `clip_gains_db` | Persistent gain overrides keyed by exact filename |
| `log_level` | Python logging level, such as `INFO` or `DEBUG` |

### Select a speaker

The speaker manager can discover ALSA devices and save the selected one as the
friendly `defrost` alias without replacing unrelated `~/.asoundrc` content:

```bash
chmod +x speaker-manager.sh
./speaker-manager.sh
```

Set `"alsa_device": "defrost"` in `config.json` after saving the alias.

## Run

Start the listener directly with:

```bash
.venv/bin/python defrost_listener.py
```

For a host configured with `defrost.service`:

```bash
sudo systemctl status defrost.service
sudo journalctl -u defrost.service -f
```

## Manage audio files in a browser

`audio_manager.py` is a small web server for adding and
removing clips. It can only create or delete direct `.mp3` and `.wav` files in
the configured `audio_dir` (or its starting directory when no `config.json`
exists); it does not follow symlinks or allow paths.
Existing files are never overwritten, and the **Preview** button plays a file
in the browser directly from that directory. It listens on all network
interfaces by default so that phones and other devices on the local network can
use it. It intentionally has no login, so anyone who can reach the chosen port
can upload, delete, or trigger audio files:

```bash
.venv/bin/python audio_manager.py
```

Open `http://<server-LAN-IP>:8000/` from another device. To restrict access to
the server machine only, choose an explicit loopback address:

```bash
.venv/bin/python audio_manager.py --host 127.0.0.1
```

The page shows the **Configured listening topic** from `mqtt_topic` above the
tabs (default: `home/ambient-audio`), including any MQTT wildcards. This is the
configured subscription, not live connection status. Restart both the manager
and listener after changing the topic in `config.json`.

The **Soundboard** tab provides a Launchpad-style grid of colored pads, one per
audio file. Open `http://<server-LAN-IP>:8000/#soundboard` to go straight to it.
Use **Play on** to choose **This device** (the default, through your laptop,
iPad, or other browser) or **MQTT speaker**. Tap or click a pad, or focus it with
Tab and press Enter or Space. Local playback has browser audio controls and a
new clip replaces the current one; it does not publish to MQTT. The MQTT option
uses the same payload settings as **Queue**, with clips playing sequentially.
**Queued** means broker acceptance, not playback completion. Changing output
stops local playback but does not cancel requests already queued on the MQTT
speaker. Use **Refresh sounds** after adding or removing files.

On a Linux system with systemd, install it as an always-on service from the
Frostbyte project directory (the directory containing `defrost_listener.py`):

```bash
.venv/bin/python audio_manager.py --install
```

The installer prompts for `sudo` only to place, enable, and start
`frostbyte-audio-manager.service`; the service runs as the invoking user and
restarts after failures and reboot. Manage it with:

```bash
sudo systemctl status frostbyte-audio-manager.service
sudo systemctl restart frostbyte-audio-manager.service
```

The **Playback activity** tab shows the latest 200 accepted, ignored, started, and ended
playback attempts. The listener stores this shared activity log as
`playback_activity.jsonl` next to `defrost_listener.py` by default. Set
`activity_log_path` in `config.json` to use another location. The log must be
writable by the listener and readable by the manager.
After updating this version, restart the listener so it begins writing activity:

```bash
sudo systemctl restart defrost.service
```

The **Payload history** tab shows the latest 200 MQTT messages received by the
listener. They are cached atomically in `payload_history.json`, or at the path
set by `payload_history_path`. Both JSON objects and plain-filename messages are
preserved. Payloads larger than 16 KiB are shown as truncated and cannot be
replayed.

Use **Queue** beside a file to publish a standard payload, or edit and submit a
JSON object in the Payload history tab. **Queue again** republishes a saved
payload. The manager uses `mqtt_host`, `mqtt_port`, and `mqtt_topic` from
`config.json`; a successful response means the broker accepted the publish, and
the listener records the message when it receives it.

Manual queueing requires Paho in the manager's Python environment, just as the
listener does. If uploads and previews work but queueing fails, rerun the install
command above with `.venv/bin/python`, then restart the service:

```bash
sudo systemctl restart frostbyte-audio-manager.service
sudo journalctl -u frostbyte-audio-manager.service -n 50 --no-pager
```

Reinstalling updates the service's Python path; restarting alone keeps the old
path. Broker failures record the exception type in the service journal without
logging payloads or connection credentials. If the environment is correct, check
that the manager's `config.json` points to the reachable broker and listener topic.

Because the site has no login, restrict port 8000 at the network boundary or
bind it to loopback if other network users must not view payload metadata or
trigger playback.

## Send playback requests

Publish a JSON message containing at least a `clip`:

```bash
mosquitto_pub -h fruitsalad -p 1883 -t home/ambient-audio \
  -m '{"clip":"doorbell.wav","volume":95}'
```

A plain filename is also accepted and uses all configured defaults:

```bash
mosquitto_pub -h fruitsalad -p 1883 -t home/ambient-audio -m 'doorbell.wav'
```

Only the filename is honored; directory components are stripped, and the file
must exist inside `audio_dir` with an allowed extension.

## Make quiet clips louder

`volume` controls the ALSA mixer and cannot go above 100. To amplify a quiet
recording beyond that ceiling, add `gain_db` to the message:

```bash
mosquitto_pub -h fruitsalad -p 1883 -t home/ambient-audio \
  -m '{"clip":"quiet-alert.mp3","volume":100,"gain_db":6}'
```

Gain is measured in decibels: `6` dB is roughly twice the signal amplitude and
`12` dB is roughly four times. Frostbyte sends gained audio through a limiter to
reduce clipping, renders it as 48 kHz PCM to preserve playback speed across
source formats, and clamps requests to `max_gain_db` (12 dB by default). Start
at 3-6 dB and increase carefully; extra gain cannot restore detail missing from
a poor recording and can still make distortion more noticeable.

For a consistently quiet file, add it to `clip_gains_db` in `config.json`:

```json
"clip_gains_db": {
  "quiet-alert.mp3": 6,
  "soft-chime.wav": 9
}
```

An explicit message-level `gain_db` overrides the per-file setting. The per-file
setting overrides `default_gain_db`. If FFmpeg is unavailable, Frostbyte logs a
warning and plays the clip without gain.

## Troubleshooting

Run automated checks with `python -m unittest discover`. Soundboard script tests
also use Node.js when available and are skipped if it is not installed; Node.js
is not required to run the application.

List ALSA devices and test the configured alias:

```bash
aplay -L
speaker-test -D defrost -c 2
```

If playback works but mixer volume does not change, list the available controls
with `amixer scontrols` and update `volume_control` in `config.json`.
