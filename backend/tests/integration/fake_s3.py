"""A small S3-compatible HTTP endpoint for tests, with a fault switch per request
(docs/testing.md#attachment-storage-faults).

Real sockets, so the real botocore client (through django-storages' S3Storage, built by
config.settings.base.attachment_s3_backend) talks to it exactly as to a bucket: SigV4
headers, retries, timeouts, aws-chunked bodies. Path-style, one bucket, objects in memory:
PUT / GET / HEAD / DELETE of objects, multipart uploads (s3transfer's path above 8 MB),
HEAD of the bucket, ListObjectsV2.

Faults (`Fault(kind, ...)`), queued for the next matching requests or set for all:
  ok, 403, 404, 500, 503 (SlowDown), slow (sleep before answering), slow_body (GET: half
  the body, a pause, the rest), drop (GET: half the body, then the connection closes; other
  methods: closed without an answer), hang (accepted, never answered), truncate (PUT:
  answered 200 but only half the bytes kept).
A stand-in for the protocol, not for S3's semantics (consistency, multipart limits,
encryption): docs/testing.md says what still needs a real bucket.
"""

from __future__ import annotations

import contextlib
import hashlib
import socket
import threading
import time
import uuid
from collections import deque
from dataclasses import dataclass, field
from datetime import UTC, datetime
from email.utils import formatdate
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, unquote, urlsplit
from xml.sax.saxutils import escape

ERRORS = {
    "403": (403, "AccessDenied", "Access Denied"),
    "404": (404, "NoSuchKey", "The specified key does not exist."),
    "500": (500, "InternalError", "We encountered an internal error. Please try again."),
    "503": (503, "SlowDown", "Please reduce your request rate."),
}


@dataclass(frozen=True)
class Fault:
    kind: str
    seconds: float = 0.0
    methods: frozenset[str] = frozenset()  # empty: every method

    def applies(self, method: str) -> bool:
        return not self.methods or method in self.methods


def fault(kind: str, seconds: float = 0.0, *methods: str) -> Fault:
    return Fault(kind, seconds, frozenset(methods))


@dataclass
class FakeS3:
    bucket: str = "arkray-fault-tests"
    objects: dict[str, bytes] = field(default_factory=dict)
    uploads: dict[str, dict[int, bytes]] = field(default_factory=dict)  # multipart, by id
    queued: deque[Fault] = field(default_factory=deque)
    standing: Fault | None = None
    requests: list[tuple[str, str]] = field(default_factory=list)
    closing: threading.Event = field(default_factory=threading.Event)
    lock: threading.Lock = field(default_factory=threading.Lock)

    def __post_init__(self) -> None:
        fake = self

        class Handler(_Handler):
            server_fake = fake

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.server.daemon_threads = True
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    @property
    def endpoint(self) -> str:
        return f"http://127.0.0.1:{self.server.server_address[1]}"

    def once(self, *faults: Fault) -> None:
        """Faults for the next requests they apply to, one each, in order."""
        with self.lock:
            self.queued.extend(faults)

    def always(self, standing: Fault | None) -> None:
        """A fault for every request (None: healthy again)."""
        with self.lock:
            self.standing = standing

    def heal(self) -> None:
        with self.lock:
            self.queued.clear()
            self.standing = None

    def take(self, method: str) -> Fault | None:
        with self.lock:
            for index, queued in enumerate(self.queued):
                if queued.applies(method):
                    del self.queued[index]
                    return queued
            if self.standing is not None and self.standing.applies(method):
                return self.standing
            return None

    def count(self, method: str | None = None) -> int:
        with self.lock:
            return sum(1 for m, _ in self.requests if method is None or m == method)

    def close(self) -> None:
        self.closing.set()
        self.server.shutdown()
        self.server.server_close()


class _Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_fake: FakeS3

    def log_message(self, format: str, *args: object) -> None:
        pass

    # --- dispatch ---------------------------------------------------------------------------
    def do_PUT(self) -> None:
        self._handle("PUT")

    def do_GET(self) -> None:
        self._handle("GET")

    def do_HEAD(self) -> None:
        self._handle("HEAD")

    def do_DELETE(self) -> None:
        self._handle("DELETE")

    def do_POST(self) -> None:
        self._handle("POST")

    def _handle(self, method: str) -> None:
        fake = self.server_fake
        parts = urlsplit(self.path)
        bucket, _, key = parts.path.lstrip("/").partition("/")
        key = unquote(key)
        with fake.lock:
            fake.requests.append((method, key))
        body = self._body() if method in {"PUT", "POST"} else b""
        found = fake.take(method)
        if found is not None:
            if found.kind == "hang":
                fake.closing.wait(found.seconds or 60)
                self.close_connection = True
                return
            if found.kind == "slow":
                time.sleep(found.seconds)
            elif found.kind in ERRORS:
                self._error(*ERRORS[found.kind], head=method == "HEAD")
                return
            elif found.kind == "drop" and method != "GET":
                self._drop()
                return
            elif found.kind == "truncate" and method == "PUT":
                body = body[: len(body) // 2]
        if bucket != fake.bucket:
            self._error(
                404, "NoSuchBucket", "The specified bucket does not exist.", method == "HEAD"
            )
            return
        if not key:
            self._bucket(method, parse_qs(parts.query))
            return
        query = parse_qs(parts.query, keep_blank_values=True)
        if "uploads" in query or "uploadId" in query:
            self._multipart(method, key, query, body)
            return
        if method == "PUT":
            with fake.lock:
                fake.objects[key] = body
            self._reply(200, b"", {"ETag": _etag(body)})
        elif method == "DELETE":
            with fake.lock:
                fake.objects.pop(key, None)
            self._reply(204, b"")
        else:
            with fake.lock:
                content = fake.objects.get(key)
            if content is None:
                self._error(*ERRORS["404"], head=method == "HEAD")
                return
            headers = {
                "ETag": _etag(content),
                "Last-Modified": formatdate(usegmt=True),
                "Content-Type": "application/octet-stream",
            }
            if method == "HEAD":
                self._reply(200, b"", headers, length=len(content))
            else:
                self._send_object(content, headers, found)

    # --- bodies -----------------------------------------------------------------------------
    def _body(self) -> bytes:
        if self.headers.get("Transfer-Encoding", "").lower() == "chunked":
            raw = self._http_chunks()
        else:
            raw = self.rfile.read(int(self.headers.get("Content-Length") or 0))
        encoding = self.headers.get("Content-Encoding", "")
        if "aws-chunked" in encoding or self.headers.get("x-amz-decoded-content-length"):
            return _aws_chunks(raw)
        return raw

    def _http_chunks(self) -> bytes:
        out = b""
        while True:
            size = int(self.rfile.readline().split(b";")[0].strip(), 16)
            if size == 0:
                while self.rfile.readline() not in (b"\r\n", b"\n", b""):
                    pass
                return out
            out += self.rfile.read(size)
            self.rfile.readline()

    def _send_object(self, content: bytes, headers: dict[str, str], found: Fault | None) -> None:
        self.send_response(200)
        for name, value in headers.items():
            self.send_header(name, value)
        self.send_header("Content-Length", str(len(content)))
        self.end_headers()
        half = len(content) // 2
        if found is not None and found.kind in {"slow_body", "drop"}:
            self.wfile.write(content[:half])
            self.wfile.flush()
            if found.kind == "drop":
                self._drop()
                return
            self.server_fake.closing.wait(found.seconds)
            content = content[half:]
        try:
            self.wfile.write(content)
        except OSError:
            self.close_connection = True

    def _multipart(self, method: str, key: str, query: dict[str, list[str]], body: bytes) -> None:
        """s3transfer's path for files above 8 MB: initiate, parts, complete (or abort)."""
        fake = self.server_fake
        xmlns = 'xmlns="http://s3.amazonaws.com/doc/2006-03-01/"'
        if method == "POST" and "uploads" in query:
            upload_id = uuid.uuid4().hex
            with fake.lock:
                fake.uploads[upload_id] = {}
            xml = (
                f'<?xml version="1.0" encoding="UTF-8"?><InitiateMultipartUploadResult {xmlns}>'
                f"<Bucket>{fake.bucket}</Bucket><Key>{escape(key)}</Key>"
                f"<UploadId>{upload_id}</UploadId></InitiateMultipartUploadResult>"
            ).encode()
            self._reply(200, xml, {"Content-Type": "application/xml"})
            return
        upload_id = query["uploadId"][0]
        with fake.lock:
            parts = fake.uploads.get(upload_id)
        if parts is None:
            self._error(404, "NoSuchUpload", "The specified upload does not exist.", False)
        elif method == "PUT":
            parts[int(query["partNumber"][0])] = body
            self._reply(200, b"", {"ETag": _etag(body)})
        elif method == "POST":
            content = b"".join(parts[number] for number in sorted(parts))
            with fake.lock:
                fake.objects[key] = content
                fake.uploads.pop(upload_id, None)
            xml = (
                f'<?xml version="1.0" encoding="UTF-8"?><CompleteMultipartUploadResult {xmlns}>'
                f"<Bucket>{fake.bucket}</Bucket><Key>{escape(key)}</Key>"
                f"<ETag>{escape(_etag(content))}</ETag></CompleteMultipartUploadResult>"
            ).encode()
            self._reply(200, xml, {"Content-Type": "application/xml"})
        else:  # DELETE: abort
            with fake.lock:
                fake.uploads.pop(upload_id, None)
            self._reply(204, b"")

    def _bucket(self, method: str, query: dict[str, list[str]]) -> None:
        if method == "HEAD":
            self._reply(200, b"")
            return
        if method != "GET" or query.get("list-type") != ["2"]:
            self._error(400, "InvalidRequest", "Not supported by the fake.", False)
            return
        prefix = query.get("prefix", [""])[0]
        after = query.get("continuation-token", query.get("start-after", [""]))[0]
        limit = int(query.get("max-keys", ["1000"])[0])
        with self.server_fake.lock:
            keys = sorted(k for k in self.server_fake.objects if k.startswith(prefix) and k > after)
            sizes = {k: len(self.server_fake.objects[k]) for k in keys[:limit]}
        page, truncated = keys[:limit], len(keys) > limit
        now = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%S.000Z")
        contents = "".join(
            f"<Contents><Key>{escape(k)}</Key><LastModified>{now}</LastModified>"
            f"<ETag>&quot;x&quot;</ETag><Size>{sizes[k]}</Size>"
            "<StorageClass>STANDARD</StorageClass></Contents>"
            for k in page
        )
        token = (
            f"<NextContinuationToken>{escape(page[-1])}</NextContinuationToken>"
            if truncated
            else ""
        )
        xml = (
            '<?xml version="1.0" encoding="UTF-8"?>'
            '<ListBucketResult xmlns="http://s3.amazonaws.com/doc/2006-03-01/">'
            f"<Name>{self.server_fake.bucket}</Name><Prefix>{escape(prefix)}</Prefix>"
            f"<KeyCount>{len(page)}</KeyCount><MaxKeys>{limit}</MaxKeys>"
            f"<IsTruncated>{'true' if truncated else 'false'}</IsTruncated>{token}{contents}"
            "</ListBucketResult>"
        ).encode()
        self._reply(200, xml, {"Content-Type": "application/xml"})

    # --- answers ----------------------------------------------------------------------------
    def _reply(
        self,
        status: int,
        body: bytes,
        headers: dict[str, str] | None = None,
        length: int | None = None,
    ) -> None:
        self.send_response(status)
        for name, value in (headers or {}).items():
            self.send_header(name, value)
        self.send_header("Content-Length", str(len(body) if length is None else length))
        self.end_headers()
        if body:
            self.wfile.write(body)

    def _error(self, status: int, code: str, message: str, head: bool) -> None:
        xml = (
            f'<?xml version="1.0" encoding="UTF-8"?><Error><Code>{code}</Code>'
            f"<Message>{escape(message)}</Message><RequestId>fake</RequestId></Error>"
        ).encode()
        self._reply(status, b"" if head else xml, {"Content-Type": "application/xml"})

    def _drop(self) -> None:
        self.close_connection = True
        with contextlib.suppress(OSError):
            self.connection.shutdown(socket.SHUT_RDWR)


def _etag(content: bytes) -> str:
    return f'"{hashlib.md5(content).hexdigest()}"'  # noqa: S324 — S3's ETag, not security


def _aws_chunks(raw: bytes) -> bytes:
    """The payload of an aws-chunked body: `<hex size>[;chunk-signature=...]\\r\\n<data>\\r\\n`
    repeated, a zero-size chunk, then trailers."""
    out, position = b"", 0
    while position < len(raw):
        line_end = raw.index(b"\r\n", position)
        size = int(raw[position:line_end].split(b";")[0], 16)
        if size == 0:
            break
        start = line_end + 2
        out += raw[start : start + size]
        position = start + size + 2
    return out


def closed_port() -> int:
    """A local port nothing listens on (connection refused)."""
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])
