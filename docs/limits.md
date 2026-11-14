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

Automatic updates keep an extension's sign-in only as far as the host keeps it. The
pinned host replaces the extension's code and keeps its settings, storage and saved
signed sessions. It files a signed session by the namespace, base URL, app version and
platform in the extension's manifest, and the rest of that object shapes every signed
request. The bridge reads both manifests (the installed one and the package's root
`manifest.json`) and treats an update as keeping the sign-in only when the whole
signedSession object is exactly the same (or absent in both), storage access stays,
every saved credential-like setting is still declared, and every required runtime
feature and the minimum app version are known to be met by the pinned host. Anything
different, unreadable or unknown counts as signing the extension out. Whether the new code still accepts old tokens
cannot be known in advance; the status is checked again after each update. The host
does not keep the previous version and refuses to downgrade, so there is no automatic
return to it. Removing and reinstalling an older package also removes the extension's
saved settings and sign-in.

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

A signed session is kept in native state and survives a bridge restart. The host
renews it only when the extension makes a signed request during the last hour before
it expires; an extension that is not used in that hour lapses, and its next request
needs a new verification. The keep-alive makes a one-result catalog search in that
hour, which renews the session only for extensions whose catalog searches are signed
requests. For extensions that sign only their downloads, the bridge can renew the session
by briefly starting the last song the extension delivered and cancelling it (setting
`renew_by_download`). The host lets the bridge cancel at the earliest when the session's
expiry has moved later or when its progress report says the transfer has begun; the
cancel reaches the transfer between reads, so some audio may be fetched before it stops.
The service may count the start as a short play on your account. If the host does not
acknowledge a renewal's cancel within `cancel_timeout` while other calls are running,
the bridge waits for those calls, at most `call_timeout`, before restarting the worker,
and a preparation interrupted by such a restart is not held back afterwards. Without a delivered
song, or when such attempts do not renew the session, the extension lapses when unused. The
bridge finds the host's session record by the host's own naming of the pinned version;
another host version may name or shape it differently. Automatic sign-in helps only when the extension's service
issues a new session without a browser or when a declared sign-in action needs no
input. Sign-in tokens that the host's own login helper holds only in memory are lost on
restart unless the extension saves them in its settings or storage.

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
