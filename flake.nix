{
  description = "Operator-built Mornye extension bridge for Shijhon";
  inputs = {
    # A fixed NixOS 26.05 nixpkgs snapshot, independent of the host's NixOS inputs.
    nixpkgs.url = "github:NixOS/nixpkgs/26f4107e47e3ad0ffd0db220469ba83b6f6d96e8";
    pyproject-nix = {
      url = "github:pyproject-nix/pyproject.nix/ddc400a6883b042646d0940e65971bb7f7e96107";
      inputs.nixpkgs.follows = "nixpkgs";
    };
    uv2nix = {
      url = "github:pyproject-nix/uv2nix/7aba73a3d85454f1118f8cf9de97979b4d26bcac";
      inputs.nixpkgs.follows = "nixpkgs";
      inputs.pyproject-nix.follows = "pyproject-nix";
    };
    pyproject-build-systems = {
      url = "github:pyproject-nix/build-system-pkgs/953d35d03a51dbecaf3feb3fb679eb63e90ebeb4";
      inputs.nixpkgs.follows = "nixpkgs";
      inputs.pyproject-nix.follows = "pyproject-nix";
      inputs.uv2nix.follows = "uv2nix";
    };
  };
  outputs = inputs@{ self, nixpkgs, ... }:
    let
      systems = [ "x86_64-linux" "aarch64-linux" ];
      forAllSystems = nixpkgs.lib.genAttrs systems;
    in {
      packages = forAllSystems (system:
        let
          pkgs = nixpkgs.legacyPackages.${system};
          nativeHost = import ./nix/host.nix { inherit pkgs; };
          bridge = import ./nix/package.nix {
            inherit pkgs nativeHost;
            inherit (inputs) pyproject-nix uv2nix pyproject-build-systems;
          };
        in { default = bridge; inherit bridge nativeHost; });
      nixosModules.default = import ./nix/module.nix { inherit self; };
      nixosModules.shijhon-mornye-bridge = self.nixosModules.default;
      checks = forAllSystems (system:
        let
          pkgs = nixpkgs.legacyPackages.${system};
          machine = nixpkgs.lib.nixosSystem {
            inherit system;
            modules = [ self.nixosModules.default ({ ... }: {
              boot.loader.grub.enable = false;
              fileSystems."/" = { device = "none"; fsType = "tmpfs"; };
              system.stateVersion = "26.05";
              services.shijhon-mornye-bridge.enable = true;
            }) ];
          };
          cfg = machine.config;
          unit = cfg.systemd.services.shijhon-mornye-bridge;
        in {
          package = self.packages.${system}.default;
          module = assert cfg.networking.firewall.allowedTCPPorts == [ ];
            assert unit.serviceConfig.User == "shijhon-mornye-bridge";
            assert unit.serviceConfig.ProtectSystem == "strict";
            assert cfg.services.shijhon-mornye-bridge.settings.management_host == "127.0.0.1";
            pkgs.runCommand "bridge-module-check" { } ''
              ${self.packages.${system}.default.pythonEnvironment}/bin/python -c \
                'from pathlib import Path; from mornye_bridge.config import Config; Config.load(Path("${unit.environment.BRIDGE_CONFIG}"))'
              test -s ${unit.environment.BRIDGE_CONFIG}
              cp ${unit.environment.BRIDGE_CONFIG} $out
            '';
        });
    };
}
