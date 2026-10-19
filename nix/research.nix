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
      opencodeConfig = {
        "$schema" = "https://opencode.ai/config.json";
        update = "disable";
        default_agent = "researcher";
        websearch.provider = "random";
        instructions = [ (toString rules) ];
        plugins = [ (toString ./_research/plugin) ];
        permissions = [
          {
            action = "*";
            resource = "*";
            effect = "allow";
          }
        ];
        agents = {
          researcher = {
            mode = "primary";
            description = "Автономный исследователь: гипотезы, эксперименты, MVP";
          };
          critic = {
            mode = "all";
            description = "Независимая проверка доказательств и воспроизводимости";
            system = critic;
          };
        };
        mcp.servers =
          lib.mapAttrs (_: server: {
            type = "local";
            command = [ server.command ] ++ server.args;
          }) localMcp
          // {
            context7 = {
              type = "remote";
              url = "https://mcp.context7.com/mcp";
              oauth = false;
            };
          };
        experimental.todo = true;
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
      opencodeFile = pkgs.writeText "research-opencode.json" (builtins.toJSON opencodeConfig);
      opencodeLauncher = pkgs.writeShellApplication {
        name = "research-opencode";
        runtimeInputs = tools;
        text = ''
          export XDG_CONFIG_HOME=${lib.escapeShellArg "${root}/opencode-config"}
          export XDG_DATA_HOME=${lib.escapeShellArg "${config.xdg.dataHome}/agent-research"}
          export XDG_STATE_HOME=${lib.escapeShellArg "${config.xdg.stateHome}/agent-research"}
          export XDG_CACHE_HOME=${lib.escapeShellArg "${config.xdg.cacheHome}/agent-research"}
          export OPENCODE_DISABLE_PROJECT_CONFIG=1
          export OPENCODE_DISABLE_LSP_DOWNLOAD=true
          unset OPENCODE_CONFIG OPENCODE_CONFIG_CONTENT OPENCODE_CONFIG_DIR OPENCODE_DB
          export OPENCODE_CONFIG=${opencodeFile}
          mkdir -p "$XDG_STATE_HOME"
          (
            flock 9
            if [[ "$(${lib.getExe pkgs.opencode} service get disabled)" != "true" ]]; then
              ${lib.getExe pkgs.opencode} service set disabled true >/dev/null
            fi
          ) 9>"$XDG_STATE_HOME/service-mode.lock"
          case "''${1:-}" in
            run|api)
              command="$1"
              shift
              exec ${lib.getExe pkgs.opencode} "$command" --standalone "$@"
              ;;
            ""|-*|/*|./*|../*) exec ${lib.getExe pkgs.opencode} --standalone "$@" ;;
            *) exec ${lib.getExe pkgs.opencode} "$@" ;;
          esac
        '';
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
          opencodeLauncher
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
        opencodeLauncher
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
      xdg.configFile."agent-research/opencode-config/opencode/opencode.json".text =
        builtins.toJSON opencodeConfig;
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
    };
}
