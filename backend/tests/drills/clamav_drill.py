# ruff: noqa: T201
# An operator's drill (prints its result), not application code.
"""The attachment scanner against a real ClamAV daemon (R94; docs/runbooks.md#file-storage).

    docker run -d --name arkray-clamav-test -p 127.0.0.1:53310:3310 clamav/clamav:1.5.4
    python tests/drills/clamav_drill.py --scanner clamd://127.0.0.1:53310 \
        --container arkray-clamav-test --report clamav-drill.json [--e2e]

Drives the application's own clamd client (activities.storage.scan) and checks that it
fails closed:

1. PING and VERSION (the daemon answers; its engine and signature versions are reported);
2. a clean file is clean; the EICAR test file (assembled here at run time: this repository
   never holds it, so antivirus on a developer machine has nothing to quarantine) is FOUND;
3. a file of the application's largest size (ATTACHMENT_MAX_BYTES; 100 MiB by default, the
   database's ceiling) is scanned whole: clean, and with a drill signature's marker in its
   last bytes, FOUND. With `--container` the drill installs that signature (a one-line
   .ndb in the container's database directory, then RELOAD). This is what proves clamd's
   StreamMaxLength and MaxFileSize are at least the application's limit: a smaller
   StreamMaxLength fails every larger file closed, forever; a smaller MaxFileSize makes clamd
   skip the rest of the file and answer OK (fail open) unless AlertExceedsMax is on;
4. a stream over clamd's StreamMaxLength is never clean (`size_limit`, or a dropped
   connection where the operating system discards clamd's last words);
5. a connection dropped mid-stream (a proxy that cuts it), a scanner that never answers,
   nothing listening, and bytes that aren't the uploaded file are never clean;
6. ten scans at once (clamd's MaxThreads is 10 by default), timed;
7. with `--e2e`: upload, scan and download through the API (tests/drills/clamav_e2e.py,
   run by pytest against a test database: DATABASE_URL as for the test suite).

The report holds outcomes, reasons, sizes and timings; never file content. Exits non-zero
at the end if any check failed.
"""

from __future__ import annotations

import argparse
import hashlib
import io
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import IO, Any
from urllib.parse import urlsplit

BACKEND = Path(__file__).resolve().parents[2]
MIB = 1024 * 1024
# The EICAR anti-malware test file, in pieces (see the module docstring).
_EICAR_PARTS = (
    "X5O!P%@AP[4",
    "\\PZX54(P^)7CC)7}$",
    "EICAR-STANDARD-",
    "ANTIVIRUS-TEST-",
    "FILE!$H+H*",
)
# The drill's own signature: an .ndb body-pattern (any file type, any offset) for a marker
# that is random per run, so nothing else ever matches it.
DRILL_SIGNATURE = "Arkray.Drill.Marker"


def docker() -> str:
    found = shutil.which("docker")
    if found is None:
        raise SystemExit("docker isn't on PATH (needed for --container)")
    return found


def eicar() -> bytes:
    return "".join(_EICAR_PARTS).encode("ascii")


def configure(scanner: str, max_bytes: int) -> None:
    sys.path.insert(0, str(BACKEND))
    import django
    from django.conf import settings

    settings.configure(
        ATTACHMENT_SCANNER=scanner,
        ATTACHMENT_MAX_BYTES=max_bytes,
        ATTACHMENT_SPOOL_MEMORY_BYTES=MIB,
        INSTALLED_APPS=[],
        USE_TZ=True,
    )
    django.setup()


def raw(address: tuple[str, int], command: bytes, timeout: float = 10) -> str:
    with socket.create_connection(address, timeout=timeout) as connection:
        connection.sendall(b"z" + command + b"\0")
        reply = b""
        while not reply.endswith(b"\0"):
            part = connection.recv(4096)
            if not part:
                break
            reply += part
    return reply.rstrip(b"\0").decode("utf-8", "replace")


def sized_file(size: int, tail: bytes = b"") -> IO[bytes]:
    """`size` bytes of pseudo-random filler (a repeated block), ending in `tail`."""
    block = hashlib.sha256(b"arkray-drill").digest() * (MIB // 32)
    file = tempfile.TemporaryFile()  # noqa: SIM115 — returned, closed by the caller
    remaining = size - len(tail)
    while remaining > 0:
        piece = block[: min(len(block), remaining)]
        file.write(piece)
        remaining -= len(piece)
    file.write(tail)
    file.seek(0)
    return file


def sha256_of(file: IO[bytes]) -> str:
    file.seek(0)
    digest = hashlib.sha256()
    for chunk in iter(lambda: file.read(MIB), b""):
        digest.update(chunk)
    file.seek(0)
    return digest.hexdigest()


class Drill:
    def __init__(self, scanner: str, container: str | None) -> None:
        from arkray.activities import storage

        self.storage = storage
        parts = urlsplit(scanner)
        self.address = (parts.hostname or "", parts.port or 3310)
        self.container = container
        self.checks: list[dict[str, Any]] = []

    def check(self, name: str, expect: str, run: Callable[[], Any]) -> Any:
        """`expect`: "clean", "found", "no_verdict", or "ok" (run returns a truthy value)."""
        started = time.monotonic()
        outcome: dict[str, Any] = {"check": name, "expect": expect}
        result: Any = None
        try:
            result = run()
        except self.storage.ScannerUnavailable as exc:
            outcome.update(got="no_verdict", reason=exc.reason)
        except Exception as exc:
            outcome.update(got="error", error=f"{type(exc).__name__}: {exc}"[:300])
        else:
            if isinstance(result, self.storage.Verdict):
                outcome["got"] = "clean" if result.clean else "found"
                if not result.clean:
                    outcome["signature"] = result.signature
            else:
                outcome["got"] = "ok" if result else "failed"
                if result not in (True, None):
                    outcome["detail"] = result
        outcome["seconds"] = round(time.monotonic() - started, 3)
        outcome["passed"] = outcome["got"] == expect
        self.checks.append(outcome)
        mark = "PASS" if outcome["passed"] else "FAIL"
        extra = outcome.get("signature") or outcome.get("reason") or outcome.get("error") or ""
        print(f"{mark} {name}: {outcome['got']} {extra} ({outcome['seconds']} s)", flush=True)
        return result

    def scan(self, file: IO[bytes], **kwargs: Any) -> Any:
        try:
            return self.storage.scan(file, **kwargs)
        finally:
            file.close()

    # --- the drill signature --------------------------------------------------------------------
    def install_marker(self, marker: bytes) -> bool:
        if not self.container:
            return False
        line = f"{DRILL_SIGNATURE}:0:*:{marker.hex()}\n"
        subprocess.run(  # noqa: S603 — fixed command
            [
                docker(),
                "exec",
                "-i",
                self.container,
                "sh",
                "-c",
                "cat > /var/lib/clamav/arkray-drill.ndb",
            ],
            input=line.encode(),
            check=True,
            capture_output=True,
            timeout=60,
        )
        if raw(self.address, b"RELOAD") != "RELOADING":
            return False
        deadline = time.monotonic() + 180
        probe = b"drill " + marker + b" probe"
        while time.monotonic() < deadline:  # the reload is asynchronous
            time.sleep(2)
            try:
                if not self.storage.scan(io.BytesIO(probe)).clean:
                    return True
            except self.storage.ScannerUnavailable:
                continue
        return False

    def remove_marker(self) -> None:
        if self.container:
            subprocess.run(  # noqa: S603 — fixed command
                [
                    docker(),
                    "exec",
                    self.container,
                    "rm",
                    "-f",
                    "/var/lib/clamav/arkray-drill.ndb",
                ],
                check=False,
                capture_output=True,
                timeout=60,
            )
            raw(self.address, b"RELOAD")


class Cutter:
    """A TCP proxy to clamd that cuts the connection after `limit` bytes from the client."""

    def __init__(self, target: tuple[str, int], limit: int) -> None:
        self.target, self.limit = target, limit
        self.server = socket.create_server(("127.0.0.1", 0))
        self.url = f"clamd://127.0.0.1:{self.server.getsockname()[1]}"
        threading.Thread(target=self.serve, daemon=True).start()

    def serve(self) -> None:
        try:
            client, _ = self.server.accept()
        except OSError:
            return
        with client, socket.create_connection(self.target) as upstream:
            sent = 0
            while sent < self.limit:
                data = client.recv(65536)
                if not data:
                    break
                upstream.sendall(data)
                sent += len(data)
            client.shutdown(socket.SHUT_RDWR)

    def close(self) -> None:
        self.server.close()


class Silent:
    """Accepts, reads everything, never answers."""

    def __init__(self) -> None:
        self.server = socket.create_server(("127.0.0.1", 0))
        self.url = f"clamd://127.0.0.1:{self.server.getsockname()[1]}"
        self.stop = threading.Event()
        threading.Thread(target=self.serve, daemon=True).start()

    def serve(self) -> None:
        try:
            client, _ = self.server.accept()
        except OSError:
            return
        with client:
            client.settimeout(0.5)
            while not self.stop.is_set():
                try:
                    if not client.recv(65536):
                        return
                except TimeoutError:
                    continue
                except OSError:
                    return

    def close(self) -> None:
        self.stop.set()
        self.server.close()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--scanner", default="clamd://127.0.0.1:53310")
    parser.add_argument("--container", help="the clamd container (installs the drill signature)")
    parser.add_argument("--max-bytes", type=int, default=100 * MIB, help="ATTACHMENT_MAX_BYTES")
    parser.add_argument(
        "--stream-max-length", type=int, default=100 * MIB, help="clamd's StreamMaxLength"
    )
    parser.add_argument("--report", required=True)
    parser.add_argument("--e2e", action="store_true")
    options = parser.parse_args()
    configure(options.scanner, options.max_bytes)
    from django.conf import settings

    drill = Drill(options.scanner, options.container)
    from arkray.activities import storage

    report: dict[str, Any] = {"scanner": options.scanner, "max_bytes": options.max_bytes}

    # 1. the daemon
    drill.check("ping", "ok", lambda: raw(drill.address, b"PING") == "PONG")
    report["version"] = raw(drill.address, b"VERSION")
    print(f"     clamd: {report['version']}")

    # 2. clean and EICAR
    drill.check("clean file", "clean", lambda: drill.scan(io.BytesIO(b"Lab report: fine.\n")))
    drill.check("eicar", "found", lambda: drill.scan(io.BytesIO(eicar())))
    clean_pdf = b"%PDF-1.7\n" + b"0" * 4096 + b"\n%%EOF\n"
    drill.check(
        "clean file, hash checked",
        "clean",
        lambda: drill.scan(
            io.BytesIO(clean_pdf), expected_sha256=hashlib.sha256(clean_pdf).hexdigest()
        ),
    )

    # 3. the application's largest file, scanned whole
    marker = os.urandom(16).hex().encode()
    report["drill_signature"] = drill.install_marker(marker)
    big = options.max_bytes
    drill.check(f"{big} bytes, clean", "clean", lambda: drill.scan(sized_file(big)))
    if report["drill_signature"]:
        drill.check("drill marker, small file", "found", lambda: drill.scan(io.BytesIO(marker)))
        drill.check(
            f"{big} bytes, marker in the last bytes",
            "found",
            lambda: drill.scan(sized_file(big, tail=b" " + marker)),
        )
    else:
        print("SKIP marker checks: no --container, or the signature didn't load")

    # 4. over clamd's StreamMaxLength
    over = options.stream_max_length + 1
    drill.check(
        f"{over} bytes (over StreamMaxLength)", "no_verdict", lambda: drill.scan(sized_file(over))
    )
    drill.check("still answering after that", "ok", lambda: raw(drill.address, b"PING") == "PONG")

    # 5. failures
    cutter = Cutter(drill.address, limit=4 * MIB)
    settings.ATTACHMENT_SCANNER = cutter.url
    drill.check("connection cut mid-stream", "no_verdict", lambda: drill.scan(sized_file(16 * MIB)))
    cutter.close()
    silent = Silent()
    settings.ATTACHMENT_SCANNER = silent.url
    storage.SCAN_DEADLINE_S, deadline = 3.0, storage.SCAN_DEADLINE_S
    drill.check(
        "a scanner that never answers (3 s deadline)",
        "no_verdict",
        lambda: drill.scan(io.BytesIO(b"hello")),
    )
    storage.SCAN_DEADLINE_S = deadline
    silent.close()
    with socket.create_server(("127.0.0.1", 0)) as free:
        port = free.getsockname()[1]
    settings.ATTACHMENT_SCANNER = f"clamd://127.0.0.1:{port}"
    drill.check("nothing listening", "no_verdict", lambda: drill.scan(io.BytesIO(b"hello")))
    settings.ATTACHMENT_SCANNER = options.scanner
    drill.check(
        "bytes that aren't the upload",
        "no_verdict",
        lambda: drill.scan(io.BytesIO(b"replaced"), expected_sha256="0" * 64),
    )

    # 6. ten at once
    def ten() -> dict[str, Any]:
        started = time.monotonic()
        with ThreadPoolExecutor(10) as pool:
            verdicts = list(pool.map(lambda _: drill.scan(sized_file(MIB)), range(10)))
        took = round(time.monotonic() - started, 3)
        return (
            {"all_clean": all(v.clean for v in verdicts), "seconds": took}
            if all(v.clean for v in verdicts)
            else {}
        )

    drill.check("ten 1 MiB scans at once", "ok", ten)

    # 7. through the API
    if options.e2e:
        env = {**os.environ, "ARKRAY_CLAMAV_URL": options.scanner, "PYTHONUTF8": "1"}
        if report["drill_signature"]:
            env["ARKRAY_CLAMAV_MARKER"] = marker.decode()

        def e2e() -> bool:
            result = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "pytest",
                    "tests/drills/clamav_e2e.py",
                    "-q",
                    "--no-cov",
                    "-p",
                    "no:cacheprovider",
                ],
                cwd=BACKEND,
                env=env,
                capture_output=True,
                text=True,
                timeout=900,
                check=False,
            )
            print(result.stdout[-3000:] + result.stderr[-1000:])
            return result.returncode == 0

        drill.check("upload, scan, download through the API", "ok", e2e)

    if report["drill_signature"]:
        drill.remove_marker()
    report["checks"] = drill.checks
    report["passed"] = all(check["passed"] for check in drill.checks)
    Path(options.report).write_text(json.dumps(report, indent=2), encoding="utf-8")
    print("DRILL " + ("PASSED" if report["passed"] else "FAILED"))
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
