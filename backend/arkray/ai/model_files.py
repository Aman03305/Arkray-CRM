"""The embedding model's files: pinned, verified, fetched once.

BAAI/bge-small-en-v1.5 (MIT licence) at a fixed revision; every file is checked against
its SHA-256 before it is used, so a tampered or truncated download is refused. The image
build runs this (`python -m arkray.ai.model_files /opt/models/bge-small-en-v1.5`) and the
runtime only ever reads the files from disk. No Django import: it runs at build time.
"""

from __future__ import annotations

import hashlib
import http.client
import sys
import urllib.error
import urllib.request
from pathlib import Path

MODEL_REPOSITORY = "BAAI/bge-small-en-v1.5"
MODEL_REVISION = "5c38ec7c405ec4b44b94cc5a9bb96e735b38267a"
MODEL_FILES = {
    "model.onnx": (
        "onnx/model.onnx",
        "828e1496d7fabb79cfa4dcd84fa38625c0d3d21da474a00f08db0f559940cf35",
    ),
    "tokenizer.json": (
        "tokenizer.json",
        "d241a60d5e8f04cc1b2b3e9ef7a4921b27bf526d9f6050ab90f9267a1f9e5c66",
    ),
}
DOWNLOAD_TIMEOUT_S = 300


def _digest(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def verify(directory: Path) -> list[str]:
    """The names of files that are missing or don't match their pinned digest."""
    return [
        name
        for name, (_, expected) in MODEL_FILES.items()
        if not (directory / name).is_file() or _digest(directory / name) != expected
    ]


# Phase 10: a slow but working network cut the 133 MB model's connection every minute or
# two; eight requests were needed to get it once. So requests are limited by the progress
# they make, not by their number: give up after IDLE_REQUESTS in a row that add nothing,
# or after FRESH_STARTS downloads from the first byte (a server that ignores Range, or a
# whole file with the wrong digest); MAX_REQUESTS bounds the whole download regardless.
IDLE_REQUESTS = 3
FRESH_STARTS = 3
MAX_REQUESTS = 40


def fetch(directory: Path) -> None:
    """Download whatever is missing or wrong, verifying each file before it is kept."""
    directory.mkdir(parents=True, exist_ok=True)
    directory.chmod(0o755)
    for name in verify(directory):
        _download(directory, name)


def _download(directory: Path, name: str) -> None:
    """One file, resumably: a connection cut mid-file continues with an HTTP Range request
    for the rest instead of starting over. Nothing is kept unless the whole file matches its
    pinned SHA-256; a full-length file that doesn't is started again from the beginning."""
    remote, expected = MODEL_FILES[name]
    url = f"https://huggingface.co/{MODEL_REPOSITORY}/resolve/{MODEL_REVISION}/{remote}"
    partial = directory / f".{name}.partial"
    partial.unlink(missing_ok=True)
    idle = fresh = 0
    try:
        for number in range(1, MAX_REQUESTS + 1):
            offset = partial.stat().st_size if partial.exists() else 0
            headers = {"Range": f"bytes={offset}-"} if offset else {}
            written = 0
            failure: Exception | None = None
            try:
                request = urllib.request.Request(url, headers=headers)
                with urllib.request.urlopen(request, timeout=DOWNLOAD_TIMEOUT_S) as response:  # noqa: S310
                    resumed = offset > 0 and getattr(response, "status", 200) == 206
                    if not resumed:
                        fresh += 1
                    with partial.open("ab" if resumed else "wb") as out:
                        for block in iter(lambda: response.read(1 << 20), b""):
                            out.write(block)
                            written += len(block)
            except urllib.error.HTTPError as exc:
                failure = exc
                if exc.code == 416:  # nothing left to fetch, yet the digest was wrong
                    partial.unlink(missing_ok=True)
                    failure = ValueError(f"{name}: does not match the pinned SHA-256 {expected}.")
            except (OSError, http.client.HTTPException) as exc:  # IncompleteRead: a cut body
                failure = exc
            if failure is None:
                actual = _digest(partial)
                if actual == expected:
                    partial.replace(directory / name)
                    # Readable by the non-root user the containers run as (the build runs
                    # as root, and a temporary file is created owner-only).
                    (directory / name).chmod(0o644)
                    return
                failure = ValueError(
                    f"{name}: SHA-256 {actual} doesn't match the pinned {expected}."
                )
            idle = 0 if written else idle + 1
            if idle >= IDLE_REQUESTS or fresh >= FRESH_STARTS or number == MAX_REQUESTS:
                raise failure
            end = f"{offset + written:,} bytes ({type(failure).__name__})"
            print(f"{name}: request {number} ended at {end}; resuming", file=sys.stderr)  # noqa: T201
    finally:
        partial.unlink(missing_ok=True)


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print("usage: python -m arkray.ai.model_files <directory>", file=sys.stderr)  # noqa: T201
        return 2
    target = Path(argv[1])
    fetch(target)
    print(f"{MODEL_REPOSITORY}@{MODEL_REVISION[:8]} verified in {target}")  # noqa: T201
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
