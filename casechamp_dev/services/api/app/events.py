import base64
import binascii
import json
from datetime import datetime
from uuid import UUID, uuid4

from app.common import Problem, canonical, field_error, iso, truncate


def emit(scope, name, aggregate, identity, version, facts):
    event_id = uuid4()
    try:
        resource_id = str(UUID(str(identity)))
    except ValueError:
        resource_id = None
    scope.insert(
        "outbox_events",
        event_id=event_id,
        aggregate_type=aggregate,
        aggregate_id=str(identity),
        aggregate_version=version,
        event_name=name,
        occurred_at=scope.now,
        publish_status="PENDING",
        next_attempt_at=scope.now,
        payload={
            "correlation_id": scope.request_id,
            "client_run_id": scope.client_run_id,
            "resource_id": resource_id,
            "facts": facts,
        },
    )
    return event_id


def audit(scope, operation, outcome, identity=None, facts=None):
    try:
        resource_id = UUID(str(identity)) if identity else None
    except ValueError:
        resource_id = None
    scope.insert(
        "audit_log",
        request_id=scope.request_id,
        client_run_id=scope.client_run_id,
        operation_id=operation,
        outcome_code=outcome,
        demo_role="SYSTEM",
        resource_id=resource_id,
        occurred_at=scope.now,
        facts=facts or {},
    )


def record(scope, entries):
    resources = {
        "scenario_viewed": ("scenario_revisions", "scenario_revision_id"),
        "request_form_started": ("scenario_revisions", "scenario_revision_id"),
        "proposal_viewed": ("quotes", "quote_id"),
        "checkout_viewed": ("reservations", "reservation_id"),
    }
    for i, entry in enumerate(entries):
        identity = entry["resource_id"]
        if identity:
            table, name = resources[entry["event_name"]]
            if not scope.scoped(table, **{name: identity}):
                raise Problem("RESOURCE_NOT_FOUND", fields=[field_error(f"events[{i}].resource_id")])
    for entry in entries:
        occurred = datetime.fromisoformat(entry["occurred_at"])
        if abs((occurred - scope.now).total_seconds()) > 86400:
            occurred = scope.now
        scope.db.insert(
            "client_events",
            scope.keys | entry | {"occurred_at": truncate(occurred), "received_at": scope.now},
            conflict="ON CONFLICT DO NOTHING",
        )
    return {"resource_id": str(uuid4()), "status": "RECORDED", "invalidated_reservations": 0}


def parse_cursor(value):
    if value is None:
        return None
    try:
        value += "=" * (-len(value) % 4)
        stamp, eid, *extra = json.loads(base64.b64decode(value, altchars=b"-_", validate=True))
        date = datetime.fromisoformat(stamp)
        source = extra[0] if len(extra) == 1 else "SERVER" if not extra else None
        if date.tzinfo is None or source not in ("CLIENT", "SERVER"):
            raise ValueError
        return date, UUID(eid), source
    except (ValueError, TypeError, OverflowError, binascii.Error):
        raise Problem("VALIDATION_ERROR", fields=[field_error("cursor")]) from None


def list_events(scope, limit, cursor):
    after = parse_cursor(cursor)
    predicates = ""
    args = ()
    if after:
        predicates = "WHERE (occurred_at,event_id,source) > (%s,%s,%s)"
        args = after
    rows = scope.db.all(
        f"""SELECT * FROM (
             SELECT event_id,event_name,occurred_at,'SERVER' AS source,
                    payload->>'correlation_id' AS correlation_id,payload->>'client_run_id' AS client_run_id,
                    payload->>'resource_id' AS resource_id,payload->'facts' AS facts
             FROM vk_demo.event_projection WHERE sandbox_id=%s AND epoch=%s
             UNION ALL
             SELECT event_id,event_name,occurred_at,'CLIENT',event_id::text,client_run_id::text,resource_id::text,
                    '{{}}'::jsonb FROM vk_demo.client_events WHERE sandbox_id=%s AND epoch=%s
             ) AS events {predicates} ORDER BY occurred_at,event_id,source LIMIT %s""",
        scope.sandbox_id,
        scope.epoch,
        scope.sandbox_id,
        scope.epoch,
        *args,
        limit + 1,
    )
    items = []
    for row in rows[:limit]:
        facts = row.pop("facts")
        value = facts.get("total_minor")
        row["properties"] = {"code": facts.get("reason_code"), "value": value, "duration_ms": None}
        row["occurred_at"] = iso(row["occurred_at"])
        row["event_id"] = str(row["event_id"])
        items.append(row)
    next_cursor = None
    if len(rows) > limit:
        last = items[-1]
        next_cursor = (
            base64.urlsafe_b64encode(canonical([last["occurred_at"], last["event_id"], last["source"]]))
            .decode()
            .rstrip("=")
        )
    return {"items": items, "next_cursor": next_cursor}


def metrics(scope):
    arg = (scope.sandbox_id, scope.epoch)
    outcomes = scope.db.all(
        """SELECT result_code,count(*) AS count FROM vk_demo.solve_runs
           WHERE sandbox_id=%s AND epoch=%s GROUP BY result_code""",
        *arg,
    )
    counts = {row["result_code"]: row["count"] for row in outcomes}
    orders = scope.db.first(
        """SELECT count(*) confirmed, count(*) FILTER (WHERE status='CANCELED') canceled,
           COALESCE(sum(total_minor),0)::bigint gross,
           COALESCE(sum(total_minor) FILTER (WHERE status='CANCELED'),0)::bigint canceled_value
           FROM vk_demo.orders WHERE sandbox_id=%s AND epoch=%s""",
        *arg,
    )
    views = scope.db.first(
        """SELECT count(*) count FROM vk_demo.client_events WHERE sandbox_id=%s AND epoch=%s
           AND event_name='scenario_viewed'""",
        *arg,
    )
    reserves = scope.db.first(
        """SELECT count(*) count FROM vk_demo.reservations WHERE sandbox_id=%s AND epoch=%s""",
        *arg,
    )
    if max(orders["gross"], orders["canceled_value"]) > 2**53 - 1:
        raise Problem("INTERNAL_INVARIANT_FAILED")
    return {
        "is_test": True,
        "scenario_views": views["count"],
        "solve_requests": sum(counts.values()),
        "quoted": counts.get("QUOTED", 0),
        "infeasible": counts.get("INFEASIBLE", 0),
        "no_purchase_required": counts.get("NO_PURCHASE_REQUIRED", 0),
        "reserved": reserves["count"],
        "test_orders_confirmed": orders["confirmed"],
        "test_orders_canceled": orders["canceled"],
        "net_active_test_orders": orders["confirmed"] - orders["canceled"],
        "gross_test_order_value_minor": orders["gross"],
        "canceled_test_order_value_minor": orders["canceled_value"],
    }
