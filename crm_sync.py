from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Iterable
from uuid import uuid4

from sqlalchemy import BigInteger, Column, MetaData, Table, Text, text
from sqlalchemy.dialects.postgresql import JSONB, UUID, insert as pg_insert

from models import db
from batch_apply import apply_snapshot

LOG = logging.getLogger(__name__)
BATCH_SIZE = 250
_METADATA = MetaData()

_CUSTOMERS = Table(
    "crm_sync_customers", _METADATA,
    Column("run_id", UUID(as_uuid=True)),
    Column("crm_id", BigInteger),
    Column("login", Text),
    Column("payload", JSONB),
    Column("contacts", JSONB),
    Column("companies", JSONB),
)
_LEADS = Table(
    "crm_sync_leads", _METADATA,
    Column("run_id", UUID(as_uuid=True)),
    Column("crm_id", BigInteger),
    Column("login", Text),
    Column("payload", JSONB),
    Column("contacts", JSONB),
)


def json_compatible(value):
    return json.loads(json.dumps(value, ensure_ascii=False, default=_serialize_special))


def _serialize_special(value):

    from datetime import date, datetime
    from decimal import Decimal
    from uuid import UUID as UUIDType

    if isinstance(value, (date, datetime)):
        return value.isoformat()
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, UUIDType):
        return str(value)
    raise TypeError("CRM payload contains an unserializable value: " + type(value).__name__)


def _entity_json(entity):
    if callable(getattr(entity, "as_dict", None)):
        return json_compatible(entity.as_dict())
    return json_compatible({
        key: value for key, value in vars(entity).items()
        if key != "_new_next_date" and not callable(value)
    })


def _field(entity, name):
    values = getattr(entity, "custom_fields_values", None) or {}
    item = values.get(name) or {}
    return item.get("value")


def _links(items):
    result = []
    for item in items or []:
        if not isinstance(item, dict) or item.get("id") is None:
            raise ValueError("CRM embedded item must be a dict with an id")
        result.append({
            "general": {"id": int(item["id"]), "name": item.get("name")},
            "api_value": json_compatible(item),
        })
    return result


def customer_row(customer, run_id):

    login = _field(customer, "Логин")
    if not login:
        return None
    crm_id = int(customer.id)
    return {
        "run_id": run_id,
        "crm_id": crm_id,
        "login": str(login),
        "payload": {
            "general": {
                "id": crm_id,
                "login": str(login),
                "billing_type": _field(customer, "Вид оплаты"),
                "is_robot": bool(_field(customer, "Робот")),
                "is_nds": bool(_field(customer, "Оплата с НДС")),
                "system": _field(customer, "Система"),
                "host": _field(customer, "Хост"),
                "name": getattr(customer, "name", None),
            },
            "api_value": _entity_json(customer),
        },
        "contacts": _links(getattr(customer, "contacts", None) or (getattr(customer, "_embedded", None) or {}).get("contacts", [])),
        "companies": _links(getattr(customer, "companies", None) or (getattr(customer, "_embedded", None) or {}).get("companies", [])),
    }


def lead_row(lead, run_id):
    crm_id = int(lead.id)
    login = _field(lead, "Логин")
    return {
        "run_id": run_id,
        "crm_id": crm_id,
        "login": str(login) if login else None,
        "payload": {
            "general": {
                "id": crm_id,
                "login": str(login) if login else None,
                "name": getattr(lead, "name", None),
            },
            "api_value": _entity_json(lead),
        },
        "contacts": _links(getattr(lead, "contacts", None) or (getattr(lead, "_embedded", None) or {}).get("contacts", [])),
    }


def _insert_batch(table, rows):
    if not rows:
        return
    rows = list({
        (row["run_id"], row["crm_id"]): row
        for row in rows
    }.values())
    stmt = pg_insert(table).values(rows)
    changes = {
        col: getattr(stmt.excluded, col)
        for col in table.c.keys() if col not in {"run_id", "crm_id"}
    }
    stmt = stmt.on_conflict_do_update(
        index_elements=[table.c.run_id, table.c.crm_id], set_=changes,
    )
    with db.engine.begin() as connection:
        connection.execute(stmt)


def _stage(iterator: Iterable, table, converter, run_id, batch_size, is_full, skipped_status):
    batch = []
    count = 0
    for entity in iterator:
        if not is_full and getattr(entity, "status_id", None) == skipped_status:
            continue
        row = converter(entity, run_id)
        if row is None:
            continue
        batch.append(row)
        count += 1
        if len(batch) >= batch_size:
            _insert_batch(table, batch)
            batch.clear()
    _insert_batch(table, batch)
    return count


def sync_crm(
    customers: Iterable,
    leads: Iterable,
    *,
    is_full: bool = True,
    batch_size: int = BATCH_SIZE,
    allow_empty_full: bool = False,
):
    if batch_size < 1:
        raise ValueError("batch_size must be positive")
    run_id = uuid4()
    with db.engine.begin() as connection:
        connection.execute(
            text("INSERT INTO crm_sync_runs(run_id, is_full) VALUES (:run_id, :is_full)"),
            {"run_id": run_id, "is_full": is_full},
        )

    try:
        customer_count = _stage(
            customers, _CUSTOMERS, customer_row, run_id, batch_size, is_full, 111349,
        )
        lead_count = _stage(
            leads, _LEADS, lead_row, run_id, batch_size, is_full, 143,
        )
        with db.engine.begin() as connection:
            connection.execute(
                text("""UPDATE crm_sync_runs
                        SET status = 'ready', customers_count = :customers,
                            leads_count = :leads WHERE run_id = :run_id
                            AND status = 'loading'"""),
                {"run_id": run_id, "customers": customer_count, "leads": lead_count},
            )
    except Exception:
        LOG.exception("CRM collection failed; staging run %s will not be applied", run_id)

        with db.engine.begin() as connection:
            connection.execute(
                text("UPDATE crm_sync_runs SET status='failed' WHERE run_id=:run_id"),
                {"run_id": run_id},
            )
        raise

    try:
        with db.engine.begin() as connection:
            stats = apply_snapshot(
                connection, run_id, allow_empty_full=allow_empty_full,
                batch_size=batch_size,
            )

        db.session.expire_all()
        LOG.info("CRM run %s completed: %s", run_id, stats)
        return {"run_id": str(run_id), "stats": stats}
    except Exception:

        LOG.exception("CRM apply failed; run %s is still staged for inspection", run_id)
        raise


def install_schema(engine):
    location = Path(__file__).resolve().parent
    schema_text = (location / "schema.sql").read_text(encoding="utf-8")
    schema_text = "\n".join(
        line for line in schema_text.splitlines()
        if not line.lstrip().startswith("--")
    )
    with engine.begin() as conn:
        for statement in schema_text.split(";"):
            if statement.strip():
                conn.exec_driver_sql(statement)

