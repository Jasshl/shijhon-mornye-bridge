# Shijhon Mornye extension bridge

A separate bridge that runs [Mornye](https://github.com/mornyeapp/Mornye)'s
operator-built native extension host in a worker process and exposes installed
extensions as audio add-ons, catalogs or both for [Shijhon](https://github.com/Jasshl/shijhon).
Manage extensions, settings and private add-on links on a password-protected page.
The bridge contains no provider implementations; each extension supplies its own
catalog, formats, login and download behavior. No registry is preconfigured.

## Docker next to Shijhon

Start Shijhon's Compose stack, then build and start the bridge from this checkout:

```sh
git clone --branch v0.1.1 --depth 1 https://github.com/Jasshl/shijhon-mornye-bridge.git
cd shijhon-mornye-bridge
docker compose -f compose.shijhon.yaml build
docker compose -f compose.shijhon.yaml up -d
```

The example joins Shijhon's `shijhon_default` network, with API address
`http://mornye-bridge:4759` and management at <http://127.0.0.1:4760>.
Build locally: no image or native binary is published because the operator builds
Mornye's LGPL native library. See [Docker configuration and state](docs/docker.md).

## NixOS

Import the flake's `nixosModules.default` and enable the service:

```nix
services.shijhon-mornye-bridge.enable = true;
```

Management defaults to <http://127.0.0.1:4760>, with a separate system user and
private persistent state.
See [flake installation, configuration and checks](docs/nixos.md).

## By hand

Install Python 3.12+, uv, Git, FFmpeg/ffprobe and the native build prerequisites,
then from the source checkout:

```sh
uv sync --locked
./tools/build-host.sh /absolute/private/build-directory
cp config.example.toml config.toml
# Set library in config.toml to the absolute path printed by build-host.sh.
uv run mornye-bridge check
uv run mornye-bridge serve
```

Open <http://127.0.0.1:4759>. The native build pins Mornye commit
`c90ec6478ad3e808ef22ba9464f8ad05054dde7b` and Rust 1.99.0.
See [manual prerequisites and service operation](docs/manual.md).

## First start

Open the management page and **Choose a password**. Any non-empty password up to
1024 characters is accepted; saving logs you in. Until a password is chosen,
whoever reaches the page first sets it: keep the page private (default loopback).
`mornye-bridge set-password` resets a forgotten password. Add only registry addresses
and extensions you trust.
See [management page details](docs/management.md) and [CLI commands](docs/cli.md).

## Add the links to Shijhon

In **Use in Shijhon**, copy each enabled extension's manifest link to Shijhon's
Add-ons page. Select reach **private** across Docker/private networks, or **loopback**
for processes on the same machine. `public_url` must be reachable from Shijhon;
loopback inside a container means that container itself. Give each audio add-on a
`budget_seconds` of about 30 in Shijhon: preparing a file often takes longer than
Shijhon's default.
For a full catalog, select it on Shijhon's Catalog page, check the declared identity,
select **Confirm this identity**, save and restart Shijhon.
See [connection, identity and time-budget details](docs/shijhon.md).

## Security in brief

Keep management on loopback or behind your private HTTPS reverse proxy with explicit
[trusted proxy addresses](docs/reverse-proxy.md). Use a separate unprivileged user
and dedicated private state. Extensions are code you run; the worker isolates crashes
and is not a security sandbox. Keep tokens in add-on/audio links private and disable
proxy access logs. Back up the master key with native state; never delete it to reset
a password. See [behavior and limits](docs/limits.md), [protocol](docs/protocol.md)
and [development/live checks](docs/development.md).

## License and credits

Copyright © 2026 Jasshl. The bridge is [MIT licensed](LICENSE), independent of and
not affiliated with or endorsed by Mornye. Mornye provides the extension host and
framework; its pinned native workspace declares
[LGPL-3.0-only](https://github.com/mornyeapp/Mornye/blob/c90ec6478ad3e808ef22ba9464f8ad05054dde7b/rust_backend/Cargo.toml).
The operator builds that library; it is never distributed here, in Python packages
or as a published image/binary. Retain upstream source and notices for your build.
Extensions have their own licenses and terms. No registry, provider or extension is
recommended or bundled.
