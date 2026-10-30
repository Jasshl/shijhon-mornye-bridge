# Docker next to Shijhon

Build the image yourself: no image or native binary is published because the
operator builds Mornye's LGPL native library locally.

Start Shijhon using its `packaging/compose.yaml` first. Its default project is
`shijhon`, so its normal network is `shijhon_default`. From this bridge checkout:

```sh
docker compose -f compose.shijhon.yaml build
docker compose -f compose.shijhon.yaml up -d
```

The example joins that existing network with DNS name `mornye-bridge`. If you changed
Shijhon's project/network name, set `SHIJHON_NETWORK` to that network's actual name.
The network must permit internet egress for extensions. No registry or extension is
preconfigured. No Shijhon files, music or credentials are mounted in the bridge.

Open <http://127.0.0.1:4760> and choose a password. Until one is chosen, whoever
reaches the page first sets it. Only the management port is published, on host
loopback. The API listens on `0.0.0.0:4759` inside the container and is reachable
on the joined network at `http://mornye-bridge:4759`. The management listener also
binds `0.0.0.0:4760` inside the container, so other containers on that network can
reach it too; keep the network private. Do not publish management publicly.

The image runs as UID/GID 10001 with private state in the `bridge-state` named
volume. Docker Desktop users should keep the named volume for database safety.
The example drops capabilities, uses a read-only root and gives temporary work a
tmpfs. Native libraries are loaded only by the worker. Allow 45 seconds to stop.

To customize, copy [packaging/docker.toml](../packaging/docker.toml) to `config.toml`,
edit it and uncomment the read-only file mount in [the Compose example](../compose.shijhon.yaml).
Keep `/state` and the native library path, separate API/management listeners and
IPv4 loopback access inside the container for CLI reuse. The config file is TOML;
there is no environment-to-settings translation. Compose's `SHIJHON_NETWORK`
only selects the Docker network. For HTTPS management use a private
[reverse proxy](reverse-proxy.md) and explicit trusted proxy addresses.

```sh
docker compose -f compose.shijhon.yaml exec mornye-bridge \
  mornye-bridge --config /etc/mornye-bridge/config.toml check
docker compose -f compose.shijhon.yaml logs --tail 20
# Stop containers without deleting persistent state:
docker compose -f compose.shijhon.yaml down
```

The healthcheck requests the management login page. `check` additionally initializes
and enumerates through the pinned native host. The multi-stage build uses Rust
1.99.0 and libclang, invokes the same pinned build helper as manual installation,
and installs the locked Python bridge with uv. FFmpeg/ffprobe are included.
Keep the pinned source and upstream notices for your local native build.
Builds have been checked on Linux arm64 through Docker Desktop; other image
architectures require their own verification.

Copy links from **Use in Shijhon** to Shijhon's Add-ons page with reach **private**.
For a full catalog, confirm its identity on Shijhon's Catalog page, save and restart.
See [connection details](shijhon.md).
