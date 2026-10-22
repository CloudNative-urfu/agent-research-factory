# Проверки выделения — 27 октября 2026

## Выполнено

| Проверка | Результат |
|---|---|
| `python3 scripts/verify-source.py` | 28 исходных файлов совпали побайтно |
| Полный `unittest discover -s nix/_research -p 'test_*.py'` в рабочем окружении | 115 тестов прошли без пропусков |
| Dashboard `unittest discover -s dashboard -p 'test_*.py'` | 8 тестов прошли |
| `nix flake check` на x86_64-linux | Все 4 checks прошли |
| Offline-набор внутри Nix | 113 тестов прошли без пропусков |
| HM/NixOS export check | Сервисы фабрики вычислены, launcher прокси собран |
| Standalone CLI packages | factory, experiment, proxy, dashboard собраны |

Nix checks: `factory`, `dashboard`, `source-integrity`, `module-exports`.
Отличие 115 от 113 — два контрактных теста установленных OpenCode/Claude CLI,
использующих синтетический HTTP-провайдер. Они не входят в переносимый CI-набор.
В Python-only CI без bubblewrap sandbox-тесты могут пропускаться; Nix check
предоставляет bubblewrap и проверяет их отдельно.

## Повторить

После добавления новых файлов в индекс Git:

```sh
python3 scripts/verify-source.py
python3 -B scripts/test-offline.py
python3 -B -m unittest discover -s dashboard -p 'test_*.py'
nix flake check --print-build-logs
nix build .#research-factory .#research-experiment .#research-llm-proxy .#research-dashboard
```

До добавления в индекс используйте `path:$PWD` вместо `.` в Nix-командах:
Git-flake не видит untracked файлы. В полном CLI-наборе требуется существующий
`/tmp/opencode`, OpenCode V2 в PATH, а для Claude — `RESEARCH_TEST_CLAUDE`
с абсолютным путём к CLI. Это исходные требования перенесённых тестов.

## Что не утверждается

- aarch64-linux объявлен в outputs, но здесь не собирался.
- GitHub Actions ещё не исполнялся: код не закоммичен и не отправлен.
- Реальная доступность моделей не перепроверялась при упаковке.
- Полная NixOS-система не устанавливалась; проверены модульные экспорты и пакеты.
- Научная эффективность защит этими проверками не подтверждается.

Исторический журнал исходного проекта: [source/VALIDATION.md](source/VALIDATION.md).
