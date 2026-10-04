"""The embedding model's files: verified before use, and readable by the runtime user (the
image build fetches them as root; the containers run as a non-root user)."""

from __future__ import annotations

import hashlib
import io
import os
import stat
import urllib.error
from pathlib import Path
from unittest import mock

import pytest

from arkray.ai import model_files

CONTENT = b"fake model bytes"


@pytest.fixture
def one_file(monkeypatch):
    digest = hashlib.sha256(CONTENT).hexdigest()
    monkeypatch.setattr(model_files, "MODEL_FILES", {"model.onnx": ("onnx/model.onnx", digest)})


def fake_download(content: bytes):
    response = mock.MagicMock()
    response.__enter__.return_value = io.BytesIO(content)
    return mock.patch("arkray.ai.model_files.urllib.request.urlopen", return_value=response)


def test_fetched_files_are_verified_and_world_readable(tmp_path: Path, one_file):
    with fake_download(CONTENT):
        model_files.fetch(tmp_path)
    fetched = tmp_path / "model.onnx"
    assert fetched.read_bytes() == CONTENT
    assert model_files.verify(tmp_path) == []
    if os.name == "posix":  # Windows has no POSIX permission bits to check
        assert stat.S_IMODE(fetched.stat().st_mode) == 0o644


def test_a_tampered_download_is_refused_and_nothing_is_kept(tmp_path: Path, one_file):
    with fake_download(b"something else"), pytest.raises(ValueError, match="SHA-256"):
        model_files.fetch(tmp_path)
    assert not (tmp_path / "model.onnx").exists()
    assert list(tmp_path.iterdir()) == []


def test_the_permission_bits_are_set_explicitly():
    """The chmod is in the code path (the posix check above runs in the Linux image/CI)."""
    source = Path(model_files.__file__).read_text(encoding="utf-8")
    assert ".chmod(0o644)" in source


def test_a_corrupt_download_is_retried_and_the_good_one_kept(tmp_path: Path, one_file):
    """Phase 10: on a flaky network the 133 MB model arrived short (a different digest
    each time); each bad copy is refused and the download tried again, at most 3 times."""
    responses = iter([b"cut sh", b"also wrong", CONTENT])

    def opened(*_args, **_kwargs):
        response = mock.MagicMock()
        response.__enter__.return_value = io.BytesIO(next(responses))
        return response

    with mock.patch("arkray.ai.model_files.urllib.request.urlopen", side_effect=opened):
        model_files.fetch(tmp_path)
    assert (tmp_path / "model.onnx").read_bytes() == CONTENT
    assert [p.name for p in tmp_path.iterdir()] == ["model.onnx"]  # no partial files left


def test_an_interrupted_download_resumes_where_it_stopped(tmp_path: Path, one_file):
    """Phase 10: the connection cut the model mid-file; the next request asks for the rest
    (HTTP Range) and appends it, instead of starting over."""
    seen_ranges: list[str | None] = []

    def opened(request, *_args, **_kwargs):
        seen_ranges.append(request.get_header("Range"))
        response = mock.MagicMock()
        if len(seen_ranges) == 1:
            response.status, body = 200, CONTENT[:5]  # cut short
        else:
            response.status, body = 206, CONTENT[5:]
        response.__enter__.return_value = response
        response.read.side_effect = io.BytesIO(body).read
        return response

    with mock.patch("arkray.ai.model_files.urllib.request.urlopen", side_effect=opened):
        model_files.fetch(tmp_path)
    assert seen_ranges == [None, "bytes=5-"]
    assert (tmp_path / "model.onnx").read_bytes() == CONTENT
    assert [p.name for p in tmp_path.iterdir()] == ["model.onnx"]


def served(status: int, body: bytes):
    response = mock.MagicMock()
    response.status = status
    response.__enter__.return_value = response
    response.read.side_effect = io.BytesIO(body).read
    return response


def test_a_download_making_progress_is_never_cut_off_by_a_request_count(tmp_path: Path, one_file):
    """Phase 10: the image build needed 8 requests for the model (each connection cut after
    a minute or two); one byte per request here, twice that many, still succeeds."""
    ranges: list[str | None] = []

    def opened(request, *_args, **_kwargs):
        ranges.append(request.get_header("Range"))
        offset = len(ranges) - 1
        return served(206 if offset else 200, CONTENT[offset : offset + 1])

    with mock.patch("arkray.ai.model_files.urllib.request.urlopen", side_effect=opened):
        model_files.fetch(tmp_path)
    assert len(ranges) == len(CONTENT) == 16
    assert (tmp_path / "model.onnx").read_bytes() == CONTENT


def test_requests_that_add_nothing_give_up(tmp_path: Path, one_file):
    calls = iter([served(200, CONTENT[:5])])

    def opened(*_args, **_kwargs):
        try:
            return next(calls)
        except StopIteration:
            raise ConnectionResetError("cut") from None

    with (
        mock.patch("arkray.ai.model_files.urllib.request.urlopen", side_effect=opened) as urlopen,
        pytest.raises(ConnectionResetError),
    ):
        model_files.fetch(tmp_path)
    assert urlopen.call_count == 1 + model_files.IDLE_REQUESTS
    assert list(tmp_path.iterdir()) == []


def test_a_server_ignoring_range_gets_a_bounded_number_of_whole_downloads(tmp_path: Path, one_file):
    with (
        mock.patch(
            "arkray.ai.model_files.urllib.request.urlopen",
            side_effect=lambda *_a, **_k: served(200, CONTENT[:5]),
        ) as urlopen,
        pytest.raises(ValueError, match="SHA-256"),
    ):
        model_files.fetch(tmp_path)
    assert urlopen.call_count == model_files.FRESH_STARTS
    assert list(tmp_path.iterdir()) == []


def test_a_whole_file_with_the_wrong_digest_starts_again_from_the_first_byte(
    tmp_path: Path, one_file
):
    ranges: list[str | None] = []

    def opened(request, *_args, **_kwargs):
        ranges.append(request.get_header("Range"))
        if len(ranges) == 1:
            return served(200, b"x" * len(CONTENT))  # complete, but corrupt
        if len(ranges) == 2:
            raise urllib.error.HTTPError(request.full_url, 416, "Range Not Satisfiable", {}, None)
        return served(200, CONTENT)

    with mock.patch("arkray.ai.model_files.urllib.request.urlopen", side_effect=opened):
        model_files.fetch(tmp_path)
    assert ranges == [None, "bytes=16-", None]
    assert (tmp_path / "model.onnx").read_bytes() == CONTENT


def test_a_chunked_body_cut_mid_way_resumes_too(tmp_path: Path, one_file):
    """Phase 10 review: http.client.IncompleteRead is not an OSError and escaped on the
    first request instead of resuming."""
    import http.client

    calls: list[str | None] = []

    def opened(request, *_args, **_kwargs):
        calls.append(request.get_header("Range"))
        if len(calls) == 1:
            response = served(200, b"")
            response.read.side_effect = http.client.IncompleteRead(CONTENT[:3], 13)
            return response
        return served(200, CONTENT)

    with mock.patch("arkray.ai.model_files.urllib.request.urlopen", side_effect=opened):
        model_files.fetch(tmp_path)
    assert len(calls) == 2
    assert (tmp_path / "model.onnx").read_bytes() == CONTENT
