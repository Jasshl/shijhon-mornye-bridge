{ pkgs }:
let
  overlaySource = builtins.fetchTarball {
    url = "https://github.com/oxalica/rust-overlay/archive/19ecdd08213836b36c4296a3a4f4af5a3d5308db.tar.gz";
    sha256 = "1x5jxzm82v4h2y39d28clnva8h2l7xdincr2xbvs9h8yhxzx96dv";
  };
  rustPkgs = pkgs.extend (import overlaySource);
  toolchain = rustPkgs.rust-bin.stable."1.99.0".minimal;
  rustPlatform = pkgs.makeRustPlatform { cargo = toolchain; rustc = toolchain; };
  src = pkgs.fetchFromGitHub {
    owner = "mornyeapp";
    repo = "Mornye";
    rev = "c90ec6478ad3e808ef22ba9464f8ad05054dde7b";
    hash = "sha256-z2dQepqk2f86KSOlgS3LHMdigJ+g2XmVBfLu/MQw8xs=";
  };
in rustPlatform.buildRustPackage {
  pname = "mornye-desktop";
  version = "5.2.6-c90ec6478";
  inherit src;
  sourceRoot = "${src.name}/rust_backend";
  cargoHash = "sha256-9m0EDbSuYnMLaO7XOvrbHFYoW8WPlMmgj7Yp/IuTphE=";
  cargoBuildFlags = [ "-p" "mornye-desktop" "--lib" ];
  cargoTestFlags = [ "-p" "mornye-desktop" "--lib" ];
  nativeBuildInputs = [ pkgs.pkg-config rustPlatform.bindgenHook ];
  LIBCLANG_PATH = "${pkgs.lib.getLib pkgs.llvmPackages.libclang}/lib";
  preBuild = ''
    test "$(rustc --version | cut -d ' ' -f 2)" = 1.99.0
    test "$(cargo --version | cut -d ' ' -f 2)" = 1.99.0
  '';
  installPhase = ''
    runHook preInstall
    install -Dm755 target/${pkgs.stdenv.hostPlatform.rust.rustcTarget}/release/libmornye_desktop.so $out/lib/libmornye_desktop.so
    mkdir -p $out/share/mornye
    for name in LICENSE COPYING NOTICE; do
      if test -f ../$name; then cp ../$name $out/share/mornye/$name; fi
    done
    cp NOTICE $out/share/mornye/rust_backend-NOTICE
    echo c90ec6478ad3e808ef22ba9464f8ad05054dde7b > $out/share/mornye/source-commit
    cp Cargo.lock $out/share/mornye/Cargo.lock
    runHook postInstall
  '';
  meta = {
    description = "Locally built pinned Mornye native extension host";
    license = pkgs.lib.licenses.lgpl3Only;
    platforms = pkgs.lib.platforms.linux;
  };
}
