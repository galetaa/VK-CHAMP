# Backend D2 для ТЗ 2.0.0

«Первый заказ из сообщества» работает через `/api/v2`. P0 ведёт предложение от черновика до тестового заказа и обработки оператором. P1 добавляет точный подбор B001 в ту же цепочку quote → резерв → заказ. Для P1 покупатель передаёт идентификатор сохранённого решения.

## Запуск v2

Из `casechamp_dev` выполните команды ниже. Создание файла окружения нужно один раз. Для v2 используется отдельный проект Compose и новая схема `vk_demo_v2`. Данные v1 можно оставить в их исходном проекте.

```sh
uvx --from uv==0.13.9 uv sync --locked
mkdir -p var
uvx --from uv==0.13.9 uv run --locked python scripts/setup_demo_env.py --output var/compose-v2.env --port 18081
docker compose --env-file var/compose-v2.env -p casechamp-d2-v2 \
  -f compose.yaml -f compose.v2.yaml -f compose.performance.yaml up -d --build --wait
```

Swagger доступен по адресу `http://localhost:18081/api/v2/docs`. `GET /api/v2/health` проверяет миграцию, БД и heartbeat worker. Для запуска клиента D1 положите его сборку в `apps/web/dist` или укажите `CASECHAMP_WEB_DIST`. Прокси обслуживает сайт и API с одного origin. Конфигурация внешнего HTTPS описана в основном README.

При прямом запуске укажите `DATABASE_URL`, `MIGRATION_DATABASE_URL`, `SESSION_SECRET` и `WEB_ORIGIN`. Рабочий worker получает `MAINTENANCE_DATABASE_URL` для удаления истёкших песочниц по окончании срока receipts. `APP_MODE=test` используется тестами вместе с внедряемым Python Clock.

```sh
uvx --from uv==0.13.9 uv run --locked alembic -c alembic.v2.ini upgrade head
uvx --from uv==0.13.9 uv run --locked uvicorn app.v2.main:create_app --factory --no-proxy-headers --no-access-log
uvx --from uv==0.13.9 uv run --locked python -m app.v2.worker
```

## Контракт и транзакции

- Машиночитаемые файлы v2 лежат в [packages/contracts_v2](../packages/contracts_v2/openapi.json). Использованы строгие DTO Pydantic, сгенерированные из OpenAPI, и проверка исходных JSON-типов. Контракт и точки запуска v1 сохранены.
- OWNER управляет каталогом и приглашениями, OPERATOR обрабатывает предложения и заказы, BUYER видит свои коммерческие ресурсы. Для приглашений используются отдельные контексты браузера. HMAC, HttpOnly cookie, Origin и CSRF проверяются сервером. Повтор успешного обмена возвращает исходную сессию и срок.
- Публикация фиксирует снимок и хеш утверждённых условий. Старый резерв сохраняет цену после изменения каталога или новой публикации. Pause и archive освобождают активные резервы всех версий предложения.
- Изменения сериализуются по песочнице. Истёкшие резервы освобождаются до savepoint команды. При бизнес-отказе применяются истечения, а эффект команды откатывается. Receipt успешной команды и outbox коммитятся вместе с эффектом.
- P1 делает точный лексикографический поиск со строгим лимитом 2 секунды. Снимок читается и перепроверяется под mutex, поиск выполняется вне транзакции. Изменившийся остаток позволяет один пересчёт. Набор проверок сравнивает результат с независимым полным перебором.

Основной seed CORE-001 и B001 взяты из поставки v2. UUID шаблона и ресурсов создаются заново для каждой песочницы. При reset OWNER сохраняет свою сессию и исторические receipts, прежние BUYER и OPERATOR теряют доступ. Для v2 выбрана отдельная инсталляция схемы по карте перехода из ТЗ.

## Проверки

```sh
uvx --from uv==0.13.9 uv run --locked pytest -q tests/v2
uvx --from uv==0.13.9 uv run --locked python scripts/smoke_v2.py --output reports/v2-compose-smoke.json
uvx --from uv==0.13.9 uv run --locked python scripts/measure_v2.py --env-file var/compose-v2.env
```

Pytest поднимает временную PostgreSQL 17 на UNIX-сокете, если не задан `TEST_DATABASE_URL` выделенной тестовой БД. Нагрузочный скрипт добавляет отдельную песочницу со своим синтетическим набором, включая предельный шаблон P1. Стандартный B001 остаётся доступным в обычном bootstrap. Сведения о фактически выполненных проверках лежат в [отчёте v2](../reports/V2.md).
