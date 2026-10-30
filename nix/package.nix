{ pkgs, nativeHost, pyproject-nix, uv2nix, pyproject-build-systems }:
let
  inherit (pkgs) lib;
  python = pkgs.python312;
  workspace = uv2nix.lib.workspace.loadWorkspace { workspaceRoot = ../.; };
  pythonSet = (pkgs.callPackage pyproject-nix.build.packages { inherit python; }).overrideScope (
    lib.composeManyExtensions [
      pyproject-build-systems.overlays.wheel
      (workspace.mkPyprojectOverlay { sourcePreference = "wheel"; })
      (final: prev: {
        shijhon-mornye-bridge = prev.shijhon-mornye-bridge.overrideAttrs (old: {
          src = lib.fileset.toSource {
            root = ../.;
            fileset = lib.fileset.unions [
              ../pyproject.toml ../README.md ../LICENSE
              (lib.fileset.fileFilter (file: !(lib.hasSuffix ".pyc" file.name)) ../src)
            ];
          };
        });
      })
    ]
  );
  environment = (pythonSet.mkVirtualEnv "shijhon-mornye-bridge-env" {
    shijhon-mornye-bridge = [ ];
  }).overrideAttrs (old: {
    postInstall = (old.postInstall or "") + ''
      UV_CACHE_DIR="$TMPDIR/uv-cache" ${pkgs.uv}/bin/uv pip check --python "$out/bin/python"
    '';
  });
  application = (pkgs.callPackages pyproject-nix.build.util { }).mkApplication {
    venv = environment;
    package = pythonSet.shijhon-mornye-bridge;
  };
in application.overrideAttrs (old: {
  nativeBuildInputs = (old.nativeBuildInputs or [ ]) ++ [ pkgs.makeWrapper ];
  postInstall = (old.postInstall or "") + ''
    wrapProgram "$out/bin/mornye-bridge" --prefix PATH : ${lib.makeBinPath [ pkgs.ffmpeg ]}
    mkdir -p "$out/lib"
    ln -s ${nativeHost}/lib/libmornye_desktop.so "$out/lib/libmornye_desktop.so"
  '';
  passthru = (old.passthru or { }) // { inherit nativeHost; pythonEnvironment = environment; };
  meta = (old.meta or { }) // {
    description = "Mornye extension bridge for Shijhon with an operator-built native host";
    license = lib.licenses.mit;
    platforms = lib.platforms.linux;
  };
})
