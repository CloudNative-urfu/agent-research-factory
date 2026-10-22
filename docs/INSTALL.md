# Установка

## nix-dots / Dendritic

После публикации первого коммита добавь файл, например
`modules/aspects/agent-research-external.nix`:

```nix
{ inputs, ... }: {
  flake-file.inputs.agent-research.url =
    "github:CloudNative-urfu/agent-research-factory";
  flake-file.inputs.agent-research.inputs.nixpkgs.follows = "nixpkgs";
  flake-file.inputs.agent-research.inputs.home-manager.follows = "home-manager";
  imports = [ inputs.agent-research.flakeModules.default ];
}
```

Регенерируй `flake.nix` через `nix run .#write-flake` по правилам nix-dots.
Новые файлы должны быть добавлены в индекс для import-tree. Старый локальный
`research.nix` нужно убрать из дерева импортируемых модулей при переходе на
внешний аспект, иначе определения будут дублироваться. Данные `_research` можно
сохранить отдельно как исходную копию.

Подключи экспортированные аспекты выбранному хосту и пользователю:

```nix
# В modules хоста:
inputs.self.modules.nixos.agent-research

# В home-manager.users.<имя>.imports:
inputs.self.modules.homeManager.agent-research
```

Этот путь сохраняет исходный контракт nix-dots: `flake.modules` и
`local.unfreePackages` предоставляются самим nix-dots. Фабрика не включается на
все хосты при одном только импорте flake-модуля.

## Обычный Home Manager

```nix
imports = [ inputs.agent-research.homeManagerModules.default ];
```

В используемом экземпляре nixpkgs разреши `claude-code` существующим механизмом
unfree-пакетов. Если используется `home-manager.useGlobalPkgs`, это настройка
NixOS/nixpkgs, а не отдельного пользователя. Не заменяй существующий predicate:
добавь Claude Code к уже разрешённым пакетам.

Также экспортирован `nixosModules.default`: он подключает Home Manager и добавляет
модуль фабрики в `home-manager.sharedModules`. Это удобно для выделенного
исследовательского хоста; для одного пользователя предпочитай прямой HM-импорт.

## CLI без установки сервисов

```sh
nix run .#research-factory -- --help
nix run .#research-experiment -- --help
nix run .#research-dashboard -- ~/.local/share/research-campaigns/defense
```

Standalone-пакеты не создают профили авторизации: для реальной работы контроллера
нужны доступные в PATH `research-claude`, `research-opencode` и
`research-experiment`, настроенные writable paths и отдельная авторизация.
Полную настройку делает Home Manager-модуль. `--help`, `status` и работа
экспериментального исполнителя доступны отдельно.

Flake закрепляет те же ревизии nixpkgs и Home Manager, что исходный nix-dots на
момент выделения. `follows` сознательно заменяет эти версии версиями потребителя;
после такого изменения повтори проверки совместимости CLI.
