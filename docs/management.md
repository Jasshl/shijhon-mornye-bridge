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
`state_dir/settings.toml` is a writable, atomic 0600 overlay for registries and catalog
identities; extension settings remain in native state. Saved overlay keys take
precedence over base TOML (including an explicitly empty registry list). Remove an
overlay key to return to its declarative base value, then restart. Back up this file
with native state. Save failures report that the state directory needs writable
permissions, without disclosing private filesystem/native errors. Restart after
editing either TOML by hand.

