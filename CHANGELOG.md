# Changelog

## 0.2.0

- Sign in to every extension that needs it from one page. The dashboard shows a notice
  when any extension needs login or verification, and the sign-in page walks through
  each extension's own steps in turn, checking each one again afterwards.
- Try once to sign an extension in again by itself when it reports a missing sign-in:
  a signed-session extension's service may issue a new session without a browser, and
  a declared sign-in action that needs no input runs automatically. Failed tries back
  off from ten minutes up to six hours. Saved signed sessions are also checked every
  thirty minutes, so a lapsed sign-in shows before playback fails.
- Keep a missing sign-in on the dashboard until the account is shown to work again, and
  across restarts: the reporting operation succeeds, a download reports success, a
  session grant or the sign-in action completes, or a signed session expires later than
  at the report. A passing health probe, a catalog search, another action, another
  failure in between or a page view no longer hides it.
- Install extension updates automatically, once a day and one extension at a time,
  when they keep the extension's sign-in: the same signedSession object, storage access,
  saved credential settings, and host requirements known to be met. An update that
  would sign an extension out waits, without being downloaded again, until the
  automatic sign-in has been tried and the extension is shown to be signed out; its
  sign-in step offers to install it first. A stale offer is checked against the registry
  and the package before installing and never installs another, the same or an older
  version; an update the pinned host cannot load is shown and never installed. Nothing
  is downloaded while the extension prepares a file, other management continues during
  an upgrade, shutdown waits for it within the stop allowance, and leftover packages
  are removed at start. The dashboard shows which updates wait; the setting is on by default. A second
  setting, off by default, installs every update right away, including those.
- Keep idle signed sessions alive: about fifty minutes before a session expires, the
  bridge makes one single-result catalog search through the extension, which renews the
  session when that extension signs its searches. Extensions that sign only downloads
  are renewed instead by briefly starting the last song the extension delivered, once
  in the last hour before the session expires and while that extension prepares
  nothing, cancelled as soon as the session is renewed or the transfer begins; the
  audio is never kept or served, though some may be fetched and the service may count
  it as a short play (setting `renew_by_download`, on by default, works with
  keep-alive on). Each card shows until when its session is valid and when and how it
  was last renewed. The keep-alive setting is on by default.
- Answer `503 {"error": "needs_sign_in", "url": "<management origin>/sign-in"}` on the
  add-on routes and audio addresses when an extension needs signing in, as Shijhon's
  add-on protocol asks (`url` only when `management_public_url` is configured). This
  replaces `needs_login_or_verification` there.
- When an extension gives an item's artists as one display credit with a single artist
  ID, answer that credit as the item's `artist` and the ID as an artist reference without
  a name. The credit may join several artists ("A, B"), and it is no longer presented as
  one artist of that name.
- When an extension joins the IDs of several artists with commas beside a credit that
  joins their names with ", ", each artist is answered with its own ID and name, when
  there are as many names as IDs.

## 0.1.1

- Accept a prepared file whose reported ISRC differs from the wanted song's when its
  length matches, as Shijhon's matching does: the same recording is often released
  under several ISRCs. Such files are no longer discarded as `different_recording_isrc`.

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
