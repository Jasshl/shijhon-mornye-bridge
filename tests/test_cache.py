from __future__ import annotations

import time

import pytest

from mornye_bridge.cache import Cache
from mornye_bridge.errors import BridgeError


def test_cache_lru_age_and_leases(config, tmp_path) -> None:
    config.initialize()
    config.cache_bytes = 12
    cache = Cache(config)

    def publish(name):
        path = tmp_path / (name + ".wav")
        path.write_bytes(b"123456")
        return cache.publish("invented", name, "native", path, {"title": name}, {"codec": "pcm"})

    first = publish("one")
    second = publish("two")
    cache.acquire(first.selector)
    third = publish("three")
    assert cache.selector(second.selector) is None
    assert cache.selector(first.selector) and cache.selector(third.selector)
    cache.acquire(third.selector)
    with pytest.raises(BridgeError, match="cache_full"):
        publish("four")
    cache.release(first.selector)
    cache.release(third.selector)
    cache.db.execute("UPDATE files SET created=?", (time.time() - config.cache_age - 1,))
    cache.evict()
    assert cache.entries("invented") == []
    cache.close()


def test_restart_preserves_published_and_removes_orphans(config, tmp_path) -> None:
    config.initialize()
    cache = Cache(config)
    path = tmp_path / "invented.wav"
    path.write_bytes(b"audio")
    entry = cache.publish("invented", "one", "native", path, {}, {})
    cache.close()
    (config.state_dir / "audio" / "orphan.partial").write_bytes(b"partial")
    cache = Cache(config)
    assert cache.selector(entry.selector)
    assert not (config.state_dir / "audio" / "orphan.partial").exists()
    cache.close()


def test_pre_faststart_m4a_cache_is_regenerated_once(config, tmp_path) -> None:
    config.initialize()
    cache = Cache(config)
    entries = []
    for suffix in (".m4a", ".flac"):
        path = tmp_path / ("invented" + suffix)
        path.write_bytes(b"audio")
        entries.append(cache.publish("invented", suffix, "native", path, {}, {}))
    cache.db.execute("PRAGMA user_version = 0")  # old finalization generation
    cache.close()
    cache = Cache(config)
    assert cache.selector(entries[0].selector) is None
    assert not entries[0].path.exists()
    assert cache.selector(entries[1].selector) is not None
    path = tmp_path / "faststart.m4a"
    path.write_bytes(b"new-audio")
    fresh = cache.publish("invented", "new", "native", path, {}, {})
    cache.close()
    cache = Cache(config)
    assert cache.selector(fresh.selector) is not None
    cache.close()
