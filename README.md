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

`audio_manager.py` is a small dependency-free web server for adding and
removing clips. It can only create or delete direct `.mp3` and `.wav` files in
the directory where it is started; it does not follow symlinks or allow paths.
Existing files are never overwritten. It listens on all network interfaces by
default so that phones and other devices on the local network can use it. It
intentionally has no login, so anyone who can reach the chosen port can upload
or delete audio files:

```bash
python3 audio_manager.py
```

Open `http://<server-LAN-IP>:8000/` from another device. To restrict access to
the server machine only, choose an explicit loopback address:

```bash
python3 audio_manager.py --host 127.0.0.1
```

On a Linux system with systemd, install it as an always-on service from the
audio directory:

```bash
python3 audio_manager.py --install
```

The installer prompts for `sudo` only to place, enable, and start
`frostbyte-audio-manager.service`; the service runs as the invoking user and
restarts after failures and reboot. Manage it with:

```bash
sudo systemctl status frostbyte-audio-manager.service
sudo systemctl restart frostbyte-audio-manager.service
```

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
reduce clipping and clamps requests to `max_gain_db` (12 dB by default). Start at
3-6 dB and increase carefully; extra gain cannot restore detail missing from a
poor recording and can still make distortion more noticeable.

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

List ALSA devices and test the configured alias:

```bash
aplay -L
speaker-test -D defrost -c 2
```

If playback works but mixer volume does not change, list the available controls
with `amixer scontrols` and update `volume_control` in `config.json`.
