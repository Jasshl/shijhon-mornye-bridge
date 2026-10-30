# Service and protocol behavior

The internal API is `Service.execute(operation, params, private=False)` plus Catalog
and preparation methods. Browser pages use it directly after session/CSRF checks.
The authenticated CLI transport is
`POST /management`, `Authorization: Bearer <admin.token>`, with JSON
`{"operation":"settings.show","params":{"extension":"example"},"private":false}`.
Operations are CLI names, with `registry.get/set`, `settings.show/set`, and
`pending-auth.show/grant/callback`. `settings.set` takes `values`; action input is
`arguments`; grants use `grant`; callbacks use `url`. Only an authenticated explicit
`private:true` request returns private auth/action results or add-on links. Do not log
requests/responses. Add-on access tokens grant no management access. Mutations serialize;
disable/remove/update cancel that extension's preparation before changing it.

Add-on endpoints live at `/addons/<access.token>/<extension-id>/...`:

| Endpoint | Behavior |
| --- | --- |
| `manifest.json`, `search?q=&limit=1..100` | Native capabilities and manifest filter IDs/default search |
| `album/{id}`, `artist/{id}`, `catalog/track/{id}` | Raw generic native metadata mapped to full-catalog fields |
| `catalog/artist/{id}/releases?cursor=...` | Passes opaque `albums_next` to native `getArtist`; returns albums and next cursor |
| `catalog/isrc/{isrc}` | Catalogs: search the native extension for ISRC text; retain only exact returned ISRC matches |
| `resolve?title=&artist=&durationMs=&isrc=&album=` | Audio: ask the extension's `checkAvailability`; `{"item": ...}` with a recording ID, or 404 |
| `stream/{id}?format=...` | Native or recording ID: completed file descriptor, or 503 with `Retry-After: 2` while preparation continues |
| `availability` | Readiness from cache/in-flight work; remembered misses and failures stay unavailable; `prepare=true` starts a bounded preparation |
| `cached`, `cached-stream/{selector}` | Exact completed variant at any native quality; never prepares or changes the selected variant |
| `/audio/{opaque-token}` | Completed file only, GET/HEAD/ranges; missing/expired files fail |

Installed-extension information is cached for three seconds, invalidated by lifecycle
and settings/auth changes. Native `not_found` codes and generic ABI errors explicitly
saying not found become 404; null metadata also becomes 404. Other malformed native
answers return 502, never caller-error 400. Native result codes take priority over
fallback error-message heuristics; echoed metadata/title fields are not searched.
Descriptors carry one probed container name, bit depth when measurable and native
quality when returned. Readiness title/artist matching folds case/accents and splits
common artist separators, while exact IDs and ISRCs take priority. Completed-file
publication is idempotent. Repository packages are removed after install/update;
startup clears native temp and unfinished job files under exclusive ownership.

Each extension is its own add-on with its own manifest ID. Set `capacity_group` in
Shijhon for add-ons that share upstream capacity or authentication, according to their
extension documentation. Limits and formats come from extensions; the bridge does not
guarantee upstream delivery.

## Finding songs and recording IDs

Audio extensions declare `resolve`, not `isrc`, so Shijhon never searches them for a
song. `resolve` calls the extension's `checkAvailability(isrc, trackName, artistName,
options)` through the generic `invokeExtensionAction` with `arguments_json`, with the
arguments the pinned host's download planner sends: `options` carries `duration_ms`
and a `track` object (`name`, `artists`, `album_name`, `duration_ms`, `isrc`, empty
`id`, and the host's defaults for other fields). The host's per-service track ID
fields are left out; the bridge has none to send. Only `available: true` counts;
`track_id` and `prepared_context` are opaque and not kept, because the download asks
again. `available: false` (or no answer) is read with the rules for native errors
(its `error_type` or `code`, else its `reason`): a login or verification need answers
503 without Retry-After and marks the extension's login status, a rate limit answers
429; neither is remembered. Anything else is absence: 404. Found and absent answers
are remembered per extension and recording: found for 10 minutes, absent for 5
minutes (at most 2,048), and cleared with the miss memory. A recording already in the cache
(same recording ID, or the same ISRC) is found without a native call. Title and artist
are required and at most 500 characters; an ISRC that is not 12 letters or digits is
ignored.

The item echoes the wanted title, artist, `durationMs` (when given) and ISRC under a
recording ID: `.rec.` followed by base-62 text. `.r` is no hex escape, so no native ID
encodes to it. The ID holds ISRC, title, artist, length and album, and fits the
190-character bound; the album is dropped first when it does not fit. A recording still
too long gets a digest ID; its description is kept in the bridge state
(`cache.sqlite`) across restarts, pruned after 30 days unused and beyond 20,000 rows.
A pruned digest ID answers 404 unless its file is cached, and Shijhon looks the song
up again.

`stream/<recording ID>` runs `downloadByStrategy` with `service` and
`download_provider` set to the extension, `source` empty, fallback off, extensions on,
and the recording's ISRC, title, artist, album and length, with no provider track ID.
With an empty source the pinned planner skips metadata enrichment. It runs the
extension's preflight and `checkAvailability`, downloads the found track with its
prepared context and checks the file against the ISRC or title, artist and length
(within 10 s). Shijhon's rules are stricter, so before publishing the bridge checks the
completed file again: the probed length must be within 3 s of the wanted length; an
ISRC the result reports that differs from the wanted one is another recording; with
no wanted or probed length, only the wanted ISRC reported by the result will do. A
mismatch discards the file and is a recording miss (404 `different_recording_length`,
`different_recording_isrc` or `unverified_recording`, remembered). Shijhon sends no
version to `resolve` or `availability`, so there is no wanted version to compare.
Published metadata is the wanted title, artist and album with the probed length, the
ISRC only as the result reports it, and `explicit: true` when the result says so. The
host reports explicit as true or false only, so clean cannot be told from unknown and
is not recorded. Cache entries, availability, cached matching and misses use this
metadata; a cached file with the same reported ISRC from the same extension is served
for any recording ID that carries it.

A track of an extension's own catalog (`stream/<native ID>`, `availability?id=`,
catalog songs) is prepared the same way. The bridge reads its details with `getTrack`
and downloads it as the recording those details describe: its ISRC, title, artist,
length and album, with `source` empty and no track ID. A track without a title or
artist answers 404 `track_lacks_details`. The result is checked as above against the
track's length and ISRC. The file is published under the native ID, with the catalog's
metadata, the probed length and the ISRC as reported. The catalog's explicit or clean
flag is kept only when the result reports the track's own ISRC. The extension finds the
track by its details, so this costs one availability check more than a direct download,
and the match rests on the ISRC and details, not on the exact ID. Files cached earlier
under a native ID stay valid and are served as before.

A native recording miss (`not_found`, `quality_unavailable`, or an explicit
not-playable/no-full-stream reason inside a generic native failure) returns **404**
at `/stream`, so Shijhon can ask the next add-on. Generic extension, network, storage
and provider failures remain 502/503; login/verification and work still preparing are
503. Native failure messages never leave the worker/service boundary as public text.
Recording misses are remembered in memory for five minutes, separately by extension,
track and native format, with at most 2,048 entries. Failed preparations answering 502
(extension, native-answer, download-path and finalization failures) are remembered the
same way, also for five minutes, and answered at once with the same code. Login or
verification (503), rate limits (429), cancellations, busy slots and changing
extensions are not remembered; timeouts have their own backoff below. Both ID and
recording-matching availability requests return `available:false, preparing:false`
for a remembered miss or failure, even with `prepare=true`. Expiry, restart or relevant
settings/auth/lifecycle operations permit another attempt.

`checkAvailability` finds a recording; it is not a readiness shortcut. It does not
promise a complete local file at the selected format. The bridge reports ready only
from its completed cache.

ISRC search (catalogs only) uses the declared track filter when available, otherwise native default
search. It never treats a title/artist match as an ISRC match. Three nonempty ISRC
search answers containing no valid ISRCs suppress further ISRC searches for that
extension for 15 minutes (404 without a native request). The in-memory counter decays,
resets when ISRC-bearing rows return, and clears on relevant settings/auth/lifecycle
changes or restart. Empty searches do not prove missing ISRC support. There is no
provider-specific lookup or guarantee that every extension indexes ISRC text.

Native downloads use a separate progress watchdog: `download_stall_timeout` defaults
to **180 seconds** without forward byte/progress or stage/status change, and
`download_timeout` is an absolute **14,400-second (4-hour)** cap. Kind-10 native deltas
are merged/routed by `item_id`; another transfer's progress and speed-only heartbeats
do not keep a stalled job alive. The ordinary `call_timeout` remains 135 seconds for
short native calls. Watchdog expiry sends C cancellation and `cancelDownload` by item
ID, waits up to `cancel_timeout` (**10 seconds**) for both replies, then removes
staging. A missing reply requires killing the unresponsive worker; normal stall
cancellation preserves every other call. No partial-file resume is promised.

Timeouts are retryable 503 failures, separately backed off by `download_backoff`
(**300 seconds**, bounded 2,048 in-memory entries per failure memory). They are not
recording misses. Availability reports `preparing:true` only while lookup/preparation
is in flight, then false during backoff. A full preparation queue answers 200 with
`available:false, preparing:false`; it never admits unbounded work. Short-call
cancellation returns to its caller immediately and drains the native reply in the
background while retaining its admission slot. Download cancellation waits before
staging cleanup. Shutdown allows `shutdown_timeout` (**15 seconds**) for cancellations,
then kills the worker process group before removing staging; Uvicorn also allows up
to 15 seconds for active HTTP requests, so allow about **30 seconds plus process
teardown** in a service stop timeout (for example `TimeoutStopSec=45s`). Keep any
custom graceful/shutdown values below the service manager's stop timeout.

A `stream` request waits up to `stream_wait` (**8 seconds**) for the file. Unfinished
preparation answers `503 {"error":"preparing"}` with `Retry-After: 2` and continues, so
Shijhon can ask again while the add-on's time lasts. Retry-After is sent only where
asking again soon can help: preparing, busy preparation slots or worker admission,
timeout backoff, changing extensions (2 s) and rate limits. Login/verification and other
failures carry none. Give bridge audio add-ons a `budget_seconds` of about 30 in
Shijhon; the page shows that recommendation with each link.

Preparation slots: each extension prepares at most its manifest's
`capabilities.downloadTransfer.maxConcurrentDownloads` files at once, read as the host
reads it (a number, rounded, bounded to 1-3; 3 when absent). `preparations`
(default **6**, below `admission` = 8) caps all extensions together. A full slot
answers `503 preparation_busy` with `Retry-After: 3`.

Logging: `serve` writes one line per preparation and per resolve to standard error:
extension ID, ID kind (`native` or `recording`), format, phase timings (metadata,
native call, finalize) or the resolve time and source (`native`, `memory`, `cache`),
and the outcome or classified error code. Lines carry no titles, artists, track IDs,
URLs, tokens or native message text.

A track with neither a native album ID nor a non-empty album title is offered as its
own deterministic single release.
Its release ID is `.single.` followed by the canonically encoded track ID; the prefix
cannot collide with a canonically encoded native album ID. `/album/<single-id>` decodes
that track ID and fetches the track directly, including after restart. The release has
kind `single`, title `<track title> - Single`, the track's artists/artwork/date and one
track at disc 1, track 1. The track names that release. The suffix prevents an identically
named owned album from being grouped with it. Tracks with an album title but no ID
keep a title-only album reference and their native disc/track numbers; synthetic single
lookups for these tracks return 404. Tracks with a native album ID keep it; tracks
returned inside an actual album keep that album's ID and native numbering.
Synthetic IDs must fit the same 190-character protocol bound (the `.single.` prefix
leaves 182 characters for the encoded track ID); oversized IDs fail explicitly. Native
download matching still receives the extension's original album title, never the
synthetic single title. A single release supplies an addable identity, not a claim
that the extension can deliver its audio.

Native IDs escape every non-alphanumeric UTF-8 byte (including dots) as `.xx`, so
literal escape-looking IDs cannot collide. IDs/cursors exceeding protocol limits
fail explicitly. No catalog list is deliberately shortened by the bridge; oversized
album/JSON answers fail explicitly rather than claiming a truncated complete album.
The native extension can itself cap searches/releases or return partial data. The
bridge requests the supplied limit and follows/exposes every returned continuation;
it cannot recover data an extension does not expose. Shijhon also has its own protocol
bounds. See [limits](limits.md).

Catalog calls use the pinned host's generic `invokeExtensionAction(arguments_json)`
with `customSearch`, `getTrack`, `getAlbum`, `getArtist`, rather than normalized
`customSearchWithExtension`/`getProviderMetadata` envelopes. This is intentional: the
normalizer invents missing explicit=false and track numbers and drops album metadata.
Raw fields preserve distinctions; only present booleans and positive numbers become
facts. There are no per-extension code paths. This generic raw export contract is
part of the upstream pin.

One worker owns the process-global native runtime. Admission and preparation counts
are bounded. Crashes, malformed result pipes and unanswered deadlines recover through
a fresh worker on the next call, without replaying mutations. Cancellation uses C
call IDs and native item cancellation, drains the reply or kills the worker process
group before staging cleanup. Native stderr and log events are discarded, not retained
as plaintext. Native errors become fixed public codes. Needs-login/verification is
503; API rate limits are 429/Retry-After; audio delivery never returns a rate-limit 429.

Prepared files are probed and atomically moved into a persistent size/age/LRU cache.
Active file responses hold eviction leases; an evicted link can fail before a client
opens it. A startup lock prevents competing owners and allows crash-staging cleanup.
Download requests name this extension as service and download provider, with fallback
disabled.
Finalization supports the Dart MOV-key descriptor aliases and legacy key, candidate
key encodings, container-copy fallbacks and the native AC-4 repair helper. FLAC in MP4
is remuxed when requested. AAC/ALAC in MP4 is copied into a faststart M4A (index
before media), served explicitly as `audio/mp4`; platform MIME guesses such as
`audio/mp4a-latm` are not used for MP4 files. No quality-changing conversion runs.
Cache format generations expire incompatible entries for preparation on demand.
Unknown decryption strategies fail closed. There are no direct extension/CDN URL shortcuts in this version.
