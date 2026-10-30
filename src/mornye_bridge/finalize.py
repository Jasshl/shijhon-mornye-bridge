"""Playable-file steps from the pinned Dart finalizers; every FFmpeg operation copies audio."""

from __future__ import annotations

import asyncio
import base64
import json
import re
from pathlib import Path
from typing import Any

from .config import Config
from .errors import BridgeError
from .supervisor import Worker

ALIASES = {
    "",
    "ffmpeg.mov_key",
    "ffmpeg_mov_key",
    "mov_decryption_key",
    "mp4_decryption_key",
    "ffmpeg.mp4_decryption_key",
}


def key_candidates(key: str) -> list[str]:
    candidates = [key.strip(), re.sub(r"^0x", "", key.strip(), flags=re.I)]
    compact = re.sub(r"[^a-fA-F0-9]", "", candidates[-1])
    if compact and len(compact) % 2 == 0:
        candidates.append(compact)
    try:
        candidates.append(base64.b64decode(re.sub(r"\s", "", candidates[1]), validate=True).hex())
    except ValueError:
        pass
    return list(dict.fromkeys(k for k in candidates if k))


async def execute(argv: list[str], timeout: float = 120) -> bool:
    # Arguments include keys: never print argv, stderr or a subprocess exception.
    process = await asyncio.create_subprocess_exec(
        *argv, stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL
    )
    try:
        return await asyncio.wait_for(process.wait(), timeout) == 0
    finally:
        if process.returncode is None:
            process.kill()
            await process.wait()


async def probe(path: Path, config: Config) -> dict[str, Any]:
    process = await asyncio.create_subprocess_exec(
        config.ffprobe,
        "-v",
        "error",
        "-select_streams",
        "a:0",
        "-show_entries",
        "stream=codec_name,sample_rate,bits_per_raw_sample,bits_per_sample:format=format_name,duration",
        "-of",
        "json",
        str(path),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.DEVNULL,
    )
    try:
        stdout, _ = await asyncio.wait_for(process.communicate(), 30)
        value = json.loads(stdout)
        stream = value["streams"][0]
        if process.returncode != 0 or not stream.get("codec_name"):
            raise ValueError
        return {
            "codec": stream["codec_name"],
            "sampleRate": int(stream.get("sample_rate") or 0),
            "container": value.get("format", {}).get("format_name", "").split(",")[0],
            **(
                {
                    "bitDepth": int(
                        stream.get("bits_per_raw_sample") or stream.get("bits_per_sample") or 0
                    )
                }
                if int(stream.get("bits_per_raw_sample") or stream.get("bits_per_sample") or 0) > 0
                else {}
            ),
            "duration": float(value.get("format", {}).get("duration") or 0),
        }
    except (ValueError, KeyError, IndexError):
        raise BridgeError("audio_probe_failed", 502) from None
    finally:
        if process.returncode is None:
            process.kill()
            await process.wait()


async def finalize(
    path: Path, result: dict[str, Any], config: Config, worker: Worker
) -> tuple[Path, dict[str, Any]]:
    original = path
    descriptor = result.get("decryption")
    if not descriptor and result.get("decryption_key"):
        descriptor = {
            "strategy": "ffmpeg.mov_key",
            "key": result["decryption_key"],
            "input_format": "mov",
            "output_extension": result.get("output_extension"),
        }
    if descriptor:
        if (
            not isinstance(descriptor, dict)
            or descriptor.get("strategy", "").lower() not in ALIASES
        ):
            raise BridgeError("unsupported_decryption_descriptor", 502)
        key = descriptor.get("key", "")
        if not isinstance(key, str) or not key.strip():
            raise BridgeError("missing_decryption_key", 502)
        demuxer = descriptor.get("input_format") or "mov"
        if not re.fullmatch(r"[a-z0-9_]+", demuxer):
            raise BridgeError("invalid_decryption_descriptor", 502)
        preferred = descriptor.get("output_extension") or result.get("output_extension") or ".flac"
        preferred = "." + str(preferred).lstrip(".").lower()
        if not re.fullmatch(r"\.[a-z0-9]{1,12}", preferred):
            raise BridgeError("invalid_decryption_descriptor", 502)
        attempts = list(dict.fromkeys([preferred, ".m4a", ".mp4"]))
        completed = False
        for candidate in key_candidates(key):
            for extension, force_mov in [*((x, False) for x in attempts), (".mp4", True)]:
                output = path.with_name("decrypted" + extension)
                output.unlink(missing_ok=True)
                args = [
                    config.ffmpeg,
                    "-v",
                    "error",
                    "-nostdin",
                    "-y",
                    "-decryption_key",
                    candidate,
                    "-f",
                    demuxer,
                    "-i",
                    str(original),
                    "-map",
                    "0:a:0",
                    "-c",
                    "copy",
                ]
                if force_mov:
                    args += ["-f", "mov"]
                if await execute([*args, str(output)]):
                    # The native repair restores dac4 before ffprobe sees an AC-4 MOV.
                    try:
                        if output.suffix in (".m4a", ".mp4"):
                            await worker.call(
                                "ensureAC4Config",
                                {"file_path": str(output), "source_path": str(original)},
                            )
                        measured = await probe(output, config)
                    except BridgeError:
                        continue
                    # FFmpeg can remux AC-4 even when this build has no AC-4 decoder.
                    if measured["codec"] != "ac4" and not await execute(
                        [
                            config.ffmpeg,
                            "-v",
                            "error",
                            "-xerror",
                            "-nostdin",
                            "-i",
                            str(output),
                            "-map",
                            "0:a:0",
                            "-f",
                            "null",
                            "-",
                        ]
                    ):
                        continue
                    path = output
                    completed = True
                    break
            if completed:
                break
        if not completed:
            raise BridgeError("audio_decryption_failed", 502)
    info = await probe(path, config)
    if result.get("requires_container_conversion") and info["codec"] == "flac":
        with path.open("rb") as stream:
            is_native_flac = stream.read(4) == b"fLaC"
        if not is_native_flac:
            output = path.with_name("remuxed.flac")
            if not await execute(
                [
                    config.ffmpeg,
                    "-v",
                    "error",
                    "-nostdin",
                    "-y",
                    "-i",
                    str(path),
                    "-map",
                    "0:a:0",
                    "-c",
                    "copy",
                    "-f",
                    "flac",
                    str(output),
                ]
            ):
                raise BridgeError("audio_remux_failed", 502)
            path = output
        info = await probe(path, config)
    if info["container"] == "mov" and info["codec"] in ("aac", "alac"):
        # AVFoundation may carry the placeholder's format hint. Put the MP4 index
        # first so it can recognize the actual container before reading media.
        output = path.with_name(path.stem + ".streamable.m4a")
        if not await execute(
            [
                config.ffmpeg,
                "-v",
                "error",
                "-nostdin",
                "-y",
                "-i",
                str(path),
                "-map",
                "0:a:0",
                "-c:a",
                "copy",
                "-movflags",
                "+faststart",
                str(output),
            ]
        ):
            raise BridgeError("audio_remux_failed", 502)
        path = output
        info = await probe(path, config)
    # The extension may label a native payload with another extension. Probe decides the descriptor.
    container = info["container"].split(",")[0]
    suffix = {
        "mov": ".mp4" if info["codec"] == "ac4" else ".m4a",
        "flac": ".flac",
        "mp3": ".mp3",
        "ogg": ".ogg",
        "matroska": ".mka",
        "wav": ".wav",
    }.get(container)
    if suffix and path.suffix != suffix:
        target = path.with_name("final" + suffix)
        path.rename(target)
        path = target
    info["format"] = path.suffix.lstrip(".")
    if isinstance(result.get("quality"), str) and result["quality"]:
        info["quality"] = result["quality"]
    return path, info
