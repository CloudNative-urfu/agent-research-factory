# Agent Research Factory

Исследовательская фабрика команды **CloudNative-urfu**: постановка задачи → идеи →
малый пилот → реализация → измерения → независимая критика. Каждый цикл создаёт
новую реализацию, используя память предыдущих результатов, а не копируя старый MVP.

Проект перенесен из NixOS конфигурации, [Манифест](docs/source-manifest.json).

## Начать здесь

- [Установка и подключение в nix-dots](docs/INSTALL.md)
- [Запуск, остановка и восстановление](docs/OPERATIONS.md)
- [Методология и границы результатов](docs/METHODOLOGY.md)
- [Режим deep_case: сборка полигона по докам кейса](docs/DEEP_CASE.md)
- [Четыре командные задачи](docs/TASKS.md)
- [Проверки и воспроизводимость](docs/VALIDATION.md)
- [Происхождение и подготовка истории](docs/PROVENANCE.md)

## Установка dashboard

Dashboard поставляется отдельным пакетом `research-dashboard` и не устанавливается
при подключении аспекта `agent-research`. Добавь его в Home Manager-конфигурацию
пользователя, используя имя flake-инпута из своей конфигурации:

```nix
{ inputs, pkgs, ... }:
{
  home.packages = [
    inputs.agent-research-factory.packages.${pkgs.stdenv.hostPlatform.system}.research-dashboard
  ];
}
```

Здесь input называется `agent-research-factory`; в примере из
[инструкции установки](docs/INSTALL.md) он называется `agent-research` — в таком
случае замени имя в выражении выше.

Для NixOS с интегрированным Home Manager примени конфигурацию выбранного хоста,
например `book`, и запусти монитор существующей кампании:

```sh
sudo nixos-rebuild switch --flake .#book
research-dashboard ~/.local/share/research-campaigns/defense
```

Открой <http://127.0.0.1:8788>. Остановка — `Ctrl+C`, другой порт — `--port 8789`.
Dashboard читает состояние кампании и не запускает её самостоятельно.
Подробности интерфейса: [dashboard/README.md](dashboard/README.md).

## Состав

| Каталог | Содержимое |
|---|---|
| `nix/_research/` | Контроллер, v2, исполнитель запросов, прокси, агенты, тесты |
| `nix/research.nix` | Исходный Dendritic-аспект из nix-dots |
| `flake.nix` | Экспорт модулей, CLI-пакетов, checks и devShell |
| `dashboard/` | Локальный read-only монитор кампании |
| `examples/` | Конфигурации без реальных секретов |
| `docs/source/` | Исходные постановка, планы и журналы проверки |

Linux; Python standard library; Nix/Home Manager для воспроизводимой установки.
Роли исполняют Claude Code и OpenCode. Экспериментальная модель — отдельный ресурс.
Текущий прокси: `qwen/qwen3.5-9b`, маршрут `darkbloom/fp4`, без fallback.
Сервисы не запускаются автоматически при импорте модуля.

Архивы кампаний, виртуальные окружения, бинарники и учётные данные в Git не входят.
Материалы конкретных кампаний хранятся отдельно; здесь нельзя трактовать тестовые
fixtures как подтверждённые исследовательские результаты.

**Лицензия:** [MIT](LICENSE). Зависимости сохраняют собственные лицензии.
