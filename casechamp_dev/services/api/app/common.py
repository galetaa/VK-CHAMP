import hashlib
import json
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta, timezone
from uuid import UUID
from zoneinfo import ZoneInfo

MOSCOW = ZoneInfo("Europe/Moscow")
SLOTS = ("dinner_plate", "dessert_plate", "drinking_glass", "cutlery_set", "napkin", "tablecloth")
REQUIRED = frozenset(("dinner_plate", "drinking_glass", "cutlery_set", "napkin"))
HASH_VERSION = "1.0.0"


def truncate(value: datetime) -> datetime:
    return value.astimezone(timezone.utc).replace(microsecond=value.microsecond // 1000 * 1000)


def iso(value: datetime | date | None) -> str | None:
    if isinstance(value, datetime):
        return truncate(value).isoformat(timespec="milliseconds").replace("+00:00", "Z")
    return value.isoformat() if value is not None else None


def json_default(value):
    if isinstance(value, UUID):
        return str(value)
    if isinstance(value, (datetime, date)):
        return iso(value)
    raise TypeError(type(value).__name__)


def canonical(value) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
        default=json_default,
    ).encode("utf-8")


def digest(value) -> str:
    return hashlib.sha256(canonical(value)).hexdigest()


def business_date(now: datetime) -> date:
    return now.astimezone(MOSCOW).date()


def expiry(now: datetime, ttl: int, needed_by: str | None, lead: int) -> datetime:
    deadline = now + timedelta(seconds=ttl)
    if needed_by is None:
        return deadline
    cap_day = date.fromisoformat(needed_by) + timedelta(days=1 - lead)
    cap = datetime.combine(cap_day, time.min, MOSCOW).astimezone(timezone.utc)
    return min(deadline, cap)


MESSAGES = {
    "MALFORMED_JSON": "Некорректный JSON.",
    "ORIGIN_NOT_ALLOWED": "Источник запроса не разрешён.",
    "DEMO_SESSION_REQUIRED": "Откройте демонстрационную сессию.",
    "DEMO_SESSION_EXPIRED": "Сессия завершилась. Откройте демонстрацию заново.",
    "RESOURCE_NOT_FOUND": "Ресурс не найден.",
    "SANDBOX_EPOCH_MISMATCH": "Демонстрация была сброшена. Обновите страницу.",
    "INVALID_STATE": "Действие недоступно в текущем состоянии.",
    "IDEMPOTENCY_KEY_REUSED": "Ключ уже использован для другой команды.",
    "IDEMPOTENCY_IN_PROGRESS": "Команда ещё выполняется.",
    "QUOTE_STALE": "Условия изменились. Пересчитайте набор.",
    "STOCK_CHANGED": "Количество товаров в наличии изменилось.",
    "ACCEPTED_QUOTE_HASH_MISMATCH": "Подтвердите актуальные условия предложения.",
    "TEMPLATE_WITHDRAWN": "Публикация отозвана.",
    "PRODUCT_WITHDRAWN": "Товар снят с продажи.",
    "MAPPING_UNCONFIRMED": "Подтвердите привязки товаров.",
    "CONCURRENT_VERSION_CHANGE": "Данные изменились. Обновите страницу.",
    "STOCK_BELOW_RESERVED": "Остаток не может быть ниже зарезервированного количества.",
    "IMPORT_NOT_VALID": "Импорт не готов к применению.",
    "CATALOG_LIMIT_EXCEEDED": "В каталоге может быть до 500 товаров.",
    "QUOTE_EXPIRED": "Срок предложения истёк. Пересчитайте набор.",
    "RESERVATION_EXPIRED": "Срок резерва истёк. Подберите набор заново.",
    "IMPORT_EXPIRED": "Проверка файла устарела. Проверьте файл повторно.",
    "PAYLOAD_TOO_LARGE": "Файл или запрос слишком большой.",
    "UNSUPPORTED_MEDIA_TYPE": "Формат запроса или файла не поддерживается.",
    "VALIDATION_ERROR": "Проверьте значения полей.",
    "CONTRADICTORY_PREFERENCES": "Предпочитаемый цвет должен быть среди разрешённых.",
    "UNSUPPORTED_ZONE": "Доставка в эту зону недоступна.",
    "IMPORT_VALIDATION_FAILED": "Проверьте строки файла.",
    "INVALID_MEDIA": "Медиафайл повреждён или превышает ограничения.",
    "MAPPING_INVALID": "Проверьте привязки товаров к слотам.",
    "RATE_LIMITED": "Слишком много запросов. Попробуйте позже.",
    "COMPUTATION_LIMIT": "Не удалось завершить точный подбор в лимите.",
    "COMPUTATION_ABORTED": "Подбор прерван. Попробуйте ещё раз.",
    "STORE_BUSY": "Хранилище занято. Повторите запрос с тем же ключом.",
    "ML_UNAVAILABLE": "Автоматический разбор недоступен. Заполните форму вручную.",
    "INTERNAL_INVARIANT_FAILED": "Операцию завершить не удалось.",
}


class Problem(Exception):
    def __init__(self, code, *, reasons=(), fields=(), refresh=False, retry=False, retry_ms=None):
        super().__init__(code)
        self.code = code
        self.reasons = list(reasons)
        self.fields = list(fields)
        self.refresh = refresh
        self.retry = retry
        self.retry_ms = retry_ms

    def body(self):
        return {
            "code": self.code,
            "message": MESSAGES[self.code],
            "field_errors": self.fields,
            "details": {
                "reason_codes": self.reasons,
                "refresh_required": self.refresh,
                "retryable": self.retry,
                "retry_after_ms": self.retry_ms,
            },
        }


def field_error(path, code="VALIDATION_ERROR", message=None):
    return {
        "path": path,
        "code": code,
        "message": message or MESSAGES.get(code, MESSAGES["VALIDATION_ERROR"]),
    }


@dataclass
class Result:
    body: dict
    status: int = 200
    resource_id: UUID | str | None = None
    replay: bool = False
    headers: dict = field(default_factory=dict)

    @classmethod
    def error(cls, problem, statuses):
        headers = {}
        if problem.retry_ms is not None:
            headers["Retry-After"] = str(max(1, (problem.retry_ms + 999) // 1000))
        return cls(problem.body(), statuses[problem.code], headers=headers)
