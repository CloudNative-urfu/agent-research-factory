# Режим deep_case

Режим глубокой проработки кейса ([T5](https://github.com/CloudNative-urfu/agent-research-factory/issues/5)).
Обычная кампания проверяет жизнеспособность гипотезы и отсеивает её; `deep_case` получает
готовую постановку из внешних документов и доводит её до исполняемой zero-версии.
Гейтов допуска нет, `screened_out` не применяется, сбой стадии ведёт к ремонту.

Включается полем `"mode": "deep_case"` в конфигурации новой кампании. Код —
`nix/_research/deep_case.py`; `factory.py` передаёт ему `init` и `run`, остальные
команды (`status`, `doctor`) общие. Пример: `examples/campaign.deep_case.json` и
задание `examples/deep_case.darp.md` для DARP.

## Конфигурация

Поля как у обычной кампании, плюс:

| Поле | Смысл |
|---|---|
| `case.repository`, `case.ref` | Репозиторий с документами кейса и ветка/тег |
| `case.documents` | Пути документов в репозитории; строительные роли получают их целиком |
| `case.key_requirements` | ID требований, которые обязаны реализовать компоненты, а не `out_of_scope` |
| `attempts_per_stage` | Бюджет попыток на каждый шаг |
| `fix_rounds` | Число ремонтных раундов сквозной проверки (по умолчанию 3) |

`cycles` остаётся обязательным общим полем и равен 1. `pipeline_version` — только 1.
Исполнители `researcher` и `critic` настраиваются как обычно.

## Запуск

```sh
research-factory init ~/.local/share/research-campaigns/darp \
  --brief "$PWD/examples/deep_case.darp.md" \
  --config "$PWD/examples/campaign.deep_case.json"
research-factory doctor ~/.local/share/research-campaigns/darp --live
systemctl --user start research-campaign@darp
research-factory status ~/.local/share/research-campaigns/darp
research-dashboard ~/.local/share/research-campaigns/darp
```

`init` клонирует `case.repository` на `case.ref`, копирует документы в `case/` кампании и
записывает `case/case.json`: commit, SHA-256 документов и список ID требований
(`FR-…`, `NFR-…`), найденных в тексте. Перед `run` выполняется тот же live preflight,
что и в обычной кампании.

## Pipeline

`decompose → build:<компонент>… → integrate → fix → report → critique`

| Шаг | Роль | Результат и проверка контроллера |
|---|---|---|
| `decompose` | researcher | `artifacts/decomposition.json`: компоненты с `requirements`, `depends_on`, `check_command`; `out_of_scope`; `assumptions`. Каждый ID из `case.json` распределён, ключевые — в компонентах, зависимости без циклов |
| `build:<id>` | researcher | Компонент в порядке зависимостей; его `check_command` проходит в чистой копии |
| `integrate` | researcher | `artifacts/submission.json`: `verify_command` (офлайн, сквозной) и необязательный `run_command` (живая модель) проходит в чистой копии |
| `fix` | researcher | Контроллер прогоняет все `check_command` и `verify_command`; при падении — ремонт и повтор |
| `report` | researcher | `artifacts/report.md`, `artifacts/status.json` со статусом каждого требования |
| `critique` | critic | В копии финального снимка: `artifacts/critique.json`, `critique.md` — описание, не gate |

Все стадии работают в одном каталоге `cycles/01/work`: полигон достраивается, а не
пишется заново. `case/`, `brief.md`, `handoff.json` и `workflow-contract.json` в песочнице
только для чтения. `handoff.json` перед каждой попыткой содержит шаг, номер попытки,
действие, компонент, статусы остальных, требования и до трёх последних сбоев с хвостами
журналов: журналы контроллера вне песочницы, агент их иначе не видит.

## Ремонт вместо отказа

- Первая попытка шага — `build`, следующие — `repair` с причиной сбоя. Последняя попытка
  при бюджете от трёх — `replan`: сузить шаг до минимальной работающей версии, явная
  заглушка допустима, причина пишется в `artifacts/simplifications.json`.
- Сбой — это невалидные артефакты, падение проверки или таймаут стадии. Исчерпав бюджет,
  шаг получает статус `failed`, кампания идёт дальше. Упавший компонент получает ещё шанс
  в `fix`: если его проверка там прошла, статус меняется на `repaired`.
- Если `decompose` не удался, шаги `build` пропускаются, а `integrate` строит полигон целиком.
- Если не создан снимок для критики (символическая ссылка или секретоподобный файл в
  рабочем каталоге), `critique` получает `skipped`.
- Кампания всегда завершается статусом `completed` и `FINAL.md` с таблицей шагов.

`check_command` компонента читается из текущего `decomposition.json`: `replan` может
сузить собственную проверку. Использованный argv записывается в журнал при каждом
запуске, а ослабление проверки видит критик.

## Что останавливает кампанию

Только инфраструктура, с сохранением состояния (`blocked`):

- сбой live preflight, отсутствие `bwrap`, не заданная модель;
- `attempts_per_stage` подряд неуспешных запусков исполнителя на одном шаге
  (ошибка провайдера, лимит подписки). Они не расходуют бюджет ремонта: сбой провайдера
  ничего не говорит о полигоне;
- прерывание процесса.

После устранения причины: `research-factory run <кампания> --retry` при остановленном
сервисе. Он обнуляет счётчики текущего шага и записывает сброс в журнал.

## Файлы кампании

| Путь | Содержимое |
|---|---|
| `case/` | Документы кейса и `case.json` (commit, хеши, ID требований) |
| `journal.jsonl` | Журнал: `started`, `invalid`, `check_failed`, `timeout`, `worker_failed`, `done`, `gave_up`, `suite`, `repaired`, `skipped`, `retry`, `blocked`, `completed` |
| `cycles/01/work/` | Полигон и `artifacts/` |
| `cycles/01/logs/` | Журналы исполнителей с `.request.json`/`.receipt.json`, журналы проверок |
| `cycles/01/verification.json` | Последняя сквозная проверка контроллера |
| `cycles/01/snapshot/`, `manifest.json` | Финальный снимок полигона с SHA-256 |
| `cycles/01/review/` | Рабочий каталог критика |
| `FINAL.md` | Итог и таблица шагов |

Каждая проверка выполняется в отдельной копии рабочего каталога, которая затем удаляется;
остаётся её журнал. Временные окружения (`.venv`, `tmp`, `node_modules` и т.п.) в копию не попадают.

## Известные ограничения

- Офлайн-проверки контроллера не доказывают поведение живой модели: живые прогоны идут
  через `run_command` и не запускаются контроллером автоматически.
- Бюджет запросов к экспериментальной модели не навязывается исполнителем
  `research-experiment`: полигон обращается к API сам. Лимиты задаются в `run_command`.
- Ослабление `check_command` при `replan` контролируется только журналом и критикой.
- `extend` к кампании `deep_case` не применяется.
- Офлайн-набор тестов перенесённого контроллера режим `deep_case` не покрывает.
