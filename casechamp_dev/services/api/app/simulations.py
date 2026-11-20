import json
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import datetime, timedelta
from threading import Barrier
from uuid import uuid4

from app import catalog, commerce, events, scenarios
from app.common import REQUIRED, SLOTS, Problem, Result
from app.db import DB

SIM_SKU = "SIM_DINNER_PLATE"


def recover(scope, lease):
    if lease["expires_at"] > scope.now or not lease["recovery"]:
        return
    facts = lease["recovery"]
    if facts["epoch"] == scope.epoch:
        quotes = scope.db.all(
            """SELECT * FROM vk_demo.quotes WHERE sandbox_id=%s AND epoch=%s AND scenario_revision_id=%s
               AND status IN ('ACTIVE','RESERVED') ORDER BY quote_id""",
            scope.sandbox_id,
            scope.epoch,
            facts["revision"],
        )
        commerce.invalidate(scope, quotes, "SIMULATION_INTERRUPTED", events.emit)
        _stock(scope, SIM_SKU, facts["stock"])
        _price(scope, SIM_SKU, facts["price"])
    scope.db.update("simulation_locks", {"recovery": {}}, {"sandbox_id": scope.sandbox_id})


def _stock(scope, sku, value):
    inventory = catalog.lock_inventory(scope, [sku])[sku]
    catalog.check_stock(inventory, value)
    catalog.change_inventory(scope, inventory, on_hand=value - inventory["stock_on_hand_packs"])


def _price(scope, sku, value):
    product = scope.require("products", sku_id=sku)
    catalog.update_product(scope, product, {"price_minor": value})


def _request(revision):
    return {
        "scenario_revision_id": revision,
        "guest_count": 1,
        "budget_minor": 5_000_000,
        "currency": "RUB",
        "fulfillment": {"mode": "PICKUP", "zone": None},
        "needed_by": None,
        "table_dimensions": None,
        "client_run_id": str(uuid4()),
        "slots": [
            {
                "slot_id": slot,
                "enabled": slot in REQUIRED,
                "owned_units": 0,
                "selection_mode": "EXACT_ONLY",
                "allowed_colors": [],
                "preferred_color": None,
            }
            for slot in SLOTS
        ],
    }


def reservation_body(quote):
    return {"quote_id": quote["quote_id"], "accepted_quote_hash": quote["quote_hash"]}


def run(service, command):
    execution_id = uuid4()
    originals = {}
    with service.engine.begin() as connection:
        scope, wait = service.authenticate(DB(connection), command, lock=True, check_simulation=False)
        replay = service.receipt(scope, command)
        if replay:
            return replay, scope, wait
        scope.require("scenario_revisions", scenario_revision_id=command.body["scenario_revision_id"])
        commerce.sweep(scope, events.emit)
        lease = scope.db.first("SELECT * FROM vk_demo.simulation_locks WHERE sandbox_id=%s", scope.sandbox_id)
        if lease:
            recover(scope, lease)
        active = scope.scoped("reservations", status="ACTIVE")
        if active:
            raise Problem("INVALID_STATE", reasons=["ACTIVE_RESERVATIONS_PRESENT"])
        if service.simulation_active(scope):
            raise Problem("STORE_BUSY", reasons=["SIMULATION_RUNNING"], retry=True)
        scope.db.run(
            """INSERT INTO vk_demo.simulation_locks (sandbox_id,execution_id,expires_at) VALUES (%s,%s,%s)
               ON CONFLICT (sandbox_id) DO UPDATE
               SET execution_id=EXCLUDED.execution_id,expires_at=EXCLUDED.expires_at""",
            scope.sandbox_id,
            execution_id,
            scope.now + timedelta(seconds=60),
        )

    assertions, trace, held = [], [], []
    fixture = service.settings.fixture_root / "snapshot.json"
    seed = json.loads(fixture.read_text())
    sim_sku = SIM_SKU

    def step(action, *, simulation_clock=None):
        with service.engine.begin() as connection:
            step_scope, _ = service.authenticate(DB(connection), command, lock=True, check_simulation=False)
            lease = step_scope.db.first(
                "SELECT * FROM vk_demo.simulation_locks WHERE sandbox_id=%s", step_scope.sandbox_id
            )
            if not lease or lease["execution_id"] != execution_id or lease["expires_at"] <= step_scope.now:
                raise Problem("STORE_BUSY", reasons=["SIMULATION_LEASE_LOST"], retry=True)
            if simulation_clock is not None:
                step_scope = replace(step_scope, now=simulation_clock)
            commerce.sweep(step_scope, events.emit)
            try:
                return action(step_scope)
            except Problem as problem:
                if problem.code == "INTERNAL_INVARIANT_FAILED":
                    raise
                return problem

    def check(identity, passed, message):
        assertions.append({"id": identity, "passed": bool(passed), "message": message})

    def setup(scope):
        slots = []
        for mapping in seed["template_slots"]:
            slot = mapping["slot_id"]
            sku = f"SIM_{slot.upper()}"
            if not scope.scoped("products", sku_id=sku):
                if len(catalog.list_products(scope)) >= 500:
                    raise Problem("CATALOG_LIMIT_EXCEEDED")
                product = next(row for row in seed["catalog"] if row["sku_id"] == mapping["primary_sku_id"])
                catalog.new_product(
                    scope, product | {"sku_id": sku, "stock_on_hand_packs": 12, "image_asset_id": None}
                )
            slots.append(mapping | {"primary_sku_id": sku, "alternative_sku_ids": []})
        originals["stock"] = scope.require("inventory", sku_id=sim_sku)["stock_on_hand_packs"]
        originals["price"] = scope.require("products", sku_id=sim_sku)["price_minor"]
        created = scenarios.create_scenario(
            scope, {"title": "Симуляция", "caption": "", "asset_id": None}, slots
        )
        revision = scenarios.publish(scope, created["scenario_id"], 1, events.emit, simulation=True)
        scope.db.update(
            "simulation_locks",
            {"recovery": originals | {"revision": revision["scenario_revision_id"], "epoch": scope.epoch}},
            {"sandbox_id": scope.sandbox_id, "execution_id": execution_id},
        )
        return revision["scenario_revision_id"]

    def quote(revision):
        with service.engine.begin() as connection:
            db = DB(connection)
            db.run("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY")
            snapshot_scope, _ = service.authenticate(db, command)
            intent, snapshot = commerce.read_snapshot(snapshot_scope, _request(revision))
        plan = service.plan(snapshot, intent)
        result = step(
            lambda s: commerce.save_solve(
                s,
                intent,
                snapshot,
                plan,
                snapshot_scope.now,
                service.settings.solver_config_version,
                events.emit,
            )
        )
        if isinstance(result, Problem) or not result["quote"]:
            raise RuntimeError("simulation could not prepare quotation")
        trace.append(result["quote"]["quote_id"])
        return result["quote"]

    def reserve(terms):
        result = step(lambda s: commerce.reserve(s, reservation_body(terms), events.emit))
        if not isinstance(result, Problem):
            held.append(result["reservation_id"])
            trace.append(result["reservation_id"])
        return result

    key = command.body["case_id"]
    try:
        revision = step(setup)
        if isinstance(revision, Problem):
            raise revision
        trace.append(revision)
        if key == "LAST_STOCK_RACE":
            step(lambda s: _stock(s, sim_sku, 1))
            a, b = quote(revision), quote(revision)
            gate = Barrier(2)

            def compete(terms):
                gate.wait(timeout=3)
                return reserve(terms)

            with ThreadPoolExecutor(max_workers=2) as pool:
                results = list(pool.map(compete, (a, b)))
            winners = [result for result in results if not isinstance(result, Problem)]
            losers = [result.code for result in results if isinstance(result, Problem)]
            inv = step(lambda s: s.require("inventory", sku_id=sim_sku))
            check(
                "ONE_WINNER",
                len(winners) == 1 and losers == ["STOCK_CHANGED"],
                "Один резерв, второй запрос получил STOCK_CHANGED.",
            )
            check(
                "LAST_PACK",
                inv["reserved_packs"] == inv["stock_on_hand_packs"] == 1,
                "Последняя упаковка удержана один раз.",
            )
        elif key == "PRICE_CHANGE":
            old = quote(revision)
            step(lambda s: _price(s, sim_sku, originals["price"] + 100))
            stale = reserve(old)
            check(
                "STALE",
                isinstance(stale, Problem) and stale.code == "QUOTE_STALE",
                "Старое предложение отклонено после изменения цены.",
            )
            fresh = quote(revision)
            reservation = reserve(fresh)
            if isinstance(reservation, Problem):
                raise reservation
            step(lambda s: _price(s, sim_sku, originals["price"] + 200))
            saved = step(lambda s: s.require("reservations", reservation_id=reservation["reservation_id"]))
            check(
                "HELD_PRICE",
                saved["immutable_terms"]["total_minor"] == fresh["total_minor"]
                and saved["status"] == "ACTIVE",
                "Активный резерв сохранил согласованные условия.",
            )
        else:
            terms = quote(revision)
            reservation = reserve(terms)
            if isinstance(reservation, Problem):
                raise reservation
            deadline = datetime.fromisoformat(reservation["expires_at"])
            payload = {
                "reservation_id": reservation["reservation_id"],
                "accepted_quote_hash": terms["quote_hash"],
            }
            failed = step(lambda s: commerce.confirm(s, payload, events.emit), simulation_clock=deadline)
            check(
                "BOUNDARY",
                isinstance(failed, Problem) and failed.code == "RESERVATION_EXPIRED",
                "Ровно в момент истечения подтверждение отклонено.",
            )
            before, after = (step(lambda s: s.require("inventory", sku_id=sim_sku)) for _ in range(2))
            check(
                "RELEASE_ONCE",
                before["reserved_packs"] == after["reserved_packs"] == 0
                and before["inventory_version"] == after["inventory_version"],
                "Резерв снят ровно один раз.",
            )
    except (Problem, RuntimeError):
        check("EXECUTION", False, "Симуляция не выполнила все операции.")
    finally:

        def cleanup(scope):
            for reservation_id in held:
                commerce.cancel_reservation(scope, reservation_id, events.emit)
            if originals:
                _stock(scope, sim_sku, originals["stock"])
                _price(scope, sim_sku, originals["price"])
                inventory = scope.require("inventory", sku_id=sim_sku)
                check(
                    "RESTORED",
                    inventory["reserved_packs"] == 0
                    and inventory["stock_on_hand_packs"] == originals["stock"],
                    "Исходный демонстрационный остаток восстановлен.",
                )
            result = Result(
                {
                    "case_id": key,
                    "passed": bool(assertions) and all(item["passed"] for item in assertions),
                    "assertions": assertions,
                    "trace_resource_ids": trace,
                    "test_execution_id": str(execution_id),
                },
                resource_id=execution_id,
            )
            service.save_receipt(scope, command, result)
            events.audit(
                scope, command.operation, "PASSED" if result.body["passed"] else "FAILED", execution_id
            )
            scope.db.update(
                "simulation_locks",
                {"expires_at": scope.now, "recovery": {}},
                {"sandbox_id": scope.sandbox_id, "execution_id": execution_id},
            )
            return result

        result = step(cleanup)
    if isinstance(result, Problem):
        raise result
    return result, scope, wait
