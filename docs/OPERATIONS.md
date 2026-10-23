# Работа с кампанией

После подключения Home Manager-модуля авторизуй отдельные профили:

```sh
research-claude
research-opencode
```

Учётные данные хранятся вне репозитория. Ключ экспериментальной Qwen в OpenRouter
не авторизует исследователя и критика. Шаблон установлен как
`~/.config/agent-research/openrouter.example.env`; создай из него `openrouter.env`
с правами 600 и заполни локально. Не перезаписывай существующий файл ключа.

```sh
systemctl --user start research-llm-proxy
research-llm-proxy --check
research-factory init ~/.local/share/research-campaigns/defense \
  --brief "$PWD/docs/source/agent-defense-and-continuity.md" \
  --config "$PWD/examples/campaign.v2.json"
research-factory doctor ~/.local/share/research-campaigns/defense --live
systemctl --user start research-campaign@defense
research-factory status ~/.local/share/research-campaigns/defense
```

`--check` и `doctor --live` обращаются к внешним моделям и расходуют квоты.
Пример модели критика нужно заменить, если она недоступна вашей учётной записи.
Прохождение unit-тестов не подтверждает доступность провайдеров.

## Текущий API

Модель `qwen/qwen3.5-9b`; строгий маршрут `darkbloom/fp4`, имя провайдера в ответе
`Darkbloom`. Tools используются с `auto` либо без `tool_choice`. На проверке
25 октября 2026 года `required` и выбор конкретной функции возвращали 404.
Это наблюдение, а не обещание будущей доступности; перед кампанией нужен preflight.

## Остановка и восстановление

```sh
systemctl --user stop research-campaign@defense
systemctl --user stop research-llm-proxy
```

Сначала проверь `state.json`, квитанции и последние журналы. Статус в JSON может
остаться `running`, если контроллер аварийно завершился вне обработки стадии;
сверяй его с состоянием systemd. При лимите подписки дождись сброса или явно
измени исполнителя. Контроллер читает campaign.json при запуске: редактирование
конфига не меняет роль внутри уже запущенного процесса.

После устранения причины можно выполнить `research-factory run <кампания> --retry`
при остановленном сервисе. Он сбрасывает попытки текущей стадии с записью истории.
Не оставляй `--retry` в постоянном ExecStart. Не повторяй автоматически unknown
запросы: используй явное решение исполнителя с обоснованием.

Для завершённой серии: `research-factory extend <кампания> --cycles 15 --pipeline-version 2`.
Число — общий лимит циклов, не число дополнительных. Перед изменениями сделай
проверенный бекап. Не меняй замороженные протоколы и одобренные доказательства.

## Монитор

`research-dashboard <кампания>` открывает read-only API/UI на localhost:8788.
Для фоновой работы можно использовать отдельный пользовательский systemd-сервис.
Версия цикла берётся из `cycle.json`: старые циклы остаются v1. После обновления
backend перезапусти процесс сайта, а не только страницу браузера.
