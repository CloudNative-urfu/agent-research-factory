{
  perSystem = { pkgs, lib, ... }: {
    checks = lib.optionalAttrs pkgs.stdenv.hostPlatform.isLinux {
      research-factory =
        pkgs.runCommand "research-factory-tests" { nativeBuildInputs = [ pkgs.python3 ]; }
          ''
            export PYTHONDONTWRITEBYTECODE=1
            python3 -m unittest discover -s ${./_research} -p test_factory.py
            python3 -m unittest discover -s ${./_research} -p test_proxy.py
            python3 -m unittest discover -s ${./_research} -p test_workflow_v2.py
            python3 -m unittest discover -s ${./_research} -p test_experiment_runner.py
            touch "$out"
          '';
    };
  };

  flake.modules.nixos.agent-research = {
    local.unfreePackages = [ "claude-code" ];
  };

  flake.modules.homeManager.agent-research =
    {
      pkgs,
      lib,
      config,
      ...
    }:
    let
      root = "${config.xdg.configHome}/agent-research";
      rules = ./_research/AGENTS.md;
      critic = builtins.readFile ./_research/critic.md;
      tools = with pkgs; [
        git
        curl
        jq
        ripgrep
        fd
        uv
        python3
        bun
        nodejs
        gcc
        gnumake
        pkg-config
        shellcheck
        util-linux
        mcp-nixos
        playwright-mcp
      ];
      localMcp = {
        nixos = {
          command = lib.getExe pkgs.mcp-nixos;
          args = [ ];
        };
        playwright = {
          command = lib.getExe pkgs.playwright-mcp;
          args = [
            "--headless"
            "--isolated"
          ];
        };
      };
      claudeMcp = pkgs.writeText "research-mcp.json" (
        builtins.toJSON {
          mcpServers = lib.mapAttrs (_: server: server // { type = "stdio"; }) localMcp // {
            context7 = {
              type = "http";
              url = "https://mcp.context7.com/mcp";
            };
          };
        }
      );
      claudeAgents = builtins.toJSON {
        critic = {
          description = "Независимая проверка результатов исследования";
          prompt = critic;
        };
      };
      claudeLauncher = pkgs.writeShellApplication {
        name = "research-claude";
        runtimeInputs = tools;
        text = ''
          export CLAUDE_CONFIG_DIR=${lib.escapeShellArg "${root}/claude"}
          export DISABLE_AUTOUPDATER=1
          exec ${lib.getExe pkgs.claude-code} \
            --setting-sources user \
            --model opus \
            --permission-mode bypassPermissions \
            --disallowedTools AskUserQuestion \
            --strict-mcp-config --mcp-config ${claudeMcp} \
            --append-system-prompt-file ${rules} \
            --agents ${lib.escapeShellArg claudeAgents} "$@"
        '';
      };
      proxyLauncher = pkgs.writeShellApplication {
        name = "research-llm-proxy";
        text = ''
          exec ${lib.getExe pkgs.python3} ${./_research}/llm_proxy.py "$@"
        '';
      };
      factoryLauncher = pkgs.writeShellApplication {
        name = "research-factory";
        runtimeInputs = tools ++ [
          pkgs.bubblewrap
          claudeLauncher
          experimentLauncher
        ];
        text = ''
          export RESEARCH_WRITABLE_PATHS=${
            lib.escapeShellArg (
              lib.concatStringsSep ":" [
                root
                "${config.xdg.dataHome}/agent-research"
                "${config.xdg.stateHome}/agent-research"
                "${config.xdg.cacheHome}/agent-research"
              ]
            )
          }
          export PYTHONTZPATH=${pkgs.tzdata}/share/zoneinfo
          exec ${lib.getExe pkgs.python3} ${./_research}/factory.py "$@"
        '';
      };
      experimentLauncher = pkgs.writeShellApplication {
        name = "research-experiment";
        text = ''
          exec ${lib.getExe pkgs.python3} ${./_research}/experiment_runner.py "$@"
        '';
      };
    in
    {
      home.packages = [
        claudeLauncher
        factoryLauncher
        proxyLauncher
        experimentLauncher
      ];
      xdg.configFile."agent-research/openrouter.example.env".source = ./_research/openrouter.example.env;
      systemd.user.services.research-llm-proxy = {
        Unit.Description = "Fixed-model OpenRouter research gateway";
        Service = {
          ExecStart = lib.getExe proxyLauncher;
          EnvironmentFile = "${root}/openrouter.env";
          Restart = "on-failure";
          RestartPreventExitStatus = [ 2 ];
          RestartSec = 5;
          UMask = "0077";
          NoNewPrivileges = true;
        };
      };
      systemd.user.services."research-campaign@" = {
        Unit = {
          Description = "Research campaign %i";
          Wants = [ "research-llm-proxy.service" ];
          After = [ "research-llm-proxy.service" ];
        };
        Service = {
          Type = "simple";
          WorkingDirectory = "%h/.local/share/research-campaigns/%i";
          ExecStart = "${lib.getExe factoryLauncher} run %h/.local/share/research-campaigns/%i";
          Restart = "on-failure";
          RestartPreventExitStatus = [ 2 ];
          RestartSec = 10;
          KillMode = "control-group";
          TimeoutStopSec = 15;
          UMask = "0077";
        };
      };
      systemd.user.services."research-autopilot@" = {
        Unit.Description = "Reviewed improvement autopilot %i";
        Service = {
          Type = "simple";
          ExecStart = "${lib.getExe factoryLauncher} autopilot %h/.local/share/research-autopilot/%i";
          Restart = "on-failure";
          RestartPreventExitStatus = [ 2 ];
          RestartSec = 30;
          KillMode = "control-group";
          TimeoutStopSec = 15;
          UMask = "0077";
        };
      };
    };
}
