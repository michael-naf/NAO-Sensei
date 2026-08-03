from __future__ import annotations

import re
import subprocess
import threading

import qrcode

# cloudflared writes its logs (including the assigned URL) to stderr, not
# stdout — redirected together below so we don't miss it either way.
_URL_PATTERN = re.compile(r"https://[a-zA-Z0-9-]+\.trycloudflare\.com")


class Tunnel:
    """Launches `cloudflared tunnel --url <local>` and exposes the public
    HTTPS URL it prints once the tunnel is up (§9.1). server.public_url
    (config) overrides this entirely — that path never touches this class.

    Falling back to Mode A needs no coordination from here: the web server
    is always plain HTTP underneath, tunnel or not, and the student page
    itself feature-detects `navigator.mediaDevices` to decide whether voice
    is available (§9.4) — undefined outside a secure context, i.e. exactly
    when there's no working tunnel. If this process dies mid-lecture,
    already-connected Mode B students keep working until it does; new
    connections over the LAN IP just get typed-only, no restart needed.
    """

    def __init__(self, local_url: str) -> None:
        self._local_url = local_url
        self._process: subprocess.Popen | None = None
        self._url: str | None = None
        self._url_event = threading.Event()

    def start(self) -> None:
        self._process = subprocess.Popen(
            ["cloudflared", "tunnel", "--url", self._local_url],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
        )
        threading.Thread(target=self._read_output, daemon=True).start()

    def _read_output(self) -> None:
        assert self._process is not None and self._process.stdout is not None
        for line in self._process.stdout:
            if self._url is None:
                match = _URL_PATTERN.search(line)
                if match:
                    self._url = match.group(0)
                    self._url_event.set()

    def wait_for_url(self, timeout: float = 15.0) -> str | None:
        """Blocking — call from an executor, not the event loop thread."""
        if self._url_event.wait(timeout):
            return self._url
        return None

    def is_alive(self) -> bool:
        return self._process is not None and self._process.poll() is None

    def stop(self) -> None:
        if self._process is not None and self._process.poll() is None:
            self._process.terminate()


def print_qr(url: str) -> None:
    qr = qrcode.QRCode(border=1)
    qr.add_data(url)
    qr.make(fit=True)
    try:
        # print_ascii() writes Unicode block characters; Windows consoles
        # default to a codepage (cp1252) that can't encode them, which
        # raised UnicodeEncodeError and would otherwise crash the whole
        # startup sequence over a display nicety. The URL itself — the part
        # that actually matters for connecting — is printed either way.
        qr.print_ascii(invert=True)
    except UnicodeEncodeError:
        print("(QR code not renderable in this console — use the URL below)")
    print(url)
