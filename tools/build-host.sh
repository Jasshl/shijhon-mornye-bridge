#!/bin/sh
# Build the operator-owned LGPL library in an isolated source/toolchain tree.
set -eu
umask 077
commit=c90ec6478ad3e808ef22ba9464f8ad05054dde7b
build_root=${1:-"$PWD/build/host"}
mkdir -p "$build_root"
build_root=$(cd "$build_root" && pwd)
export CARGO_TARGET_DIR="$build_root/target"
# Nix and other managed toolchains can supply the exact pinned tools directly.
if command -v rustc >/dev/null 2>&1 && command -v cargo >/dev/null 2>&1 &&
   [ "$(rustc --version | cut -d ' ' -f 2)" = "1.99.0" ] &&
   [ "$(cargo --version | cut -d ' ' -f 2)" = "1.99.0" ]; then
    pinned_on_path=yes
else
    pinned_on_path=no
    export RUSTUP_HOME="$build_root/rustup"
    export CARGO_HOME="$build_root/cargo"
    export PATH="$CARGO_HOME/bin:$PATH"
    if [ ! -x "$CARGO_HOME/bin/rustup" ]; then
        curl --fail --proto '=https' --tlsv1.2 --location https://sh.rustup.rs -o "$build_root/rustup-init.sh"
        sh "$build_root/rustup-init.sh" -y --no-modify-path --profile minimal --default-toolchain 1.99.0
    fi
    rustup toolchain install 1.99.0 --profile minimal >&2
fi
source_dir="$build_root/source"
if [ ! -d "$source_dir/.git" ]; then
    mkdir -p "$source_dir"
    git -C "$source_dir" init >&2
fi
# Fetch by URL: no remote is created in the source checkout or the bridge repository.
git -C "$source_dir" fetch --depth 1 https://github.com/mornyeapp/Mornye.git "$commit" >&2
git -C "$source_dir" checkout --detach "$commit" >&2
[ "$(git -C "$source_dir" rev-parse HEAD)" = "$commit" ]
if [ "$pinned_on_path" = yes ]; then
    cargo build --release --locked -p mornye-desktop --manifest-path "$source_dir/rust_backend/Cargo.toml" >&2
else
    cargo +1.99.0 build --release --locked -p mornye-desktop --manifest-path "$source_dir/rust_backend/Cargo.toml" >&2
fi
case "$(uname -s)" in
    Darwin) library="$CARGO_TARGET_DIR/release/libmornye_desktop.dylib" ;;
    Linux) library="$CARGO_TARGET_DIR/release/libmornye_desktop.so" ;;
    *) echo 'Unsupported host OS' >&2; exit 1 ;;
esac
[ -f "$library" ]
printf '%s\n' "$library"
