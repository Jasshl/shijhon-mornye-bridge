# NixOS

The bridge runs on NixOS. This flake provides the bridge with an operator-built
pinned native host and a reusable service module. It builds locally; no native
binary or image is published.

Add the release to your system flake's inputs and import its module:

```nix
inputs.mornye-bridge.url = "github:Jasshl/shijhon-mornye-bridge/v0.1.1";

# In a NixOS module with inputs passed via specialArgs:
imports = [ inputs.mornye-bridge.nixosModules.default ];
services.shijhon-mornye-bridge = {
  enable = true;
  # Defaults: private /var/lib/shijhon-mornye-bridge, API 4759, management 4760.
  settings = {
    host = "127.0.0.1";
    public_url = "http://127.0.0.1:4759";
    management_host = "127.0.0.1";
    management_port = 4760;
  };
  openFirewall = false;
};
```

Build and activate using your normal NixOS workflow. Open
<http://127.0.0.1:4760> on the service machine, or use an SSH tunnel/private HTTPS
reverse proxy, and choose a password. Until then, whoever reaches the page first
sets it. No registry is preconfigured. The firewall option opens only the API port;
management access is your private network/proxy decision. A container on another
network needs an API bind and `public_url` it can reach, plus reach **private** in
Shijhon. Configure `management_public_url` and `management_trusted_proxies` for
[HTTPS proxy access](reverse-proxy.md). No proxy is trusted by default.

`settings` generates a read-only TOML file in the Nix store. Do not put passwords,
tokens or extension credentials there. The module supplies library/FFmpeg paths and
maps `stateDirectory` to `/data` inside a minimal service root. Page changes write
only the private state overlay. The service has its own system user, read-only store,
private temporary directory, no capabilities and systemd filesystem/kernel hardening.
It sees the Nix store and resolver files, not host homes or music/database directories.
Extensions are code you run; choose publishers you trust. Back up the complete
state directory including its master key.

## Password reset

First start needs no shell helper. For a forgotten password, run the retained CLI
inside the service's mount/root namespace as the service user, using the generated
config path shown by `systemctl show -p ExecStart shijhon-mornye-bridge`:

```sh
pid=$(systemctl show -p MainPID --value shijhon-mornye-bridge)
uid=$(id -u shijhon-mornye-bridge)
gid=$(id -g shijhon-mornye-bridge)
# Substitute the absolute package/config store paths from ExecStart:
sudo nsenter --target "$pid" --mount --pid --root --wdns=/data \
  --setgid "$gid" --setuid "$uid" -- \
  /nix/store/PACKAGE/bin/mornye-bridge --config /nix/store/CONFIG set-password
```

Do this in a private interactive terminal; no password is passed on the command
line. Existing sessions are revoked. Never remove the master key to reset login.

## Build inputs and checks

`nix build` builds the package; its native library is `result/lib/libmornye_desktop.so`.
`nix build .#nativeHost` builds only the host. `nix flake check` builds the package
and checks the module's default generated configuration and isolation defaults.
The module can also be evaluated as part of a normal NixOS test configuration.
The flake locks NixOS 26.05 nixpkgs snapshot `26f4107e47e3ad0ffd0db220469ba83b6f6d96e8`,
with locked uv2nix/pyproject tooling; it does not follow or update the host's nixpkgs.
The native derivation pins Mornye commit `c90ec6478ad3e808ef22ba9464f8ad05054dde7b`,
Cargo dependencies and a checksum-pinned Rust overlay supplying Rust 1.99.0/libclang.
The complete upstream workspace and vendored patches remain available during build.
Linux x86_64 package/build/module evaluation is checked; the exported aarch64-linux
Nix output still needs a native Nix build. No KVM service VM has been verified.
