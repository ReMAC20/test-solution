from __future__ import annotations

import json
from typing import Iterable

from sqlalchemy import BigInteger, Column, MetaData, Table, Text
from sqlalchemy.dialects.postgresql import JSONB, UUID, insert as pg_insert

from models import db

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
    if batch_size < 1:
        raise ValueError("batch_size должен быть положительным")
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
