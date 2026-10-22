{
  description = "CloudNative-urfu autonomous research factory";

  inputs = {
    nixpkgs.url = "github:NixOS/nixpkgs/02c087f3fe673c9e317f206a627cb6b910336dbe";
    home-manager.url = "github:nix-community/home-manager/cb7de2c7e589afe7531d4832c71ad4df144624ea";
    home-manager.inputs.nixpkgs.follows = "nixpkgs";
  };

  outputs =
    {
      self,
      nixpkgs,
      home-manager,
    }:
    let
      systems = [
        "x86_64-linux"
        "aarch64-linux"
      ];
      forAllSystems = nixpkgs.lib.genAttrs systems;
      aspect = import ./nix/research.nix;
      pkgsFor =
        system:
        import nixpkgs {
          inherit system;
          config.allowUnfreePredicate = pkg: nixpkgs.lib.getName pkg == "claude-code";
        };
    in
    {
      # Original Dendritic aspect; expects flake.modules and local.unfreePackages.
      flakeModules.default = ./nix/research.nix;
      homeManagerModules.default = aspect.flake.modules.homeManager.agent-research;
      nixosModules.default = {
        imports = [ home-manager.nixosModules.home-manager ];
        home-manager.sharedModules = [ self.homeManagerModules.default ];
      };

      packages = forAllSystems (
        system:
        let
          pkgs = pkgsFor system;
          command =
            name: file:
            pkgs.writeShellApplication {
              inherit name;
              runtimeInputs = [
                pkgs.python3
                pkgs.bubblewrap
                pkgs.git
                pkgs.curl
                pkgs.uv
                pkgs.bun
              ];
              text = ''exec ${pkgs.python3}/bin/python3 ${./nix/_research}/${file} "$@"'';
            };
        in
        {
          research-factory = command "research-factory" "factory.py";
          research-experiment = command "research-experiment" "experiment_runner.py";
          research-llm-proxy = command "research-llm-proxy" "llm_proxy.py";
          default = self.packages.${system}.research-factory;
          research-dashboard = pkgs.writeShellApplication {
            name = "research-dashboard";
            text = ''exec ${pkgs.python3}/bin/python3 ${./dashboard}/dashboard.py "$@"'';
          };
        }
      );

      apps = forAllSystems (
        system:
        builtins.mapAttrs (name: package: {
          type = "app";
          program = "${package}/bin/${if name == "default" then "research-factory" else name}";
        }) self.packages.${system}
      );

      checks = forAllSystems (
        system:
        let
          pkgs = pkgsFor system;
          home = home-manager.lib.homeManagerConfiguration {
            inherit pkgs;
            modules = [
              self.homeManagerModules.default
              {
                home.username = "researcher";
                home.homeDirectory = "/home/researcher";
                home.stateVersion = "26.05";
              }
            ];
          };
          host = nixpkgs.lib.nixosSystem {
            inherit system;
            modules = [
              self.nixosModules.default
              {
                nixpkgs.config.allowUnfreePredicate = pkg: nixpkgs.lib.getName pkg == "claude-code";
                system.stateVersion = "26.05";
                home-manager.useGlobalPkgs = true;
                home-manager.users.researcher = {
                  home.username = "researcher";
                  home.homeDirectory = "/home/researcher";
                  home.stateVersion = "26.05";
                };
              }
            ];
          };
        in
        {
          module-exports =
            assert builtins.hasAttr "research-campaign@" home.config.systemd.user.services;
            assert builtins.hasAttr "research-llm-proxy"
              host.config.home-manager.users.researcher.systemd.user.services;
            pkgs.runCommand "research-module-exports" { } ''
              test -x ${builtins.head home.config.systemd.user.services.research-llm-proxy.Service.ExecStart}
              touch "$out"
            '';
          factory =
            pkgs.runCommand "research-factory-tests"
              {
                nativeBuildInputs = [
                  pkgs.python3
                  pkgs.bubblewrap
                ];
              }
              ''
                export PYTHONDONTWRITEBYTECODE=1
                python3 ${./scripts/test-offline.py} ${self}
                touch "$out"
              '';
          dashboard =
            pkgs.runCommand "research-dashboard-tests"
              {
                nativeBuildInputs = [ pkgs.python3 ];
              }
              ''
                export PYTHONDONTWRITEBYTECODE=1
                python3 -m unittest discover -s ${./dashboard} -p 'test_*.py'
                touch "$out"
              '';
          source-integrity =
            pkgs.runCommand "research-source-integrity"
              {
                nativeBuildInputs = [ pkgs.python3 ];
              }
              ''
                python3 ${./scripts/verify-source.py} ${self}
                touch "$out"
              '';
        }
      );

      devShells = forAllSystems (
        system:
        let
          pkgs = pkgsFor system;
        in
        {
          default = pkgs.mkShell {
            packages = [
              pkgs.python3
              pkgs.bubblewrap
              pkgs.git
            ];
          };
        }
      );
    };
}
