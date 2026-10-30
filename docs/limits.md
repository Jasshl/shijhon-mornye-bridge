# Limits

## Extensions and catalogs

Installed extensions supply metadata, formats, account flows, matching and downloads.
A loaded extension or passing health probe does not establish that a recording plays.
Search and discography completeness depend on what the extension returns. The bridge
passes limits and returned continuations through; it cannot recover omitted pages,
album identities, explicit flags or other facts. Oversized answers fail explicitly.
Catalog ISRC matching searches for ISRC text and accepts exact returned ISRCs only;
extensions that do not index that text cannot resolve it this way. Audio add-ons find a
song with the extension's own availability check; whether it finds the song depends on
the extension.

A track with no album ID or title is exposed as a deterministic single. A title-only
album reference still lacks the stable album identity needed for some Shijhon imports.
Confirm catalog identity and region against the extension's actual IDs and market.
Changing that identity creates a new catalog scope in Shijhon.

## Login and verification

The management page supports declared login forms, links, manual URL/grant returns and
native callback polling. The pinned host normally uses a `mornye://` desktop callback;
a browser page cannot register that OS scheme. The native host validates callback
ownership and state. A submitted return or disappearing challenge is not proof of
success: recheck health or retry the failed action. Retry playback in Shijhon after
login; an already answered playback request cannot be resumed.

Gateway-backed extensions may require verification in an external browser, shared
capacity settings or account-specific steps. These depend on the extension and native
host. Successful live gateway verification, login, token exchange and refresh are
unverified. The bridge adds no gateway-specific verification or callback bypass.

## Audio and platforms

Audio is prepared as a complete local file before delivery, then served with byte
ranges. Slow preparation may first answer 503 with Retry-After while work continues.
There is no partial download resume or direct extension/CDN URL delivery. Give the
add-ons a `budget_seconds` of about 30 in Shijhon. Failed preparations are remembered
for five minutes; change a setting or log in again to retry sooner.

The bridge uses the extension's native format IDs and probes the actual result. It
does not turn one quality into another. FFmpeg finalization copies audio: supported
MOV-key decryption descriptors, FLAC container conversion and faststart AAC/ALAC M4A
are handled; unknown decryption strategies fail closed. Other formats depend on the
native result, FFmpeg build and playback client. The native AC-4 repair helper is
called when needed, but live encrypted, lossless and AC-4 delivery are unverified.
Synthetic decryption/remux tests do not establish support for every extension.

The C ABI is pinned and has no version negotiation. Do not substitute another host
version without validating its native contracts. macOS and NixOS have runtime
verification. Docker Linux arm64 initialization and Linux x86_64 Nix package/module
checks are covered; other architecture combinations and a NixOS service VM remain
unverified. Worker isolation does not provide an
OS sandbox, and cache bounds are not a staging disk quota. Security penetration,
power-loss and multi-user load testing have not been performed.
