"""A throwaway certificate authority and TLS endpoints for transport-security tests.

Everything runs on 127.0.0.1 in this process: a CA, server certificates (for the right name,
the wrong name, or self-signed), and tiny servers that speak just enough of PostgreSQL's,
Redis's or SMTP's opening to reach (or refuse) the TLS handshake.
"""

from __future__ import annotations

import contextlib
import datetime as dt
import ipaddress
import socket
import ssl
import threading
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from pathlib import Path

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID


def _name(common_name: str) -> x509.Name:
    return x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, common_name)])


def _write_key(key: ec.EllipticCurvePrivateKey, path: Path) -> None:
    path.write_bytes(
        key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
    )


@dataclass(frozen=True)
class ServerCert:
    cert: Path
    key: Path


@dataclass(frozen=True)
class TestCA:
    cert: Path
    key: ec.EllipticCurvePrivateKey
    directory: Path

    def server(
        self, name: str, *, sans: tuple[str, ...] = ("localhost", "127.0.0.1")
    ) -> ServerCert:
        """A certificate signed by this CA for the given names (DNS names or IPs)."""
        key = ec.generate_private_key(ec.SECP256R1())
        ca_cert = x509.load_pem_x509_certificate(self.cert.read_bytes())
        now = dt.datetime.now(dt.UTC)
        alt: list[x509.GeneralName] = []
        for san in sans:
            try:
                alt.append(x509.IPAddress(ipaddress.ip_address(san)))
            except ValueError:
                alt.append(x509.DNSName(san))
        cert = (
            x509.CertificateBuilder()
            .subject_name(_name(sans[0]))
            .issuer_name(ca_cert.subject)
            .public_key(key.public_key())
            .serial_number(x509.random_serial_number())
            .not_valid_before(now - dt.timedelta(minutes=5))
            .not_valid_after(now + dt.timedelta(days=1))
            .add_extension(x509.SubjectAlternativeName(alt), critical=False)
            .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
            .sign(self.key, hashes.SHA256())
        )
        cert_path = self.directory / f"{name}.crt"
        key_path = self.directory / f"{name}.key"
        cert_path.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
        _write_key(key, key_path)
        return ServerCert(cert_path, key_path)


def make_ca(directory: Path, name: str = "Arkray Test CA") -> TestCA:
    key = ec.generate_private_key(ec.SECP256R1())
    now = dt.datetime.now(dt.UTC)
    cert = (
        x509.CertificateBuilder()
        .subject_name(_name(name))
        .issuer_name(_name(name))
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - dt.timedelta(minutes=5))
        .not_valid_after(now + dt.timedelta(days=1))
        .add_extension(x509.BasicConstraints(ca=True, path_length=0), critical=True)
        .add_extension(
            x509.KeyUsage(
                digital_signature=True,
                key_cert_sign=True,
                crl_sign=True,
                content_commitment=False,
                key_encipherment=False,
                data_encipherment=False,
                key_agreement=False,
                encipher_only=False,
                decipher_only=False,
            ),
            critical=True,
        )
        .sign(key, hashes.SHA256())
    )
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{name.replace(' ', '_')}.crt"
    path.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    return TestCA(path, key, directory)


def server_context(cert: ServerCert) -> ssl.SSLContext:
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(cert.cert, cert.key)
    return context


Handler = Callable[[socket.socket], None]


@contextlib.contextmanager
def serve(handler: Handler) -> Iterator[int]:
    """Accept connections on 127.0.0.1 (a free port, yielded) and run `handler` on each in a
    thread, until the block ends."""
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.bind(("127.0.0.1", 0))
    listener.listen(8)
    listener.settimeout(0.2)
    stop = threading.Event()

    def loop() -> None:
        while not stop.is_set():
            try:
                conn, _ = listener.accept()
            except (TimeoutError, OSError):
                continue
            conn.settimeout(5)

            def run(c: socket.socket = conn) -> None:
                with contextlib.suppress(Exception), c:
                    handler(c)

            threading.Thread(target=run, daemon=True).start()

    thread = threading.Thread(target=loop, daemon=True)
    thread.start()
    try:
        yield listener.getsockname()[1]
    finally:
        stop.set()
        thread.join(2)
        listener.close()


def tls_then_close(context: ssl.SSLContext | None) -> Handler:
    """A server that completes the TLS handshake (if it can) and then hangs up."""

    def handle(conn: socket.socket) -> None:
        if context is None:
            return
        with (
            context.wrap_socket(conn, server_side=True) as tls,
            contextlib.suppress(Exception),
        ):
            tls.recv(1)

    return handle


def postgres(context: ssl.SSLContext | None) -> Handler:
    """PostgreSQL's SSLRequest: answer 'S' and hand over to TLS, or 'N' (no TLS: what a
    downgrading attacker or a server without TLS says)."""

    def handle(conn: socket.socket) -> None:
        request = conn.recv(8)
        if len(request) != 8:
            return
        if context is None:
            conn.sendall(b"N")
            with contextlib.suppress(Exception):
                conn.recv(1024)  # the client's next move (it should hang up)
            return
        conn.sendall(b"S")
        tls_then_close(context)(conn)

    return handle


def smtp(context: ssl.SSLContext | None, *, offer_starttls: bool = True) -> Handler:
    """Enough SMTP to send one message, with or without STARTTLS on offer; the messages
    received are not kept."""

    def handle(conn: socket.socket) -> None:
        stream: socket.socket | ssl.SSLSocket = conn
        stream.sendall(b"220 test ESMTP\r\n")
        buffer = b""
        in_data = False
        while True:
            chunk = stream.recv(4096)
            if not chunk:
                return
            buffer += chunk
            while b"\r\n" in buffer:
                line, buffer = buffer.split(b"\r\n", 1)
                if in_data:
                    if line == b".":
                        in_data = False
                        stream.sendall(b"250 queued\r\n")
                    continue
                verb = line[:4].upper()
                if verb in (b"EHLO", b"HELO"):
                    tls_line = b"250-STARTTLS\r\n" if offer_starttls and context else b""
                    secure = isinstance(stream, ssl.SSLSocket)
                    stream.sendall(b"250-test\r\n" + (b"" if secure else tls_line) + b"250 OK\r\n")
                elif verb == b"STAR" and context is not None and offer_starttls:
                    stream.sendall(b"220 go ahead\r\n")
                    stream = context.wrap_socket(conn, server_side=True)
                    buffer = b""
                elif verb == b"DATA":
                    in_data = True
                    stream.sendall(b"354 end with .\r\n")
                elif verb == b"QUIT":
                    stream.sendall(b"221 bye\r\n")
                    return
                else:
                    stream.sendall(b"250 OK\r\n")

    return handle
