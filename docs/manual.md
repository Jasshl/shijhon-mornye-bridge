# Manual installation

- Python 3.12+, uv, Git, FFmpeg and ffprobe on PATH (or absolute configured paths).
- A library built from Mornye commit
  `c90ec6478ad3e808ef22ba9464f8ad05054dde7b`, app version `5.2.6`.
  There is no upstream C ABI version negotiation. Updating this pin requires checking
  the native calls, extension contracts and Dart finalization again.
- macOS: Xcode command-line tools, including clang/libclang.
- Linux/NixOS: Rust 1.99.0, a C compiler/linker, libclang shared libraries for bindgen,
  CA certificates and FFmpeg at runtime. On NixOS set `LIBCLANG_PATH` to the libclang
  library directory; use a shell/derivation with stdenv and llvmPackages.libclang and
  a pinned Rust toolchain. Rustup binaries need nix-ld on NixOS, or use a Nix shell
  supplying cargo/rustc **1.99.0**, stdenv and libclang directly. The complete Rust
  workspace, Cargo.lock and its vendored
  patch directories are needed. The bridge runs on NixOS; see [NixOS installation](nixos.md).

## Build and install

Install the bridge from its release tag, then build the pinned host from upstream
source in a separate private directory:

```sh
git clone --branch v0.1.1 --depth 1 https://github.com/Jasshl/shijhon-mornye-bridge.git
cd shijhon-mornye-bridge
uv sync --locked
./tools/build-host.sh /absolute/private/build-directory
```

The script uses cargo/rustc **1.99.0** already on PATH when both match the pin.
Otherwise it installs that toolchain in the build directory's `rustup` and `cargo`
directories without changing shell profiles. It fetches the exact source commit,
runs `cargo --locked`, and prints its library path. It executes upstream Cargo build
scripts;
inspect upstream source and run builds under an appropriate account. It never embeds
or distributes the library. The pinned native workspace declares
[LGPL-3.0-only](https://github.com/mornyeapp/Mornye/blob/c90ec6478ad3e808ef22ba9464f8ad05054dde7b/rust_backend/Cargo.toml);
retain the pinned source and upstream license notices for your build. Extension
packages have their own licenses and terms.

## Run

```sh
cp config.example.toml config.toml
# Set library to the absolute path printed by build-host.sh.
uv run mornye-bridge check
uv run mornye-bridge serve
```

Open <http://127.0.0.1:4759> and choose a password. No registry is preconfigured.
Paths in TOML are relative to that file; put `--config /path/to/config.toml` before
a subcommand. See [management](management.md) and [reverse proxy](reverse-proxy.md).
Run under a separate unprivileged account with a dedicated private state directory.
The native worker is crash isolation, not a security sandbox.

Back up `master.key`, native extension state and `settings.toml` together. Never
delete the master key to reset a password: encrypted extension state depends on it.
`master.key`, `access.token`, `admin.token` and password files are private (0600).
Keep config, native builds, packages, state and audio out of Git.

When building a service wrapper, allow at least 45 seconds for shutdown: HTTP drain
and native cancellation have separate grace periods. CLI reset is `set-password`;
[other commands](cli.md) can reuse the running owner through loopback.
