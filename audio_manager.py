#!/usr/bin/env python3
"""Serve a small page for managing audio clips in this directory."""

import argparse
import json
import os
import platform
import shutil
import subprocess
import sys
from email.parser import BytesParser
from email.policy import default
from html import escape
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable
from urllib.parse import unquote, urlparse

from payload_history import read_payload_history


ALLOWED_EXTENSIONS = {".mp3", ".wav"}
MAX_UPLOAD_BYTES = 100 * 1024 * 1024
MAX_QUEUE_PAYLOAD_BYTES = 16 * 1024
SERVICE_NAME = "frostbyte-audio-manager.service"
ACTIVITY_LOG_NAME = "playback_activity.jsonl"
PAYLOAD_HISTORY_NAME = "payload_history.json"
ACTIVITY_LIMIT = 200
MAX_ACTIVITY_LOG_READ_BYTES = 1024 * 1024


class MissingMQTTDependencyError(RuntimeError):
    """The manager's Python environment cannot load its MQTT publisher."""


PAGE = """<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Frostbyte Audio Manager</title><style>
body{font:16px system-ui,sans-serif;max-width:800px;margin:3rem auto;padding:0 1rem;color:#1f2937}h1{margin-bottom:.25rem}
form{display:flex;gap:.75rem;align-items:center;margin:2rem 0}button{padding:.45rem .75rem;cursor:pointer}li{display:flex;gap:.75rem;align-items:center;padding:.65rem 0;border-bottom:1px solid #ddd}li span{flex:1}.status{min-height:1.5em;color:#9b1c1c}.tabs{display:flex;gap:.5rem;margin:2rem 0 0}.tabs button[aria-selected="true"]{font-weight:700}.hidden{display:none}.event,.payload{display:block}.event time,.payload time{display:block;color:#6b7280;font-size:.85em}.payload pre{white-space:pre-wrap;overflow-wrap:anywhere;background:#f3f4f6;padding:.75rem}.payload button{margin-top:.25rem}#payload-form{display:block}textarea{box-sizing:border-box;width:100%;font:14px ui-monospace,monospace;padding:.75rem}audio{max-width:100%}
.tabs{flex-wrap:wrap}.soundboard{display:grid;grid-template-columns:repeat(4,minmax(0,1fr));gap:.75rem;padding:1rem;background:#111827;border-radius:1rem}.sound-pad{min-width:0;min-height:110px;aspect-ratio:1;border:2px solid var(--pad-color);border-radius:.65rem;background:#1f2937;color:#f9fafb;box-shadow:inset 0 -6px 0 var(--pad-color);font:600 .95rem system-ui,sans-serif;overflow-wrap:anywhere;touch-action:manipulation}.sound-pad:hover{background:#374151}.sound-pad:focus-visible{outline:3px solid #fff;outline-offset:3px}.sound-pad:active{transform:translateY(2px)}.sound-pad:disabled{opacity:.65;cursor:wait}.sound-pad.queued{background:#14532d}.sound-pad.failed{background:#7f1d1d}.sound-pad span{display:-webkit-box;-webkit-box-orient:vertical;-webkit-line-clamp:3;overflow:hidden}.sound-pad small{display:block;margin-top:.5rem;font-weight:400}#soundboard-message{color:#334155}@media(max-width:520px){.soundboard{grid-template-columns:repeat(2,minmax(0,1fr))}}
</style></head><body><h1>Frostbyte Audio Manager</h1><p>Upload, preview, or queue MP3 and WAV clips.</p>
<p>Configured listening topic: <code style="overflow-wrap:anywhere">__MQTT_TOPIC__</code></p>
<nav class="tabs" aria-label="Audio manager sections"><button type="button" data-tab="files" aria-controls="files-panel" aria-selected="true">Files</button><button type="button" data-tab="soundboard" aria-controls="soundboard-panel" aria-selected="false">Soundboard</button><button type="button" data-tab="activity" aria-controls="activity-panel" aria-selected="false">Playback activity</button><button type="button" data-tab="payloads" aria-controls="payloads-panel" aria-selected="false">Payload history</button></nav>
<section id="files-panel" role="tabpanel"><form id="upload"><input name="audio" type="file" accept=".mp3,.wav,audio/mpeg,audio/wav" required><button>Upload</button></form><p id="message" class="status" role="status"></p><ul id="files"></ul></section>
<section id="soundboard-panel" class="hidden" role="tabpanel" aria-label="Soundboard"><h2>Soundboard</h2><p>Tap a pad to play a clip. Use Tab to focus a pad, then Enter or Space to trigger it.</p><p><label for="soundboard-output">Play on </label><select id="soundboard-output"><option value="local">This device</option><option value="mqtt">MQTT speaker</option></select> <button type="button" id="refresh-soundboard">Refresh sounds</button></p><p id="soundboard-help">This device: plays through this browser. A new clip replaces the current clip.</p><audio id="soundboard-player" controls preload="none" aria-label="Soundboard playback"></audio><p id="soundboard-message" class="status" role="status"></p><div id="soundboard" class="soundboard" aria-label="Sound pads"></div></section>
<section id="activity-panel" class="hidden" role="tabpanel"><p><button type="button" id="refresh-activity">Refresh activity</button></p><p id="activity-message" class="status" role="status"></p><ul id="activity"></ul></section>
<section id="payloads-panel" class="hidden" role="tabpanel"><form id="payload-form"><label for="payload">Queue a JSON payload</label><textarea id="payload" rows="9" required>{
  "project": "defrost",
  "clip": "file.mp3",
  "source": "website_manual",
  "priority": 1,
  "volume": 100,
  "gain_db": 3
}</textarea><p><button>Queue payload</button> <button type="button" id="refresh-payloads">Refresh history</button></p></form><p id="payload-message" class="status" role="status"></p><ul id="payload-history"></ul></section><script>
const message=document.querySelector('#message'),files=document.querySelector('#files'),activity=document.querySelector('#activity'),activityMessage=document.querySelector('#activity-message'),payloadMessage=document.querySelector('#payload-message'),payloadHistory=document.querySelector('#payload-history');
function defaultPayload(clip){return {project:'defrost',clip,source:'website_manual',priority:1,volume:100,gain_db:3,timestamp:new Date().toISOString()}}
async function queuePayload(payload,status=message){status.textContent='Queueing…';try{const r=await fetch('/api/queue',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(payload)});status.textContent=r.ok?`Queued ${typeof payload==='string'?payload:payload.clip}.`:await r.text();return r.ok}catch(error){status.textContent='Could not reach the audio manager.';return false}}
async function refresh(){const r=await fetch('/api/files');const names=await r.json();files.replaceChildren(...names.map(name=>{const li=document.createElement('li'),label=document.createElement('span'),preview=document.createElement('button'),queue=document.createElement('button'),remove=document.createElement('button');label.textContent=name;preview.type=queue.type=remove.type='button';preview.textContent='Preview';preview.onclick=()=>{let player=li.querySelector('audio');if(!player){player=document.createElement('audio');player.controls=true;player.preload='none';player.src='/api/files/'+encodeURIComponent(name);li.append(player)}player.play().catch(()=>{})};queue.textContent='Queue';queue.onclick=()=>queuePayload(defaultPayload(name));remove.textContent='Delete';remove.onclick=async()=>{if(!confirm(`Delete ${name}?`))return;const r=await fetch('/api/files/'+encodeURIComponent(name),{method:'DELETE'});message.textContent=r.ok?'Deleted.':await r.text();if(r.ok)refresh()};li.append(label,preview,queue,remove);return li}))}
async function refreshActivity(){activityMessage.textContent='Loading recent activity…';try{const r=await fetch('/api/activity');if(!r.ok)throw new Error('Request failed');const events=await r.json();activityMessage.textContent=events.length?'':'No recent playback activity.';activity.replaceChildren(...events.map(event=>{const li=document.createElement('li'),text=document.createElement('span'),time=document.createElement('time'),date=new Date(event.time);li.className='event';text.textContent=`${event.event}: ${event.clip}`;time.dateTime=event.time;time.textContent=Number.isNaN(date.valueOf())?event.time:date.toLocaleString();li.append(text,time);return li}))}catch(error){activity.replaceChildren();activityMessage.textContent='Could not load playback activity. Try refreshing.'}}
async function refreshPayloads(){payloadMessage.textContent='Loading payload history…';try{const r=await fetch('/api/payloads');if(!r.ok)throw new Error('Request failed');const entries=await r.json();payloadMessage.textContent=entries.length?'':'No payloads have been received yet.';payloadHistory.replaceChildren(...entries.map(entry=>{const li=document.createElement('li'),time=document.createElement('time'),pre=document.createElement('pre'),button=document.createElement('button'),date=new Date(entry.received_at);li.className='payload';time.dateTime=entry.received_at;time.textContent=Number.isNaN(date.valueOf())?entry.received_at:date.toLocaleString();pre.textContent=typeof entry.payload==='string'?entry.payload:JSON.stringify(entry.payload,null,2);button.type='button';button.textContent='Queue again';button.disabled=entry.truncated===true;button.onclick=()=>queuePayload(entry.payload,payloadMessage);li.append(time,pre,button);return li}))}catch(error){payloadHistory.replaceChildren();payloadMessage.textContent='Could not load payload history. Try refreshing.'}}
const soundboard=document.querySelector('#soundboard'),soundboardMessage=document.querySelector('#soundboard-message');
const soundboardOutput=document.querySelector('#soundboard-output'),soundboardPlayer=document.querySelector('#soundboard-player');
let localPlaybackRequest=0;
soundboardPlayer.onended=()=>{soundboardMessage.textContent='Playback finished on this device.'};
soundboardOutput.onchange=()=>{
  // Stop local audio on a destination change so it cannot keep playing while
  // the controls say MQTT. Already published MQTT requests cannot be recalled.
  localPlaybackRequest++;
  soundboardPlayer.pause();
  soundboardPlayer.removeAttribute('src');
  soundboardPlayer.load();
  soundboardPlayer.hidden=soundboardOutput.value!=='local';
  document.querySelector('#soundboard-help').textContent=soundboardOutput.value==='local'
    ?'This device: plays through this browser. A new clip replaces the current clip.'
    :'MQTT speaker: queues clips in order. Switching output does not cancel queued clips.';
  soundboardMessage.textContent='';
  refreshSoundboard();
};
async function refreshSoundboard(){
  soundboardMessage.textContent='Loading sounds…';
  soundboard.replaceChildren();
  try{
    const response=await fetch('/api/files');
    if(!response.ok)throw new Error('Request failed');
    const names=await response.json();
    const colors=['#a78bfa','#60a5fa','#22d3ee','#34d399','#facc15','#fb923c','#f472b6','#c084fc'];
    soundboard.replaceChildren(...names.map((name,index)=>{
      const pad=document.createElement('button'),label=document.createElement('span'),state=document.createElement('small');
      pad.type='button';
      pad.className='sound-pad';
      pad.title=name;
      pad.style.setProperty('--pad-color',colors[index%colors.length]);
      pad.setAttribute('aria-label','Play '+name);
      // File names are untrusted display text; never interpret them as HTML.
      label.textContent=name;
      state.textContent=soundboardOutput.value==='local'?'Play here':'Queue on speaker';
      pad.append(label,state);
      pad.onclick=async()=>{
        // Prevent duplicate taps while browser playback or publishing starts.
        pad.disabled=true;
        pad.classList.remove('queued','failed');
        try{
          if(soundboardOutput.value==='mqtt'){
            // Broker acceptance does not mean remote playback has started.
            const request=localPlaybackRequest,status=document.createElement('span');
            state.textContent='Queueing…';
            soundboardMessage.textContent='Queueing…';
            const queued=await queuePayload(defaultPayload(name),status);
            // A slow broker response must not overwrite feedback after the
            // user switches back to local playback.
            if(request===localPlaybackRequest)soundboardMessage.textContent=status.textContent;
            pad.classList.add(queued?'queued':'failed');
            state.textContent=queued?'Queued · tap again':'Failed · retry';
          }else{
            const request=++localPlaybackRequest;
            state.textContent='Starting…';
            // Reuse one browser player to avoid overlapping local sounds. A
            // newer tap or output change can cancel a pending play() promise.
            soundboardPlayer.src='/api/files/'+encodeURIComponent(name);
            try{
              await soundboardPlayer.play();
              if(request===localPlaybackRequest){
                soundboardMessage.textContent='Playing on this device: '+name;
                state.textContent='Play again';
              }else state.textContent='Play here';
            }catch(error){
              state.textContent=request===localPlaybackRequest?'Failed · retry':'Play here';
              if(request===localPlaybackRequest){
                pad.classList.add('failed');
                soundboardMessage.textContent='Could not play '+name+' on this device. Try again or choose MQTT speaker.';
              }
            }
          }
        }finally{pad.disabled=false}
      };
      return pad;
    }));
    soundboardMessage.textContent=names.length?'':'No sounds yet. Upload MP3 or WAV clips in Files, then refresh sounds.';
  }catch(error){soundboardMessage.textContent='Could not load sounds. Try refreshing.'}
}
function selectTab(selected){
  // Only known tabs can select a panel; unknown bookmarks return to Files.
  const buttons=[...document.querySelectorAll('[data-tab]')];
  const button=buttons.find(tab=>tab.dataset.tab===selected)||buttons[0];
  selected=button.dataset.tab;
  document.querySelectorAll('[role="tabpanel"]').forEach(panel=>panel.classList.toggle('hidden',panel.id!==selected+'-panel'));
  buttons.forEach(tab=>tab.setAttribute('aria-selected',tab===button));
  if(selected==='activity')refreshActivity();
  if(selected==='payloads')refreshPayloads();
  if(selected==='soundboard')refreshSoundboard();
}
document.querySelectorAll('[data-tab]').forEach(button=>button.onclick=()=>{if(location.hash==='#'+button.dataset.tab)selectTab(button.dataset.tab);else location.hash=button.dataset.tab});
window.addEventListener('hashchange',()=>selectTab(location.hash.slice(1)));
document.querySelector('#refresh-soundboard').onclick=refreshSoundboard;
selectTab(location.hash.slice(1));
document.querySelector('#refresh-activity').onclick=refreshActivity;document.querySelector('#refresh-payloads').onclick=refreshPayloads;
document.querySelector('#payload-form').onsubmit=async event=>{event.preventDefault();try{const payload=JSON.parse(document.querySelector('#payload').value);if(await queuePayload(payload,payloadMessage))setTimeout(refreshPayloads,500)}catch(error){payloadMessage.textContent='Enter a valid JSON payload.'}};
document.querySelector('#upload').onsubmit=async event=>{event.preventDefault();const r=await fetch('/api/files',{method:'POST',body:new FormData(event.currentTarget)});message.textContent=r.ok?'Uploaded.':await r.text();if(r.ok){event.currentTarget.reset();refresh()}};document.querySelector('#payload').value=JSON.stringify(defaultPayload('file.mp3'),null,2);refresh();
</script></body></html>"""


def valid_audio_name(name: str) -> bool:
    """Accept a single, non-empty MP3 or WAV filename, never a path."""
    if not isinstance(name, str) or not name or "/" in name or "\\" in name:
        return False
    return Path(name).name == name and Path(name).suffix.lower() in ALLOWED_EXTENSIONS


def audio_files(directory: Path) -> list[str]:
    """Return only regular allowed audio files from the configured directory."""
    return sorted(
        entry.name
        for entry in directory.iterdir()
        if entry.is_file() and not entry.is_symlink() and valid_audio_name(entry.name)
    )


def load_config() -> dict[str, Any]:
    """Load the listener's broker and shared-file settings when available."""
    path = Path(__file__).resolve().parent / "config.json"
    try:
        with path.open(encoding="utf-8") as config_file:
            value = json.load(config_file)
            return value if isinstance(value, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def mqtt_publisher(host: str, port: int, topic: str) -> Callable[[str], None]:
    """Build the narrow publisher used by the queue endpoint."""
    def publish(payload_text: str) -> None:
        # Paho is already Frostbyte's MQTT dependency. Importing it here keeps
        # non-server helpers and unit tests usable without connecting to a broker.
        try:
            import paho.mqtt.publish as mqtt_publish
        except ImportError as error:
            raise MissingMQTTDependencyError from error

        mqtt_publish.single(topic, payload=payload_text, hostname=host, port=port)

    return publish


def queued_payload_text(payload: Any, directory: Path) -> str:
    """Validate a playback request and serialize it for the listener's topic."""
    if isinstance(payload, dict):
        clip = payload.get("clip")
        serialized = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    elif isinstance(payload, str):
        clip = payload
        serialized = payload
    else:
        raise ValueError("Payload must be a JSON object or a filename string.")

    if not valid_audio_name(clip):
        raise ValueError("Payload clip must be a plain .mp3 or .wav filename.")
    target = directory / clip
    if target.is_symlink() or not target.is_file():
        raise FileNotFoundError(clip)
    return serialized


def activity_events(path: Path) -> list[dict[str, str]]:
    """Read recent listener events without exposing a symlinked or oversized log."""
    try:
        if path.is_symlink() or not path.is_file():
            return []
        with path.open("rb") as activity_log:
            activity_log.seek(max(0, path.stat().st_size - MAX_ACTIVITY_LOG_READ_BYTES))
            lines = activity_log.read().splitlines()
    except OSError:
        return []

    events = []
    for line in lines[-ACTIVITY_LIMIT:]:
        try:
            event = json.loads(line)
        except (UnicodeDecodeError, json.JSONDecodeError):
            continue
        if all(isinstance(event.get(key), str) for key in ("time", "event", "clip")):
            events.append({key: event[key] for key in ("time", "event", "clip")})
    return list(reversed(events))


class AudioManagerHandler(BaseHTTPRequestHandler):
    """HTTP endpoints scoped to the server's fixed working directory."""

    server_version = "FrostbyteAudioManager/1.0"

    @property
    def audio_directory(self) -> Path:
        return self.server.audio_directory  # type: ignore[attr-defined]

    @property
    def payload_history_path(self) -> Path:
        return self.server.payload_history_path  # type: ignore[attr-defined]

    @property
    def activity_log_path(self) -> Path:
        return self.server.activity_log_path  # type: ignore[attr-defined]

    def send_text(self, status: HTTPStatus, text: str, content_type: str = "text/plain; charset=utf-8") -> None:
        encoded = text.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(encoded)))
        self.end_headers()
        self.wfile.write(encoded)

    def do_GET(self) -> None:
        path = urlparse(self.path).path
        if path == "/":
            # Topics are configuration text, not markup; escape them before
            # inserting into the page so special characters remain literal.
            page = PAGE.replace("__MQTT_TOPIC__", escape(self.server.mqtt_topic))  # type: ignore[attr-defined]
            self.send_text(HTTPStatus.OK, page, "text/html; charset=utf-8")
        elif path == "/api/files":
            self.send_text(HTTPStatus.OK, json.dumps(audio_files(self.audio_directory)), "application/json")
        elif path == "/api/activity":
            self.send_text(HTTPStatus.OK, json.dumps(activity_events(self.activity_log_path)), "application/json")
        elif path == "/api/payloads":
            # Newest-first ordering makes a freshly received or replayed payload
            # visible without requiring users to scan to the bottom of the page.
            entries = list(reversed(read_payload_history(self.payload_history_path)))
            self.send_text(HTTPStatus.OK, json.dumps(entries), "application/json")
        elif path.startswith("/api/files/"):
            self.serve_audio(unquote(path[len("/api/files/"):]))
        else:
            self.send_error(HTTPStatus.NOT_FOUND)

    def serve_audio(self, name: str) -> None:
        """Serve one validated direct-child audio file for the browser preview control."""
        if not valid_audio_name(name):
            self.send_error(HTTPStatus.NOT_FOUND)
            return
        target = self.audio_directory / name
        try:
            if target.is_symlink() or not target.is_file():
                raise FileNotFoundError
            content_type = "audio/mpeg" if target.suffix.lower() == ".mp3" else "audio/wav"
            size = target.stat().st_size
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(size))
            self.end_headers()
            with target.open("rb") as audio_file:
                shutil.copyfileobj(audio_file, self.wfile)
        except (FileNotFoundError, OSError):
            self.send_error(HTTPStatus.NOT_FOUND)

    def do_POST(self) -> None:
        path = urlparse(self.path).path
        if path == "/api/queue":
            self.queue_payload()
            return
        if path != "/api/files":
            self.send_error(HTTPStatus.NOT_FOUND)
            return

        try:
            content_length = int(self.headers.get("Content-Length", ""))
        except ValueError:
            self.send_text(HTTPStatus.BAD_REQUEST, "A valid Content-Length header is required.")
            return
        if content_length < 1 or content_length > MAX_UPLOAD_BYTES:
            self.send_text(HTTPStatus.REQUEST_ENTITY_TOO_LARGE, "Upload must be between 1 byte and 100 MiB.")
            return
        if not self.headers.get_content_type() == "multipart/form-data":
            self.send_text(HTTPStatus.UNSUPPORTED_MEDIA_TYPE, "Use multipart/form-data to upload a file.")
            return

        # The email parser handles quoted boundaries and filename encodings without
        # trusting the browser-provided filename as a filesystem path.
        body = self.rfile.read(content_length)
        message = BytesParser(policy=default).parsebytes(
            f"Content-Type: {self.headers['Content-Type']}\r\nMIME-Version: 1.0\r\n\r\n".encode() + body
        )
        part = next((item for item in message.iter_parts() if item.get_filename()), None)
        if part is None:
            self.send_text(HTTPStatus.BAD_REQUEST, "Choose a file to upload.")
            return

        name = part.get_filename()
        if not valid_audio_name(name):
            self.send_text(HTTPStatus.BAD_REQUEST, "Only plain .mp3 and .wav filenames are allowed.")
            return
        destination = self.audio_directory / name
        try:
            # Exclusive creation prevents an upload from silently replacing a clip.
            with destination.open("xb") as output:
                output.write(part.get_payload(decode=True) or b"")
        except FileExistsError:
            self.send_text(HTTPStatus.CONFLICT, "A file with that name already exists.")
            return
        except OSError:
            self.send_text(HTTPStatus.INTERNAL_SERVER_ERROR, "Could not save the upload.")
            return
        self.send_text(HTTPStatus.CREATED, "Uploaded.")

    def queue_payload(self) -> None:
        """Validate and publish one audio request to the configured listener."""
        if self.headers.get_content_type() != "application/json":
            self.send_text(HTTPStatus.UNSUPPORTED_MEDIA_TYPE, "Use application/json for a payload.")
            return
        try:
            content_length = int(self.headers.get("Content-Length", ""))
        except ValueError:
            self.send_text(HTTPStatus.BAD_REQUEST, "A valid Content-Length header is required.")
            return
        if content_length < 1 or content_length > MAX_QUEUE_PAYLOAD_BYTES:
            self.send_text(HTTPStatus.REQUEST_ENTITY_TOO_LARGE, "Payload must be between 1 byte and 16 KiB.")
            return
        try:
            payload = json.loads(self.rfile.read(content_length))
            payload_text = queued_payload_text(payload, self.audio_directory)
        except (UnicodeDecodeError, json.JSONDecodeError):
            self.send_text(HTTPStatus.BAD_REQUEST, "Enter a valid JSON payload.")
            return
        except ValueError as error:
            self.send_text(HTTPStatus.BAD_REQUEST, str(error))
            return
        except FileNotFoundError:
            self.send_text(HTTPStatus.NOT_FOUND, "The requested audio clip does not exist.")
            return

        try:
            self.server.publish_payload(payload_text)  # type: ignore[attr-defined]
        except MissingMQTTDependencyError:
            self.send_text(
                HTTPStatus.SERVICE_UNAVAILABLE,
                "MQTT publishing is unavailable: run the audio manager with the Python "
                "environment containing paho-mqtt, then reinstall its service with that Python.",
            )
            return
        except Exception as error:
            # Broker errors are intentionally generic: connection details and
            # credentials should not be disclosed to unauthenticated web clients.
            self.log_message("MQTT publish failed (%s).", type(error).__name__)
            self.send_text(HTTPStatus.BAD_GATEWAY, "Could not queue the payload with the MQTT broker.")
            return
        self.send_text(HTTPStatus.ACCEPTED, "Queued.")

    def do_DELETE(self) -> None:
        prefix = "/api/files/"
        path = urlparse(self.path).path
        if not path.startswith(prefix):
            self.send_error(HTTPStatus.NOT_FOUND)
            return
        name = unquote(path[len(prefix):])
        if not valid_audio_name(name):
            self.send_text(HTTPStatus.BAD_REQUEST, "Only plain .mp3 and .wav filenames are allowed.")
            return
        target = self.audio_directory / name
        try:
            # unlink removes only this direct child; paths and symlinks are rejected.
            if target.is_symlink() or not target.is_file():
                raise FileNotFoundError
            target.unlink()
        except FileNotFoundError:
            self.send_text(HTTPStatus.NOT_FOUND, "File not found.")
            return
        except OSError:
            self.send_text(HTTPStatus.INTERNAL_SERVER_ERROR, "Could not delete the file.")
            return
        self.send_text(HTTPStatus.NO_CONTENT, "")

    def log_message(self, format: str, *args: object) -> None:
        """Keep ordinary request logs useful without including request bodies."""
        sys.stderr.write("%s - %s\n" % (self.address_string(), format % args))


def systemd_quote(value: str) -> str:
    """Quote an argument in the limited syntax accepted by systemd unit files."""
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


def systemd_path(path: Path) -> str:
    """Format an absolute path for a systemd path setting, not a command argument."""
    value = str(path)
    # Check systemd's POSIX syntax instead of Path.is_absolute(), because this
    # generator is tested on Windows but only installed on Linux.
    if not value.startswith("/"):
        raise ValueError("systemd paths must be absolute")
    # systemd treats surrounding quotes as literal characters for
    # WorkingDirectory=. Escape spaces using its documented \x20 syntax instead.
    return value.replace("\\", "\\\\").replace(" ", "\\x20")


def service_unit(script: Path, working_directory: Path, user: str, python: str, host: str, port: int) -> str:
    """Build a unit that restarts the manager after failures or reboot."""
    return f"""[Unit]
Description=Frostbyte Audio Manager
After=network.target

[Service]
Type=simple
User={user}
WorkingDirectory={systemd_path(working_directory)}
ExecStart={systemd_quote(python)} {systemd_quote(str(script))} --host {systemd_quote(host)} --port {port}
Restart=always
RestartSec=3
NoNewPrivileges=true
PrivateTmp=true
UMask=0077

[Install]
WantedBy=multi-user.target
"""


def install_service(args: argparse.Namespace) -> None:
    """Install and start the systemd unit; root privileges are used only for systemd."""
    if platform.system() != "Linux":
        raise RuntimeError("--install is supported only on Linux systems using systemd.")
    user = os.environ.get("SUDO_USER") or os.environ.get("USER")
    if not user:
        raise RuntimeError("Could not determine which non-root user should run the service.")
    # Confirm the environment value is a real Linux account before placing it
    # directly in the unit file.
    import pwd

    try:
        pwd.getpwnam(user)
    except KeyError as error:
        raise RuntimeError(f"Linux user does not exist: {user}") from error
    script = Path(__file__).resolve()
    working_directory = Path.cwd().resolve()
    # A venv's Python can be a symlink to system Python on Linux. Resolving it
    # loses the venv and its Paho dependency when systemd starts the manager.
    unit = service_unit(script, working_directory, user, str(Path(sys.executable).absolute()), args.host, args.port)

    # Write the unit through sudo rather than creating a local temporary file.
    # Normal web-server operation therefore never writes anything but audio
    # files in its fixed working directory.
    subprocess.run(
        ["sudo", "tee", f"/etc/systemd/system/{SERVICE_NAME}"],
        input=unit,
        text=True,
        stdout=subprocess.DEVNULL,
        check=True,
    )
    subprocess.run(["sudo", "chmod", "644", f"/etc/systemd/system/{SERVICE_NAME}"], check=True)
    subprocess.run(["sudo", "systemctl", "daemon-reload"], check=True)
    subprocess.run(["sudo", "systemctl", "enable", "--now", SERVICE_NAME], check=True)
    print(f"Installed and started {SERVICE_NAME}. Open http://{args.host}:{args.port}/")


def run_server(args: argparse.Namespace) -> None:
    config = load_config()
    server = ThreadingHTTPServer((args.host, args.port), AudioManagerHandler)
    # The manager and listener must resolve clips against the same directory or
    # a web request can be accepted locally and then rejected by the listener.
    server.audio_directory = Path(config.get("audio_dir", Path.cwd())).expanduser().resolve()  # type: ignore[attr-defined]
    if not server.audio_directory.is_dir():  # type: ignore[attr-defined]
        server.server_close()
        raise RuntimeError(f"Audio directory does not exist: {server.audio_directory}")  # type: ignore[attr-defined]
    activity_setting = config.get("activity_log_path", str(Path(__file__).resolve().parent / ACTIVITY_LOG_NAME))
    server.activity_log_path = Path(activity_setting)  # type: ignore[attr-defined]
    history_setting = config.get("payload_history_path", str(Path(__file__).resolve().parent / PAYLOAD_HISTORY_NAME))
    server.payload_history_path = Path(history_setting)  # type: ignore[attr-defined]
    # Use the same startup setting for display and publishing so the page cannot
    # advertise a changed config value before the manager has been restarted.
    server.mqtt_topic = str(config.get("mqtt_topic", "home/ambient-audio"))  # type: ignore[attr-defined]
    server.publish_payload = mqtt_publisher(  # type: ignore[attr-defined]
        str(config.get("mqtt_host", "localhost")),
        int(config.get("mqtt_port", 1883)),
        server.mqtt_topic,  # type: ignore[attr-defined]
    )
    print(f"Serving {server.audio_directory} at http://{args.host}:{args.port}/")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Manage MP3 and WAV files in the current directory.")
    parser.add_argument("--host", default="0.0.0.0", help="Bind address (default: 0.0.0.0).")
    parser.add_argument("--port", default=8000, type=int, help="TCP port (default: 8000).")
    parser.add_argument("--install", action="store_true", help="Install and start a systemd service on Linux.")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    if not 1 <= args.port <= 65535:
        raise SystemExit("--port must be between 1 and 65535.")
    try:
        if args.install:
            install_service(args)
        else:
            run_server(args)
    except RuntimeError as error:
        raise SystemExit(str(error)) from error


if __name__ == "__main__":
    main()
