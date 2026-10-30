{ self }:
{ config, lib, pkgs, ... }:
let
  cfg = config.services.shijhon-mornye-bridge;
  format = pkgs.formats.toml { };
  configuration = format.generate "mornye-bridge.toml" (cfg.settings // {
    library = "${cfg.package}/lib/libmornye_desktop.so";
    state_dir = "/data";
    ffmpeg = "${pkgs.ffmpeg}/bin/ffmpeg";
    ffprobe = "${pkgs.ffmpeg}/bin/ffprobe";
  });
in {
  options.services.shijhon-mornye-bridge = {
    enable = lib.mkEnableOption "Mornye extension bridge for Shijhon";
    package = lib.mkOption {
      type = lib.types.package;
      default = self.packages.${pkgs.stdenv.hostPlatform.system}.default;
      description = "Bridge package including lib/libmornye_desktop.so.";
    };
    stateDirectory = lib.mkOption {
      type = lib.types.str;
      default = "/var/lib/shijhon-mornye-bridge";
      description = "Private persistent state, mounted at /data in the service namespace.";
    };
    openFirewall = lib.mkOption {
      type = lib.types.bool;
      default = false;
      description = "Open only the add-on API port; management remains private.";
    };
    settings = lib.mkOption {
      type = lib.types.submodule {
        freeformType = format.type;
        options = {
          host = lib.mkOption { type = lib.types.str; default = "127.0.0.1"; };
          port = lib.mkOption { type = lib.types.port; default = 4759; };
          public_url = lib.mkOption { type = lib.types.str; default = "http://127.0.0.1:4759"; };
          management_host = lib.mkOption { type = lib.types.str; default = "127.0.0.1"; };
          management_port = lib.mkOption { type = lib.types.port; default = 4760; };
        };
      };
      default = { };
      description = "TOML settings. Library, state_dir and FFmpeg paths are supplied by the module. Do not put secrets in the Nix store.";
    };
  };
  config = lib.mkIf cfg.enable {
    assertions = [
      { assertion = cfg.settings.port != cfg.settings.management_port; message = "Bridge API and management ports must differ."; }
      { assertion = lib.hasPrefix "/" cfg.stateDirectory; message = "Bridge stateDirectory must be absolute."; }
    ];
    users.users.shijhon-mornye-bridge = { isSystemUser = true; group = "shijhon-mornye-bridge"; };
    users.groups.shijhon-mornye-bridge = { };
    networking.firewall.allowedTCPPorts = lib.mkIf cfg.openFirewall [ cfg.settings.port ];
    systemd.tmpfiles.rules = [
      "d ${cfg.stateDirectory} 0700 shijhon-mornye-bridge shijhon-mornye-bridge - -"
      "d /run/shijhon-mornye-bridge-root 0755 root root - -"
    ];
    systemd.services.shijhon-mornye-bridge = {
      description = "Mornye extension bridge for Shijhon";
      wantedBy = [ "multi-user.target" ];
      wants = [ "network-online.target" ];
      after = [ "network-online.target" ];
      unitConfig.RequiresMountsFor = [ cfg.stateDirectory ];
      restartTriggers = [ configuration ];
      environment = {
        BRIDGE_CONFIG = toString configuration;
        PYTHONDONTWRITEBYTECODE = "1";
        PYTHONUNBUFFERED = "1";
        HOME = "/data";
        SSL_CERT_FILE = "${pkgs.cacert}/etc/ssl/certs/ca-bundle.crt";
      };
      serviceConfig = {
        Type = "simple";
        User = "shijhon-mornye-bridge";
        Group = "shijhon-mornye-bridge";
        UMask = "0077";
        RootDirectory = "/run/shijhon-mornye-bridge-root";
        WorkingDirectory = "/data";
        ExecStart = lib.escapeShellArgs [ "${pkgs.tini}/bin/tini" "--" "${cfg.package}/bin/mornye-bridge" "--config" (toString configuration) "serve" ];
        Restart = "on-failure";
        RestartSec = 3;
        TimeoutStopSec = 45;
        KillMode = "control-group";
        SuccessExitStatus = [ 143 ];
        NoNewPrivileges = true;
        CapabilityBoundingSet = "";
        PrivateDevices = true;
        PrivateTmp = true;
        ProtectHome = true;
        ProtectSystem = "strict";
        ProtectKernelTunables = true;
        ProtectKernelModules = true;
        ProtectControlGroups = true;
        RestrictSUIDSGID = true;
        RestrictNamespaces = true;
        RestrictAddressFamilies = [ "AF_UNIX" "AF_INET" "AF_INET6" ];
        LockPersonality = true;
        TasksMax = 128;
        TemporaryFileSystem = [ "/tmp:rw,noexec,nosuid,nodev,mode=1777" ];
        BindReadOnlyPaths = [ "/nix/store" "/etc/resolv.conf" "/etc/hosts" "/etc/nsswitch.conf" ];
        BindPaths = [ "${cfg.stateDirectory}:/data" ];
        NoExecPaths = [ "+/" "+/data" "+/tmp" ];
        ExecPaths = [ "+/nix/store" ];
      };
    };
  };
}
