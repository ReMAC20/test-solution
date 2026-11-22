"""Обрабатывает следующий случай: объединение двух AbstractCustomer.

Обычное сопоставление и обновление выполняются пакетами в batch_apply.py.
Только конфликт (сделка и покупатель в разных строках) требует чтения
двух полноценных JSONB-записей и их слияния.
"""

import json

from sqlalchemy import text


def merge_items(source, target):
    """Сохранить элементы обоих массивов; для совпадающего general.id побеждает target.

    Данные без general.id уникализируются по полному JSON-значению.
    """
    result = {}
    for item in (source or []) + (target or []):
        key_id = (item.get("general") or {}).get("id") if isinstance(item, dict) else None
        key = ("id", str(key_id)) if key_id is not None else (
            "json", json.dumps(item, sort_keys=True, ensure_ascii=False)
        )
        result[key] = item
    return list(result.values())


def merge_owners(connection, target, source):
    """В одной транзакции перенести source в target и удалить source.

    Работа без commit, границы транзакции определяются в crm_sync.sync_crm().
    """
    if target == source:
        return
    rows = connection.execute(text("""
        SELECT id, amo_customers, amo_leads, amo_contacts,
               amo_companies, send_methods
        FROM abstract_customer
        WHERE id IN (:target, :source)
        ORDER BY id FOR UPDATE
    """), {"target": target, "source": source}).mappings().all()
    data = {row["id"]: row for row in rows}
    if set(data) != {target, source}:
        raise ValueError("Cannot merge: an AbstractCustomer is missing")

    fields = ("amo_customers", "amo_leads", "amo_contacts", "amo_companies", "send_methods")
    merged = {field: merge_items(data[source][field], data[target][field]) or None for field in fields}
    connection.execute(text("""
        UPDATE abstract_customer SET
            amo_customers = CAST(:customers AS jsonb),
            amo_leads = CAST(:leads AS jsonb),
            amo_contacts = CAST(:contacts AS jsonb),
            amo_companies = CAST(:companies AS jsonb),
            send_methods = CAST(:send_methods AS jsonb),
            updated_at = NOW()
        WHERE id = :target
    """), {
        "target": target,
        "customers": json.dumps(merged["amo_customers"], ensure_ascii=False),
        "leads": json.dumps(merged["amo_leads"], ensure_ascii=False),
        "contacts": json.dumps(merged["amo_contacts"], ensure_ascii=False),
        "companies": json.dumps(merged["amo_companies"], ensure_ascii=False),
        "send_methods": json.dumps(merged["send_methods"], ensure_ascii=False),
    })
    connection.execute(text("DELETE FROM abstract_customer WHERE id = :source"), {"source": source})

    # Временные поисковые таблицы обновляются сразу
    connection.execute(text("UPDATE _members SET owner_id = :target WHERE owner_id = :source"),
                       {"target": target, "source": source})
    connection.execute(text("UPDATE _customer_targets SET owner_id = :target WHERE owner_id = :source"),
                       {"target": target, "source": source})
