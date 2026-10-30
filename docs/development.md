## Development checks

```sh
uv run ruff check src tests
uv run ruff format --check src tests
uv run mypy src
uv run pytest -q
sh -n tools/build-host.sh
```

Default tests use invented metadata and synthetic audio, and load no real library.
Opt-in live tests require dedicated disposable state. Choose a registry and extensions
yourself; no registry, extension or query is preconfigured. Supply all of these
environment variables before running `uv run pytest -q -m live tests/test_live.py --tb=short`:

- `MORNYE_LIVE_LIBRARY`: absolute path to the pinned native library.
- `MORNYE_LIVE_STATE`: absolute path to dedicated test state, separate from service state.
- `MORNYE_LIVE_REGISTRY`: registry URL you trust.
- `MORNYE_LIVE_EXTENSIONS`: whitespace-separated extension IDs supporting catalog search.
- `MORNYE_LIVE_QUERY`: query returning tracks, albums and artists for those extensions.
- `MORNYE_LIVE_PAGED_EXTENSION` and `MORNYE_LIVE_PAGED_QUERY`: extension ID and artist
  query returning more than one release page.
- `MORNYE_LIVE_AUDIO_EXTENSION` and `MORNYE_LIVE_AUDIO_QUERY`: extension ID and a query
  returning playable audio with enough transfer time to check cancellation.

The scenario installs/enables the selected extensions, makes live catalog requests,
prepares audio and cancels a transfer after byte progress. The paging and audio
extensions are installed too. Real answers, signed URLs and credentials stay out of
fixtures and ordinary output. Missing inputs skip the test.

Additional packaging checks: `uv lock --check`, `sh -n tools/build-host.sh`,
`docker build -t shijhon-mornye-bridge:local .` and `nix flake check` on Linux.
No registry is needed to check native initialization or the first-start page.
