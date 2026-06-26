"""chrome_proxy.py — Local HTTP proxy that routes Screaming Frog through undetected Chrome.

Usage:
    python suyog/chrome_proxy.py

Then in Screaming Frog:
    Configuration > System > Proxy          →  Manual: 127.0.0.1 : 8080
    Configuration > Spider > Advanced       →  uncheck "Check SSL Certificate"
    Configuration > Speed > Max Connections →  1
    Configuration > Speed > Request Timeout →  120s
"""

import os
import sys
import ssl
import socket
import socketserver
import threading
import signal
import logging
import datetime
from pathlib import Path

try:
    from cryptography import x509
    from cryptography.x509.oid import NameOID
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    HAS_CRYPTOGRAPHY = True
except ImportError:
    HAS_CRYPTOGRAPHY = False

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from seleniumbase import SB

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

HERE = Path(__file__).resolve().parent
HOST = "127.0.0.1"
PORT = int(os.environ.get("PROXY_PORT", "8080"))

_CHALLENGE_TITLES = {
    "just a moment",
    "attention required",
    "checking your browser",
    "please wait",
}

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
log = logging.getLogger("chrome_proxy")

SSL_CTX = None  # set in main() via _setup_ssl()


# ---------------------------------------------------------------------------
# SSL cert generation
# ---------------------------------------------------------------------------

def _generate_cert(cert_path: Path, key_path: Path):
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    subject = issuer = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "chrome-proxy")])
    cert = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(issuer)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(datetime.datetime.now(datetime.timezone.utc))
        .not_valid_after(datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(days=3650))
        .add_extension(x509.SubjectAlternativeName([x509.DNSName("*")]), critical=False)
        .sign(key, hashes.SHA256())
    )
    cert_path.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    key_path.write_bytes(key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.TraditionalOpenSSL,
        serialization.NoEncryption(),
    ))


def _setup_ssl():
    global SSL_CTX
    if not HAS_CRYPTOGRAPHY:
        log.warning(
            "'cryptography' package not installed — HTTPS will not work. "
            "Run: pip install cryptography"
        )
        return
    cert_path = HERE / "proxy.crt"
    key_path  = HERE / "proxy.key"
    if not cert_path.exists():
        log.info("Generating self-signed certificate...")
        _generate_cert(cert_path, key_path)
        log.info("Certificate saved: %s", cert_path)
    SSL_CTX = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    SSL_CTX.check_hostname = False
    SSL_CTX.load_cert_chain(str(cert_path), str(key_path))
    log.info("SSL ready.")


# ---------------------------------------------------------------------------
# ChromeBridge — owns the single long-lived SBase/Chrome session
# ---------------------------------------------------------------------------

class ChromeBridge:
    """Wraps a single SB(uc=True) session. All fetch() calls are serialized."""

    def __init__(self):
        self._lock = threading.Lock()
        self._sb = None
        self._sb_ctx = None
        self._cdp_active = False

    def start(self):
        """Start Chrome. Must be called once from the main thread before serving."""
        self._sb_ctx = SB(uc=True, test=True)
        self._sb = self._sb_ctx.__enter__()
        log.info("Chrome started.")

    def stop(self):
        if self._sb_ctx is not None:
            try:
                self._sb_ctx.__exit__(None, None, None)
            except Exception:
                pass
        log.info("Chrome stopped.")

    def fetch(self, url: str) -> tuple:
        """
        Navigate Chrome to url and return (http_status, content_type, html_body).
        Serialized: only one request goes to Chrome at a time.
        """
        with self._lock:
            try:
                if self._cdp_active:
                    self._sb.cdp.open(url)
                else:
                    self._sb.activate_cdp_mode(url)
                    self._cdp_active = True

                self._sb.sleep(2)
                self._sb.solve_captcha()
                self._sb.sleep(3)

                # Retry if still on a challenge page
                for _ in range(3):
                    title = (self._sb.cdp.evaluate("document.title") or "").lower()
                    if not any(t in title for t in _CHALLENGE_TITLES):
                        break
                    self._sb.solve_captcha()
                    self._sb.sleep(3)

                html = self._sb.cdp.get_page_source()
                log.info("OK  %s", url)
                return 200, "text/html; charset=utf-8", html

            except Exception as exc:
                log.error("ERR %s — %s", url, exc)
                return 502, "text/plain; charset=utf-8", f"ChromeBridge error: {exc}"


# ---------------------------------------------------------------------------
# Proxy handler
# ---------------------------------------------------------------------------

BRIDGE: ChromeBridge = None  # set in main()


class ProxyHandler(socketserver.BaseRequestHandler):

    def handle(self):
        conn = self.request
        conn.settimeout(30)
        try:
            # Read until end of HTTP headers
            raw = b""
            while b"\r\n\r\n" not in raw:
                chunk = conn.recv(4096)
                if not chunk:
                    return
                raw += chunk

            header_block = raw.split(b"\r\n\r\n")[0].decode("utf-8", errors="replace")
            first_line = header_block.splitlines()[0]
            parts = first_line.split(" ", 2)
            if len(parts) < 2:
                return
            method, target = parts[0].upper(), parts[1]

            if method == "CONNECT":
                self._handle_connect(conn, target)
            else:
                self._handle_http(conn, method, target)

        except Exception as exc:
            log.debug("Handler exception: %s", exc)
            try:
                self._send(conn, 500, "text/plain", str(exc))
            except Exception:
                pass
        finally:
            try:
                conn.close()
            except Exception:
                pass

    # ------------------------------------------------------------------
    def _handle_http(self, conn, method, target_url):
        if not target_url.startswith("http"):
            self._send(conn, 400, "text/plain", "Bad Request")
            return
        conn.settimeout(None)  # Chrome fetch can take 60+ s
        status, ctype, body = BRIDGE.fetch(target_url)
        if method == "HEAD":
            body = ""
        self._send(conn, status, ctype, body)

    # ------------------------------------------------------------------
    def _handle_connect(self, conn, target):
        """
        HTTPS via CONNECT tunnel with SSL termination.
        After the 200 ACK, wraps the socket in TLS so Screaming Frog
        gets a real handshake, then reads the plaintext request inside.
        """
        host = target.rsplit(":", 1)[0]
        conn.sendall(b"HTTP/1.1 200 Connection established\r\n\r\n")

        if not SSL_CTX:
            # No TLS support — Chrome still fetches but SF won't get the response
            conn.settimeout(None)
            BRIDGE.fetch(f"https://{host}/")
            return

        # TLS handshake with Screaming Frog
        try:
            ssl_conn = SSL_CTX.wrap_socket(conn, server_side=True)
        except ssl.SSLError as e:
            log.debug("SSL wrap failed for %s: %s", host, e)
            return

        # Read the plaintext HTTP request from inside the TLS tunnel
        ssl_conn.settimeout(15)
        raw = b""
        try:
            while b"\r\n\r\n" not in raw:
                chunk = ssl_conn.recv(4096)
                if not chunk:
                    break
                raw += chunk
        except socket.timeout:
            pass

        path, method = "/", "GET"
        try:
            first = raw.split(b"\r\n\r\n")[0].decode("utf-8", errors="replace").splitlines()[0]
            tparts = first.split()
            if len(tparts) >= 2:
                method = tparts[0].upper()
                if tparts[1].startswith("/"):
                    path = tparts[1]
        except Exception:
            pass

        ssl_conn.settimeout(None)
        url = f"https://{host}{path}"
        status, ctype, body = BRIDGE.fetch(url)
        if method == "HEAD":
            body = ""
        self._send(ssl_conn, status, ctype, body)
        try:
            ssl_conn.unwrap()
        except Exception:
            pass

    # ------------------------------------------------------------------
    @staticmethod
    def _send(conn, status: int, ctype: str, body):
        reason = "OK" if status == 200 else "Error"
        if isinstance(body, str):
            body = body.encode("utf-8")
        header = (
            f"HTTP/1.1 {status} {reason}\r\n"
            f"Content-Type: {ctype}\r\n"
            f"Content-Length: {len(body)}\r\n"
            f"Connection: close\r\n"
            f"\r\n"
        ).encode("utf-8")
        conn.sendall(header + body)


# ---------------------------------------------------------------------------
# Server
# ---------------------------------------------------------------------------

class ProxyServer(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main():
    global BRIDGE
    _setup_ssl()
    BRIDGE = ChromeBridge()
    BRIDGE.start()

    server = ProxyServer((HOST, PORT), ProxyHandler)
    log.info("Proxy listening on %s:%d", HOST, PORT)
    log.info("Screaming Frog: Configuration > System > Proxy > Manual: %s:%d", HOST, PORT)
    log.info("Ctrl+C to stop.")

    def _shutdown(sig=None, frame=None):
        log.info("Shutting down...")
        threading.Thread(target=server.shutdown, daemon=True).start()
        BRIDGE.stop()
        sys.exit(0)

    signal.signal(signal.SIGINT, _shutdown)
    signal.signal(signal.SIGTERM, _shutdown)

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        _shutdown()


if __name__ == "__main__":
    main()
