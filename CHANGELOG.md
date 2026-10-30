# Changelog

## 0.1.0

- Expose installed Mornye extensions as Shijhon add-ons for audio, catalog data or
  both, using an operator-built native host in a separate worker process. Catalog
  identity is confirmed in Shijhon.
- Find songs with each extension's own availability check, as Mornye does, and check a
  prepared file's length and ISRC against the wanted song before serving it.
- Prepare and cache complete audio files with range requests, cancellation, worker
  recovery and each extension's declared download limit. A file still preparing
  answers 503 with `Retry-After`; missing songs and failed preparations are remembered
  for five minutes.
- Manage registries, extensions, settings, login and add-on links on a
  password-protected page or through the CLI. Choose the first password on the
  first-start page, reset it and revoke sessions with the CLI.
- Build a local Docker image with a Compose example next to Shijhon, or install with
  the Nix flake and isolated NixOS service module.
- Configure a management public origin and explicit trusted proxy addresses for
  private HTTPS access, Secure cookies and origin checks.
- Log one line per preparation and per song lookup, with timings and error codes only.

Requires the pinned native host, Python 3.12+ and FFmpeg/ffprobe. Give audio add-ons a
`budget_seconds` of about 30 in Shijhon. Results, formats and authentication depend on
each extension. See [limits](https://github.com/Jasshl/shijhon-mornye-bridge/blob/v0.1.0/docs/limits.md).
