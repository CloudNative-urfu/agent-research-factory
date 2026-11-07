# Разработка следующей ревизии через OpenSpec

Это обязательный контракт при наличии candidate/development.json. Прочитай
candidate/coevolution.md и candidate/prototype/openspec/ (если есть). Базовые COSTAR
и EVALUATION-METHOD требования также действуют. Не реализуй код на стадии планирования.

## Брейншторм
Сначала выбери ограниченный шаг программы, учитывая остатки прошлой критики. В
artifacts/ создай настоящий OpenSpec change:
- openspec/config.yaml: schema: spec-driven;
- openspec/changes/<unique-kebab-name>/proposal.md (Why, What Changes, Capabilities, Impact);
- design.md (Context, Goals / Non-Goals, Decisions, Risks / Trade-offs, Migration Plan);
- specs/<capability>/spec.md: `## ADDED Requirements`, `### Requirement: ...`, SHALL/MUST,
  `#### Scenario: ...`, WHEN/THEN с конкретными проверяемыми исходами;
- tasks.md: ровно одна строка `- [ ] <id-из-plan.json> <измеримая задача>` на каждую
  задачу плана. Все ID совпадают с plan.json, без подпунктов-checkbox с новыми ID.
  Подробные тест-кейсы и шаги опиши обычным текстом. Начни все задачи unchecked.
- artifacts/openspec-plan.json (относительно work, НЕ вложенный artifacts/artifacts):
  {"change":"unique-kebab-name"}.

CLI доступен в PATH. Можно использовать `openspec new change <name>` из artifacts/.
Выполни там `openspec validate <name> --strict --no-interactive` до сдачи.
Не используй имя уже существующего change в candidate/prototype/openspec/.
Новый change описывает только одобряющееся приращение, не всю программу одним проходом.
Сделай artifacts/coevolution-progress.md с доказательствами и оставшимися зависимостями.

## Критик плана
Прочитай proposal, design, spec scenarios и tasks. Проверь достаточность тестов:
OS-негативные проверки/утечки/provenance/режимы feedback/память/матрица соперников по
выбранному шагу. OpenSpec CLI проверяет структуру, не истинность или полноту требований.
При proceed одобри ВСЕ ID этого плана. Если нужна часть — revise с сокращением change,
а не согласование подмножества при неизменных specs. Новая методика не требует сразу
реализовать всю программу; зависимости можно выполнять отдельными ревизиями.

## Исполнитель
Одобренный change уже установлен в openspec/changes/<name>. Используй его как источник
объёма и критериев. Сначала RED, затем реализация и GREEN. Не меняй утверждённые
proposal/design/specs и текст задач; разрешено только отмечать завершение `[x]`.
Не архивируй change в этом проходе: критик проверяет этот же путь и хеши. Открытая
задача честно остаётся unchecked; необходимое изменение scope предложи следующим планом.
В отчёте создай artifacts/openspec-results.json:
{"change":"name","tasks":[{"id":"id-из-плана","status":"implemented",
 "summary":"результат и ограничения","evidence":["artifacts/test-log.txt"]}]}.
Статусы: implemented, partial, blocked. Каждый ID ровно один раз; существующие непустые
доказательства нужны и для partial/blocked (причина, неуспешный тест, конкретный остаток).
Только implemented имеет `[x]`. Сохрани вывод CLI validate и тестов. Создай/обнови
artifacts/coevolution-progress.md. Количество зелёных тестов не заменяет проверку сценариев.

## Финальный критик
Прочитай утверждённый change в candidate/openspec/changes/<name> и результаты автора.
Воспроизведи сценарии, добавь независимые отрицательные пробы и проверь ограничения
доступа/памяти. Создай artifacts/openspec-review.json в том же формате tasks, но со
своими статусами, summary и evidence. Можно понизить implemented до partial/blocked;
не редактируй checkbox автора. Все ID должны быть рассмотрены. Отрази расхождения в
critique и coevolution-progress. Частичный результат не означает готовность следующего
уровня программы; следующая оценка должна учитывать твоё заключение.
