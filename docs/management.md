## Management page

Open the management listener (port 4760 for Docker/NixOS; port 4759 for the
shared manual default). On first start, **Choose a password** accepts any non-empty
password up to 1024 characters and logs you in. There is no default
password. Until a password is chosen, whoever reaches the page first sets it:
keep the page private (default loopback).

Only an Argon2id hash is stored in the 0600 password file. For a forgotten password,
run `mornye-bridge --config /path/to/config.toml set-password`; it reads a replacement
privately from the terminal and revokes browser sessions. In a source checkout prefix
commands with `uv run`. In Docker use `docker compose -f compose.shijhon.yaml exec
mornye-bridge mornye-bridge --config /etc/mornye-bridge/config.toml set-password`.
For NixOS see [password reset](nixos.md#password-reset).
Existing password files continue working; an old unused setup-code file is ignored.
The CLI admin token is separate from browser and add-on access.

The page provides:

- **Registries:** add/remove HTTPS registry addresses and see their available packages.
  The bridge saves `registries` in `state_dir/settings.toml`. Until that setting is first saved,
  an already selected native registry appears on the page. An explicitly empty list
  stays empty. The pinned host accepts one active registry, so the bridge serializes
  registry selection, enumeration and install/update, then restores the prior native
  selection. Every install/update uses the registry attached to that offer.
- **Extensions:** install, update when offered, enable/disable, remove, and inspect
  status. Ready means the extension loaded and its declared health probes pass, when
  it has probes; it is not proof that a particular song will play. Last native auth
  failures appear as needs login or verification. Health and registry views are cached
  for a minute; Refresh clears the bridge view (native probes have their own cache).
  Each card says whether an offered update installs automatically or waits for the
  next sign-in.
- **Settings:** forms use declared string, boolean, number, select and button settings,
  including quality-specific fields and non-secret defaults. Secret flags, password/OTP
  types and credential-like names are write-only, including unmarked media tokens and
  saved login URLs. Blank keeps a saved secret; Clear explicitly removes its value.
  Required values, numbers and options are checked on the server as well as in HTML.
- **Login and verification:** declared actions can return up to eight password/code/
  select form steps. Inputs go directly to the native action, never into saved settings.
  Native `setting_updates` are merged by the service. Forms and saved retry operations
  expire after three minutes. Browser links open through an authenticated redirect;
  private URLs, codes, grants and action results are not printed in page HTML or logs.
  Paste a returned URL or one-time grant into the masked return field if needed.
- **Sign in:** when any enabled extension needs login or verification, the dashboard
  shows a notice with a **Sign in** button. The sign-in page lists every extension that
  needs it and walks through them one after another. Each step shows that extension's
  own declared login and verification steps, with the same forms, browser links, paste
  box, callback polling, CSRF checks and three-minute expiry as its own page. After each
  one you return to the list, where that extension has been checked again. **Recheck
  all** checks every listed extension again. Each service keeps its own account, so one
  sign-in never signs in another extension.
- **Updates:** with **Install extension updates automatically** on (the default), the
  bridge checks its registries once a day and looks at offered updates one extension at
  a time, never while that extension prepares a file (a waiting update stays waiting
  and is tried again later). Before installing, it checks that the registry still
  offers exactly that version as an update and that the downloaded package is that
  version and newer than the installed one; anything else is dropped and looked at
  again at the next check, so a stale offer never installs another, the same or an
  older version. Other extensions' preparations continue throughout; the native upgrade
  runs without blocking other management operations and pauses only its own extension.
  An update that keeps the extension's sign-in is installed right away, and the
  extension's status is checked again afterwards. An update that would sign the
  extension out waits, and the bridge remembers it across restarts without downloading
  it again. It is installed only once the extension is shown to be signed out: the
  automatic sign-in was tried first and needs you, or the host shows a sign-in
  challenge. An extension with neither a signed session nor a declared sign-in action
  never shows either, so its waiting update is installed only when you choose it. The
  extension's sign-in step offers **Install version … first** for a waiting update, so
  you sign in once. An update that needs a runtime feature or app version the pinned
  host does not provide is shown as not installable on the card and is never
  installed, automatically or by a button. **Check for updates now** runs the check at
  once, and **Update to** on a card still installs an offered update by hand at any
  time. Packages left over from an interrupted run are removed at start. The setting is
  saved as `auto_update` in the state settings file. **Install every extension update
  right away** (`update_all`, off by default) also installs updates that would sign an
  extension out, at the next check; sign in again afterwards on the sign-in page.
- **Signed sessions:** a signed-session extension's card shows until when its session
  is valid and when the bridge last saw it renewed. With **Keep signed sessions alive**
  on (`keep_alive`, the default), the bridge keeps an idle session from lapsing, as
  described below.
- **Use in Shijhon:** copy each enabled extension's private manifest link, see whether
  it supplies audio, catalog data or both, and the recommended reach/time budget.
  Catalog keys/regions are saved per extension to `[catalogs.EXTENSION]` in the state
  settings file, overriding the base TOML values.
  Changing the identity later makes Shijhon treat it as another catalog; the form
  requires acknowledging this before saving.

The pinned host normally asks for a `mornye://` desktop callback. A web page cannot
register or capture that OS scheme, and the host does not promise an arbitrary HTTP
redirect override. Native callbacks already received by the host are drained during
CSRF-protected polling; otherwise use the paste box. The native host checks ownership
and state. A queued callback or an absent challenge alone is not proof of login:
the bridge retries a saved failed management action once, or rechecks native health.
An HTTP playback request already answered cannot be resumed; retry playback in Shijhon.
Successful live account login and gateway verification remain unverified; see
[limits](limits.md).

When a preparation, a song lookup or a check reports that an extension needs login or
verification, the bridge tries once to sign it in again by itself. For an extension
that signs in through the host's signed session, it asks the host to check the saved
session; when that session is no longer usable, the host asks the extension's service
again, which may issue a new session without a browser or answer with a verification
page. For another extension, the bridge runs a declared action whose key, action name
or label says it signs in again (log in, sign in, authenticate, connect, renew, or
refresh a token or session), and never one that signs out, disconnects, clears, resets,
removes, deletes, forgets, cancels or revokes. Anything that asks for a password, a code,
a choice or a browser page is left for the sign-in page, and the bridge does not try
that extension again until a sign-in works. For a signed-session extension, the host
finding no challenge only means that the saved session has not expired yet; unless its
expiry moved later, the try ends as unconfirmed, the notice stays and the extension is
left for the sign-in page. A failed try waits ten minutes before the
next one, doubling up to six hours, and there is at most one try per extension in ten
minutes. Every thirty minutes the bridge also asks the host to check each enabled
signed-session extension's saved session, which the host does locally while the session
is usable, so a lapsed sign-in shows on the dashboard before playback fails.

The host renews a signed session only during a signed request that the extension makes
in the last hour before the session expires. To keep an idle session alive, the bridge
reads the session's expiry from the host's saved record (only whether it holds a session
and when it expires; nothing else is kept, shown or logged) and, about fifty minutes
before expiry, makes one catalog search for a single result through the extension: an
artist search when the extension offers one, else an album search, else its default
search. It never prepares or downloads a song for this. It makes at most one such search
per extension every twenty minutes, skips an extension that is preparing a file (that
download renews the session anyway), and after a failed search waits thirty minutes,
doubling up to six hours. When the expiry moves later, the card shows the renewal. When
two searches inside the same renewal window both succeed yet leave the expiry unchanged,
that extension's searches are not signed requests: the bridge stops searching for that
version. A failed search does not count, and the count starts over whenever the expiry
moves later for any reason, a download included.

For such an extension, **Renew sessions by briefly starting the last song before they
expire** (`renew_by_download`, on by default) renews the session another way. It works
only while keep-alive is on; turning keep-alive off turns it off too. While it is on, the
bridge remembers the last recording each extension delivered (its title, artist, album,
length, ISRC and format, in `state_dir/delivered.json`, written privately); the file is
removed when the setting is turned off or the extension is removed. Once per session,
inside the last hour before it expires, the bridge starts preparing that recording again
through the normal download path and cancels it as soon as the session's expiry moves
later or the audio transfer begins, whichever comes first. The host renews the session
during the extension's first signed request, before the transfer, so the cancel usually
comes before any audio arrives. The partial file is deleted; nothing is kept, served,
cached or counted as a delivery, and Shijhon never sees it as a preparation. It starts
only while the extension prepares no file and a preparation slot is free; a preparation
of that extension that starts meanwhile stops it (it is tried again two minutes later,
up to three times in one session's last hour). If the host does not acknowledge its
cancel, the bridge waits for the calls already running, at most `call_timeout`, before
restarting the worker; a preparation interrupted by that restart is not held back. It
never runs while the extension is changing or needs signing in, or when no recording has been delivered yet
(the bridge never invents or searches for one). A renewal shows on the card as "renewed
by a brief download". When two such attempts end without a later expiry, the bridge
stops for that version and the card says that the session lapses when unused and needs
signing in again; a new version of the extension is tried again. A failed attempt does
not count as such: if it failed before the host started the download, it is tried again
in the same hour after five minutes, doubling; otherwise that session's attempt is used.
Renewal times are kept in `state_dir/sessions.json`. Automatic
sign-in uses only what the extension's own settings and the host already store; form
inputs are never saved.

A missing sign-in stays on the dashboard until one of these shows that the account
works again: the operation that reported it most strongly succeeds (a download counts
most, then an availability check, then a search or another action, then a pending-auth
check or health probe; a weaker later report never replaces a stronger one, so a search
or a pending-auth check cannot clear a download's notice by succeeding later, and a
download reporting the same need takes over from a weaker report); a download explicitly reports
success; a native session grant completes; the extension's sign-in action (or a form
step it started) finishes without another step or browser page; for a signed-session
extension, its saved session expires later than when the sign-in was reported missing,
or the host shows no challenge where its own check had reported one; for another
extension, a challenge seen earlier is gone; or you save the extension's settings or
remove it. A passing health probe, a catalog search and another declared action do not
clear it. Opening a page runs the same pending-auth check as any other and clears the
notice only on the same evidence. Another failure reported meanwhile, such as a timeout or a rate
limit, is kept beside it and never replaces it. The notice is kept in
`state_dir/sign-in.json` (extension IDs, the reporting operation and the session's
expiry at that report, written privately), so it survives a restart.

When the bridge stops, it waits up to `shutdown_timeout` (15 seconds by default) for
preparations in progress, a session renewal and an automatic upgrade together, after the web servers' own
graceful shutdown of the same length, then stops the worker. With the defaults this
fits the 45-second stop allowance of the Docker and NixOS services; raise that allowance
if you raise `shutdown_timeout`. The worker ignores the service manager's stop signal:
the bridge alone stops it, so an upgrade is not cut short by that signal. An upgrade
that has not finished by then is stopped with the worker; the round's offers and
session renewals are saved first, and the offer is looked at again at the next check.
An upgrade stopped midway may leave the extension needing an update or installation
by hand. While an upgrade
runs (up to 300 seconds), another call that times out does not restart the worker.

Sessions are opaque server-side capabilities, last at most eight hours, and are
revoked on logout/password replacement or server restart. Cookies use HttpOnly and
SameSite=Strict, plus Secure when the configured management origin is HTTPS or the
request uses HTTPS (including trusted proxy forwarding). Every browser POST, including
setup, login, logout and polling, requires a session-bound CSRF token; nonmatching
request origins are rejected. Setup and login attempts share limits of five per minute
per client address and 30 per minute overall. Form bodies and session counts are bounded.
HTML uses a strict
same-origin content policy and is never cached. Cross-origin referrers are withheld;
provider redirects explicitly use no-referrer. The bearer-only CLI endpoint does not
accept browser sessions as authorization.

Keep the default loopback bind. For remote operator access, use a TLS reverse proxy
and VPN/firewall as described below, and keep access logs off. Do not expose management
directly to the public internet. The page is single-operator management,
not a multi-user service or an OS sandbox. Page edits never write `config.toml`: it can be read-only, including a Nix store path.
`state_dir/settings.toml` is a writable, atomic 0600 overlay for registries, catalog
identities and the update and keep-alive settings; extension settings remain in native state. Saved overlay keys take
precedence over base TOML (including an explicitly empty registry list). Remove an
overlay key to return to its declarative base value, then restart. Back up this file
with native state. Save failures report that the state directory needs writable
permissions, without disclosing private filesystem/native errors. Restart after
editing either TOML by hand.

