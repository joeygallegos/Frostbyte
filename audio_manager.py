#!/usr/bin/env python3
"""Serve a small page for managing audio clips in this directory."""

import argparse
import json
import os
import platform
import subprocess
import sys
from email.parser import BytesParser
from email.policy import default
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import unquote, urlparse


ALLOWED_EXTENSIONS = {".mp3", ".wav"}
MAX_UPLOAD_BYTES = 100 * 1024 * 1024
SERVICE_NAME = "frostbyte-audio-manager.service"


PAGE = """<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Frostbyte Audio Manager</title><style>
body{font:16px system-ui,sans-serif;max-width:720px;margin:3rem auto;padding:0 1rem;color:#1f2937}h1{margin-bottom:.25rem}
form{display:flex;gap:.75rem;align-items:center;margin:2rem 0}button{padding:.45rem .75rem;cursor:pointer}li{display:flex;gap:1rem;align-items:center;padding:.55rem 0;border-bottom:1px solid #ddd}li span{flex:1}#message{min-height:1.5em;color:#9b1c1c}
</style></head><body><h1>Frostbyte Audio Manager</h1><p>Upload or remove MP3 and WAV files in this directory.</p>
<form id="upload"><input name="audio" type="file" accept=".mp3,.wav,audio/mpeg,audio/wav" required><button>Upload</button></form>
<p id="message" role="status"></p><ul id="files"></ul><script>
const message=document.querySelector('#message'),files=document.querySelector('#files');
async function refresh(){const r=await fetch('/api/files');const names=await r.json();files.replaceChildren(...names.map(name=>{const li=document.createElement('li'),label=document.createElement('span'),button=document.createElement('button');label.textContent=name;button.textContent='Delete';button.onclick=async()=>{if(!confirm(`Delete ${name}?`))return;const r=await fetch('/api/files/'+encodeURIComponent(name),{method:'DELETE'});message.textContent=r.ok?'Deleted.':await r.text();if(r.ok)refresh()};li.append(label,button);return li}))}
document.querySelector('#upload').onsubmit=async event=>{event.preventDefault();const r=await fetch('/api/files',{method:'POST',body:new FormData(event.currentTarget)});message.textContent=r.ok?'Uploaded.':await r.text();if(r.ok){event.currentTarget.reset();refresh()}};refresh();
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


class AudioManagerHandler(BaseHTTPRequestHandler):
    """HTTP endpoints scoped to the server's fixed working directory."""

    server_version = "FrostbyteAudioManager/1.0"

    @property
    def audio_directory(self) -> Path:
        return self.server.audio_directory  # type: ignore[attr-defined]

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
            self.send_text(HTTPStatus.OK, PAGE, "text/html; charset=utf-8")
        elif path == "/api/files":
            self.send_text(HTTPStatus.OK, json.dumps(audio_files(self.audio_directory)), "application/json")
        else:
            self.send_error(HTTPStatus.NOT_FOUND)

    def do_POST(self) -> None:
        if urlparse(self.path).path != "/api/files":
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
User={systemd_quote(user)}
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
    unit = service_unit(script, working_directory, user, str(Path(sys.executable).resolve()), args.host, args.port)

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
    server = ThreadingHTTPServer((args.host, args.port), AudioManagerHandler)
    # Capturing this once makes all web writes and deletes stay in the directory
    # from which the process was started, even if another thread changes cwd.
    server.audio_directory = Path.cwd().resolve()  # type: ignore[attr-defined]
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
