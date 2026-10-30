"""Opt-in live contract checks. Queries/answers stay in runtime state, never fixtures."""

from __future__ import annotations

import asyncio
import os
from pathlib import Path

import httpx
import pytest

from mornye_bridge.config import Config
from mornye_bridge.ids import decode_id
from mornye_bridge.server import create_app
from mornye_bridge.service import Service, selected_format
from mornye_bridge.supervisor import Supervisor

pytestmark = pytest.mark.live


async def test_live_bridge() -> None:
    library = os.environ.get("MORNYE_LIVE_LIBRARY")
    query = os.environ.get("MORNYE_LIVE_QUERY")
    audio_query = os.environ.get("MORNYE_LIVE_AUDIO_QUERY")
    paged_query = os.environ.get("MORNYE_LIVE_PAGED_QUERY")
    state = os.environ.get("MORNYE_LIVE_STATE")
    registry = os.environ.get("MORNYE_LIVE_REGISTRY")
    extensions = os.environ.get("MORNYE_LIVE_EXTENSIONS", "").split()
    audio_extension = os.environ.get("MORNYE_LIVE_AUDIO_EXTENSION")
    paged_extension = os.environ.get("MORNYE_LIVE_PAGED_EXTENSION")
    if not all(
        (
            library,
            query,
            audio_query,
            paged_query,
            state,
            registry,
            extensions,
            audio_extension,
            paged_extension,
        )
    ):
        pytest.skip(
            "Supply MORNYE_LIVE_LIBRARY, STATE, REGISTRY, QUERY, AUDIO_QUERY, PAGED_QUERY, "
            "EXTENSIONS, AUDIO_EXTENSION and PAGED_EXTENSION"
        )
    extensions = list(dict.fromkeys([*extensions, audio_extension, paged_extension]))
    config = Config(library=Path(library), state_dir=Path(state), stream_wait=60)
    service = Service(config)
    app = create_app(service)
    async with app.router.lifespan_context(app):
        await service.execute("registry.set", {"url": registry})
        installed = {x["id"] for x in await service.management.installed()}
        for extension in extensions:
            if extension not in installed:
                await service.execute("install", {"extension": extension})
            await service.execute("enable", {"extension": extension})
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://127.0.0.1:4759", timeout=150
        ) as client:
            for extension in extensions:
                base = f"/addons/{service.access_token}/{extension}"
                response = await client.get(base + "/manifest.json")
                assert response.status_code == 200
                response = await client.get(base + "/search", params={"q": query, "limit": 1})
                assert response.status_code == 200, "live search failed (details withheld)"
                result = response.json()
                assert result["tracks"] and result["albums"] and result["artists"]
                album = await client.get(base + "/album/" + result["albums"][0]["id"])
                assert album.status_code == 200 and album.json()["tracks"]
                song = await client.get(base + "/catalog/track/" + result["tracks"][0]["id"])
                assert song.status_code == 200 and song.json()["album"]
                artist = await client.get(base + "/artist/" + result["artists"][0]["id"])
                assert artist.status_code == 200
                cursor = artist.json().get("releasesNext")
                seen = set()
                while cursor:
                    assert cursor not in seen and len(seen) < 50
                    seen.add(cursor)
                    page = await client.get(
                        base + "/catalog/artist/" + result["artists"][0]["id"] + "/releases",
                        params={"cursor": cursor},
                    )
                    assert page.status_code == 200
                    cursor = page.json().get("next")
            # An artist with more than one native page verifies continuation over HTTP.
            base = f"/addons/{service.access_token}/{paged_extension}"
            result = (
                await client.get(base + "/search", params={"q": paged_query, "limit": 1})
            ).json()
            artist_id = result["artists"][0]["id"]
            page = (await client.get(base + "/artist/" + artist_id)).json()
            cursor = page.get("releasesNext")
            assert cursor, "configured paged artist has no second page"
            pages = 1
            seen = set()
            while cursor:
                assert cursor not in seen and pages < 50
                seen.add(cursor)
                page = (
                    await client.get(
                        base + "/catalog/artist/" + artist_id + "/releases",
                        params={"cursor": cursor},
                    )
                ).json()
                cursor = page.get("next")
                pages += 1
            print("Live native catalog continuations reached end; pages:", pages)
            base = f"/addons/{service.access_token}/{audio_extension}"
            result = (
                await client.get(base + "/search", params={"q": audio_query, "limit": 1})
            ).json()
            ident = result["tracks"][0]["id"]
            response = await client.get(base + "/stream/" + ident)
            assert response.status_code == 200, "live audio preparation failed (details withheld)"
            descriptor = response.json()
            audio = await client.get(
                httpx.URL(descriptor["url"]).path, headers={"Range": "bytes=0-31"}
            )
            assert audio.status_code == 206 and len(audio.content) == 32
            assert descriptor["codec"] and descriptor["duration"] > 0
            print("Live audio prepared and served; measured codec:", descriptor["codec"])
            # Remove the prepared variant; cancel the next transfer after byte progress.
            assert service.cache
            extension = await service.extension(audio_extension)
            quality = selected_format(extension)
            entry = service.cache.get(audio_extension, decode_id(ident), quality)
            assert entry
            entry.path.unlink()
            service.cache.db.execute("DELETE FROM files WHERE selector=?", (entry.selector,))
            service.cache.db.commit()
            host = service.worker
            assert isinstance(host, Supervisor)
            while not host.events.empty():
                host.events.get_nowait()
            task = service.begin(extension, decode_id(ident), quality)
            transferred = False
            async with asyncio.timeout(60):
                while not task.done():
                    event = await host.events.get()
                    value = event.get("value", {})
                    if event.get("kind") == 10 and any(
                        x.get("bytes_received", 0) > 0 for x in value.get("items", {}).values()
                    ):
                        transferred = True
                        break
            assert transferred and not task.done(), (
                "native transfer finished before cancellation evidence"
            )
            await service.cancel(audio_extension, ident, quality)
            assert task.cancelled()
            assert not service.jobs and list((config.state_dir / "jobs").iterdir()) == []
            print("Live native transfer cancelled after byte progress; staging cleaned")
            # Find the same song by description, as Shijhon does, and prepare it that way.
            track = result["tracks"][0]
            wanted = {"title": track["title"], "artist": track.get("artist", "")}
            wanted |= {"durationMs": str(track.get("durationMs", 0))}
            if track.get("isrc"):
                wanted["isrc"] = track["isrc"]
            found = await client.get(base + "/resolve", params=wanted)
            assert found.status_code == 200, "live resolve found nothing (details withheld)"
            recording = found.json()["item"]["id"]
            response = await client.get(base + "/stream/" + recording)
            assert response.status_code == 200, "live recording preparation failed"
            print("Live recording resolved and prepared; codec:", response.json()["codec"])
