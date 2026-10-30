## Add the links to Shijhon

Use **Use in Shijhon** on the authenticated page, or in another terminal while serving:

```sh
uv run mornye-bridge --private-output /private/new-addon-links.json links
```

Add the desired manifest URLs from that file on Shijhon's Add-ons page. For the default
same-machine configuration select reach **loopback**. When running across containers
or hosts, set `public_url` to an origin Shijhon can reach, bind accordingly and select
reach **private** for a Docker network or private host network. Protect management with loopback/firewalling or a TLS reverse
proxy. Keep access logs off at a proxy too: URL paths contain bearer capabilities.
`public_url` is configured, not reflected from request Host headers.

Set `budget_seconds` to about **30** for each audio add-on from the bridge: preparing
a file often takes longer than Shijhon's default, and the management page shows this
recommendation with each link. A request waits up to `stream_wait` (8 seconds); a file
still preparing answers 503 with `Retry-After: 2` while preparation continues, so
Shijhon can ask again within that time. Use `capacity_group` when extensions share
upstream capacity or authentication, following their documentation.

Each extension has a stable `mornye.EXTENSION` manifest ID. Metadata extensions declare
`search`, `catalog`, `fullcatalog`; audio extensions declare `search`, `stream`,
`availability`, `cached`, `resolve`. Mixed extensions declare both. Shijhon finds a
song at an audio add-on with `resolve`, which asks the extension's own availability
check with the song's ISRC, title, artist and length, as Mornye does; it never
searches it for a song. Songs of an extension's own catalog are asked for by their
track ID; the bridge reads their details and finds them the same way. All declare
`allowDownloads: true`. Audio format is the `format` setting, using exactly the
extension's native option IDs. An explicit native default wins; otherwise choose
the first option whose `kind` is not `spatial` and whose spatial flag is false/absent.
If every option is spatial, use the first option. The effective default is shown next
to each Shijhon link. Explicit spatial format choices remain supported. After an
extension update renames quality IDs, **restart Shijhon** to refresh its manifest
settings; an unknown old format returns 400 rather than silently changing quality. The bridge never invents
lossless quality, enables cross-provider fallback, or transcodes audio to a requested
quality. Descriptors report the **probed actual** codec/container.

For full catalogs, choose the add-on on Shijhon's Catalog page, check its declared
identity, select **Confirm this identity**, save and restart Shijhon. Defaults are the
extension ID reduced to lowercase letters/digits, region `""`, client artwork enabled.
Override under `[catalogs.EXTENSION]` in TOML. A malformed/reserved default key requires
an explicit valid key; it is not silently replaced. Keep identity/region aligned with
the configured extension's actual market and IDs when changing extension settings.
Changing a catalog identity has Shijhon's usual new-catalog consequences.

