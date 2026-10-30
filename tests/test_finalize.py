from __future__ import annotations

import base64
import hashlib
import mimetypes
import shutil
import subprocess

import httpx
import pytest
from conftest import FakeWorker

from mornye_bridge.errors import BridgeError
from mornye_bridge.finalize import execute, finalize, probe
from mornye_bridge.server import create_app
from mornye_bridge.service import Service

pytestmark = pytest.mark.skipif(not shutil.which("ffmpeg"), reason="ffmpeg required")


async def test_synthetic_cenc_decryption_and_flac_remux(config, tmp_path) -> None:
    # Invented synthetic sine audio, known test-only key. No provider sample or credential.
    source = tmp_path / "plain.m4a"
    assert await execute(
        [
            config.ffmpeg,
            "-v",
            "error",
            "-f",
            "lavfi",
            "-i",
            "sine=frequency=440:duration=0.2",
            "-c:a",
            "aac",
            str(source),
        ]
    )
    encrypted = tmp_path / "encrypted.mp4"
    key = "11223344556677889900112233445566"
    assert await execute(
        [
            config.ffmpeg,
            "-v",
            "error",
            "-i",
            str(source),
            "-c",
            "copy",
            "-encryption_scheme",
            "cenc-aes-ctr",
            "-encryption_key",
            key,
            "-encryption_kid",
            "00000000000000000000000000000001",
            str(encrypted),
        ]
    )
    output, info = await finalize(
        encrypted,
        {
            "decryption": {
                "strategy": "ffmpeg_mov_key",
                "key": base64.b64encode(bytes.fromhex(key)).decode(),
                "input_format": "mov",
                "output_extension": "m4a",
            }
        },
        config,
        FakeWorker(),
    )
    assert info["codec"] == "aac" and info["format"] == "m4a"
    assert info["container"] == "mov"
    # Verify audio decoding, not merely that a container has stream headers.
    assert await execute([config.ffmpeg, "-v", "error", "-i", str(output), "-f", "null", "-"])
    flac_mp4 = tmp_path / "flac.mp4"
    assert await execute(
        [
            config.ffmpeg,
            "-v",
            "error",
            "-f",
            "lavfi",
            "-i",
            "sine=frequency=220:duration=0.2",
            "-c:a",
            "flac",
            "-strict",
            "-2",
            str(flac_mp4),
        ]
    )
    output, info = await finalize(
        flac_mp4,
        {"requires_container_conversion": True, "quality": "Native lossless"},
        config,
        FakeWorker(),
    )
    assert info["codec"] == "flac" and output.suffix == ".flac"
    assert info["bitDepth"] > 0 and info["quality"] == "Native lossless"
    assert (await probe(output, config))["duration"] > 0


async def test_unsupported_decryption_fails_closed(config, tmp_path) -> None:
    with pytest.raises(BridgeError, match="unsupported_decryption"):
        await finalize(
            tmp_path / "unused",
            {"decryption": {"strategy": "unsupported", "key": "private"}},
            config,
            FakeWorker(),
        )


@pytest.mark.parametrize("codec", ["aac", "alac"])
async def test_mp4_finalization_is_faststart_and_preserves_audio(
    config, tmp_path, codec, monkeypatch
) -> None:
    source = tmp_path / "native.m4a"
    assert await execute(
        [
            config.ffmpeg,
            "-v",
            "error",
            "-f",
            "lavfi",
            "-i",
            "sine=frequency=440:duration=8",
            "-c:a",
            codec,
            str(source),
        ]
    )
    data = source.read_bytes()
    assert data.index(b"mdat") < data.index(b"moov")

    def packets(path):
        # Compare compressed packet bytes, not merely successful decoding.
        raw = subprocess.check_output(
            [
                config.ffmpeg,
                "-v",
                "error",
                "-i",
                str(path),
                "-map",
                "0:a:0",
                "-c:a",
                "copy",
                "-f",
                "data",
                "-",
            ]
        )
        return hashlib.sha256(raw).digest()

    before = packets(source)
    output, info = await finalize(source, {}, config, FakeWorker())
    data = output.read_bytes()
    assert data.index(b"moov") < data.index(b"mdat")
    assert packets(output) == before
    assert info["codec"] == codec and info["format"] == "m4a" and info["container"] == "mov"
    assert 7.9 < info["duration"] < 8.1
    # Reproduce a platform MIME database that labels MP4 as LATM AAC.
    mimetypes.init()
    monkeypatch.setitem(mimetypes.types_map, ".m4a", "audio/mp4a-latm")
    service = Service(config, FakeWorker())
    app = create_app(service)
    async with app.router.lifespan_context(app):
        entry = service.cache.publish("invented", "one", "native", output, {}, info)
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://bridge"
        ) as client:
            url = "/audio/" + entry.selector
            head = await client.head(url)
            assert head.status_code == 200 and head.content == b""
            assert head.headers["content-type"] == "audio/mp4"
            assert head.headers["content-length"] == str(len(data))
            assert head.headers["accept-ranges"] == "bytes"
            for requested, start, end in [
                ("0-1", 0, 1),
                ("7-", 7, len(data) - 1),
                ("-9", len(data) - 9, len(data) - 1),
            ]:
                part = await client.get(url, headers={"Range": "bytes=" + requested})
                assert part.status_code == 206
                assert part.headers["content-type"] == "audio/mp4"
                assert part.headers["content-range"] == f"bytes {start}-{end}/{len(data)}"
                assert part.headers["content-length"] == str(end - start + 1)
                assert part.content == data[start : end + 1]
            full = await client.get(url)
            assert full.content == data and full.headers["content-type"] == "audio/mp4"
            invalid = await client.get(url, headers={"Range": f"bytes={len(data)}-"})
            assert invalid.status_code == 416
            assert invalid.headers["content-range"] == f"bytes */{len(data)}"
