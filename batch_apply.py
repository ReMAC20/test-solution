from sqlalchemy import text
from merge import merge_owners


_COLUMNS = {
    "customer": "amo_customers",
    "lead": "amo_leads",
    "contact": "amo_contacts",
    "company": "amo_companies",
}


def _create_work_tables(connection):
    connection.exec_driver_sql("""
        CREATE TEMP TABLE _members (
            kind TEXT NOT NULL,
            crm_id BIGINT NOT NULL,
            login TEXT,
            owner_id BIGINT NOT NULL
        ) ON COMMIT DROP
    """)
    connection.exec_driver_sql("CREATE INDEX ON _members(kind, crm_id)")
    connection.exec_driver_sql("CREATE INDEX ON _members(kind, login)")
    connection.exec_driver_sql("""
        CREATE TEMP TABLE _customer_targets (
            crm_id BIGINT PRIMARY KEY,
            owner_id BIGINT NOT NULL
        ) ON COMMIT DROP
    """)
    connection.exec_driver_sql("""
        CREATE TEMP TABLE _lead_targets (
            crm_id BIGINT PRIMARY KEY,
            owner_id BIGINT NOT NULL
        ) ON COMMIT DROP
    """)
    connection.exec_driver_sql("""
        CREATE TEMP TABLE _incoming (
            kind TEXT NOT NULL,
            owner_id BIGINT NOT NULL,
            crm_id BIGINT NOT NULL,
            payload JSONB NOT NULL,
            PRIMARY KEY (kind, owner_id, crm_id)
        ) ON COMMIT DROP
    """)
    connection.exec_driver_sql("CREATE INDEX ON _incoming(kind, owner_id)")


def _refresh_members(connection):
    connection.execute(text("TRUNCATE _members"))
    connection.execute(text("""
        INSERT INTO _members(kind, crm_id, login, owner_id)
        SELECT DISTINCT 'customer',
               (elem.value #>> '{general,id}')::bigint,
               elem.value #>> '{general,login}', ac.id
        FROM abstract_customer AS ac
        CROSS JOIN LATERAL jsonb_array_elements(
            COALESCE(ac.amo_customers, '[]'::jsonb)
        ) AS elem(value)
        WHERE elem.value #>> '{general,id}' IS NOT NULL
        UNION
        SELECT DISTINCT 'lead',
               (elem.value #>> '{general,id}')::bigint,
               elem.value #>> '{general,login}', ac.id
        FROM abstract_customer AS ac
        CROSS JOIN LATERAL jsonb_array_elements(
            COALESCE(ac.amo_leads, '[]'::jsonb)
        ) AS elem(value)
        WHERE elem.value #>> '{general,id}' IS NOT NULL
    """))


    duplicate = connection.execute(text("""
        SELECT kind, crm_id
        FROM _members
        GROUP BY kind, crm_id
        HAVING COUNT(DISTINCT owner_id) > 1
        LIMIT 1
    """)).first()
    if duplicate:
        raise ValueError(f"Duplicate CRM id in different owners: {duplicate[0]} {duplicate[1]}")


def _match_customers(connection, run_id):
    connection.execute(text("""
        WITH ids AS (
            SELECT crm_id, MIN(owner_id) AS owner_id
            FROM _members WHERE kind = 'customer'
            GROUP BY crm_id
        ), unique_lead_login AS (
            SELECT login, MIN(owner_id) AS owner_id
            FROM _members
            WHERE kind = 'lead' AND login IS NOT NULL
            GROUP BY login
            HAVING COUNT(DISTINCT owner_id) = 1
        )
        INSERT INTO _customer_targets(crm_id, owner_id)
        SELECT sc.crm_id, COALESCE(ids.owner_id, by_login.owner_id)
        FROM crm_sync_customers sc
        LEFT JOIN ids ON ids.crm_id = sc.crm_id
        LEFT JOIN unique_lead_login AS by_login ON by_login.login = sc.login
        WHERE sc.run_id = :run_id
          AND COALESCE(ids.owner_id, by_login.owner_id) IS NOT NULL
    """), {"run_id": run_id})


    created = connection.execute(text("""
        WITH created AS (
            INSERT INTO abstract_customer(amo_customers)
            SELECT jsonb_build_array(sc.payload)
            FROM crm_sync_customers AS sc
            LEFT JOIN _customer_targets t ON t.crm_id = sc.crm_id
            WHERE sc.run_id = :run_id AND t.crm_id IS NULL
            RETURNING id,
                (amo_customers #>> '{0,general,id}')::bigint AS crm_id
        )
        INSERT INTO _customer_targets(crm_id, owner_id)
        SELECT crm_id, id FROM created
    """), {"run_id": run_id}).rowcount
    return created


def _put_customers_into_incoming(connection, run_id):
    connection.execute(text("""
        INSERT INTO _incoming(kind, owner_id, crm_id, payload)
        SELECT 'customer', t.owner_id, sc.crm_id, sc.payload
        FROM crm_sync_customers AS sc
        JOIN _customer_targets AS t ON t.crm_id = sc.crm_id
        WHERE sc.run_id = :run_id
    """), {"run_id": run_id})


def _upsert_json_array(connection, kind):
    column = _COLUMNS[kind]
    sql = f"""
        UPDATE abstract_customer AS ac
        SET {column} = NULLIF((
            SELECT jsonb_agg(chosen.payload ORDER BY chosen.item_key)
            FROM (
                SELECT DISTINCT ON (item_key) item_key, payload
                FROM (
                    SELECT COALESCE('id:' || (e.value #>> '{{general,id}}'),
                                    'json:' || e.value::text) AS item_key,
                           e.value AS payload, 0 AS priority
                    FROM jsonb_array_elements(COALESCE(ac.{column}, '[]'::jsonb)) e(value)
                    UNION ALL
                    SELECT 'id:' || i.crm_id::text, i.payload, 1
                    FROM _incoming AS i
                    WHERE i.owner_id = ac.id AND i.kind = :kind
                ) AS variants
                ORDER BY item_key, priority DESC
            ) AS chosen
        ), '[]'::jsonb), updated_at = NOW()
        WHERE EXISTS (
            SELECT 1 FROM _incoming AS i
            WHERE i.owner_id = ac.id AND i.kind = :kind
        )
    """
    connection.execute(text(sql), {"kind": kind})


def _lead_matches_sql():
    return """
        WITH ids AS (
            SELECT crm_id, MIN(owner_id) AS owner_id
            FROM _members WHERE kind = 'lead'
            GROUP BY crm_id
        ), unique_customer_login AS (
            SELECT login, MIN(owner_id) AS owner_id
            FROM _members
            WHERE kind = 'customer' AND login IS NOT NULL
            GROUP BY login
            HAVING COUNT(DISTINCT owner_id) = 1
        )
        SELECT sl.crm_id,
               ids.owner_id AS lead_owner,
               by_login.owner_id AS customer_owner
        FROM crm_sync_leads AS sl
        LEFT JOIN ids ON ids.crm_id = sl.crm_id
        LEFT JOIN unique_customer_login AS by_login ON by_login.login = sl.login
        WHERE sl.run_id = :run_id
    """


def _merge_lead_conflicts(connection, run_id):
    merged = 0
    while True:
        conflict = connection.execute(text("""
            SELECT crm_id, lead_owner, customer_owner
            FROM (
        """ + _lead_matches_sql() + """
            ) AS matches
            WHERE lead_owner IS NOT NULL
              AND customer_owner IS NOT NULL
              AND lead_owner != customer_owner
            ORDER BY crm_id
            LIMIT 1
        """), {"run_id": run_id}).mappings().first()
        if conflict is None:
            return merged
        merge_owners(connection, conflict["lead_owner"], conflict["customer_owner"])
        merged += 1


def _match_leads(connection, run_id):
    match_sql = _lead_matches_sql()
    connection.execute(text("""
        INSERT INTO _lead_targets(crm_id, owner_id)
        SELECT crm_id, COALESCE(lead_owner, customer_owner)
        FROM (
    """ + match_sql + """
        ) AS matches
        WHERE COALESCE(lead_owner, customer_owner) IS NOT NULL
    """), {"run_id": run_id})
    created = connection.execute(text("""
        WITH created AS (
            INSERT INTO abstract_customer(amo_leads)
            SELECT jsonb_build_array(sl.payload)
            FROM crm_sync_leads AS sl
            LEFT JOIN _lead_targets t ON t.crm_id = sl.crm_id
            WHERE sl.run_id = :run_id AND t.crm_id IS NULL
            RETURNING id, (amo_leads #>> '{0,general,id}')::bigint AS crm_id
        )
        INSERT INTO _lead_targets(crm_id, owner_id)
        SELECT crm_id, id FROM created
    """), {"run_id": run_id}).rowcount
    return created


def _put_all_into_incoming(connection, run_id):
    connection.execute(text("TRUNCATE _incoming"))
    _put_customers_into_incoming(connection, run_id)
    connection.execute(text("""
        INSERT INTO _incoming(kind, owner_id, crm_id, payload)
        SELECT 'lead', t.owner_id, sl.crm_id, sl.payload
        FROM crm_sync_leads sl
        JOIN _lead_targets t ON t.crm_id = sl.crm_id
        WHERE sl.run_id = :run_id
    """), {"run_id": run_id})


    connection.execute(text("""
        WITH links AS (
            SELECT t.owner_id, sc.crm_id AS parent_id,
                   (item.value #>> '{general,id}')::bigint AS crm_id,
                   item.value AS payload, 1 AS source_priority
            FROM crm_sync_customers sc
            JOIN _customer_targets t ON t.crm_id = sc.crm_id
            CROSS JOIN LATERAL jsonb_array_elements(sc.contacts) item(value)
            WHERE sc.run_id = :run_id
            UNION ALL
            SELECT t.owner_id, sl.crm_id AS parent_id,
                   (item.value #>> '{general,id}')::bigint AS crm_id,
                   item.value AS payload, 2 AS source_priority
            FROM crm_sync_leads sl
            JOIN _lead_targets t ON t.crm_id = sl.crm_id
            CROSS JOIN LATERAL jsonb_array_elements(sl.contacts) item(value)
            WHERE sl.run_id = :run_id
        ), chosen AS (
            SELECT DISTINCT ON (owner_id, crm_id) owner_id, crm_id, payload
            FROM links WHERE crm_id IS NOT NULL
            ORDER BY owner_id, crm_id, source_priority DESC, parent_id DESC
        )
        INSERT INTO _incoming(kind, owner_id, crm_id, payload)
        SELECT 'contact', owner_id, crm_id, payload FROM chosen
    """), {"run_id": run_id})

    connection.execute(text("""
        WITH links AS (
            SELECT t.owner_id, sc.crm_id AS parent_id,
                   (item.value #>> '{general,id}')::bigint AS crm_id,
                   item.value AS payload
            FROM crm_sync_customers sc
            JOIN _customer_targets t ON t.crm_id = sc.crm_id
            CROSS JOIN LATERAL jsonb_array_elements(sc.companies) item(value)
            WHERE sc.run_id = :run_id
        ), chosen AS (
            SELECT DISTINCT ON (owner_id, crm_id) owner_id, crm_id, payload
            FROM links WHERE crm_id IS NOT NULL
            ORDER BY owner_id, crm_id, parent_id DESC
        )
        INSERT INTO _incoming(kind, owner_id, crm_id, payload)
        SELECT 'company', owner_id, crm_id, payload FROM chosen
    """), {"run_id": run_id})


def _apply_all(connection, is_full):
    if is_full:
        connection.execute(text("""
            UPDATE abstract_customer AS ac
            SET
                amo_customers = (
                    SELECT jsonb_agg(i.payload ORDER BY i.crm_id)
                    FROM _incoming i WHERE i.owner_id = ac.id AND i.kind = 'customer'
                ),
                amo_leads = (
                    SELECT jsonb_agg(i.payload ORDER BY i.crm_id)
                    FROM _incoming i WHERE i.owner_id = ac.id AND i.kind = 'lead'
                ),
                amo_contacts = (
                    SELECT jsonb_agg(i.payload ORDER BY i.crm_id)
                    FROM _incoming i WHERE i.owner_id = ac.id AND i.kind = 'contact'
                ),
                amo_companies = (
                    SELECT jsonb_agg(i.payload ORDER BY i.crm_id)
                    FROM _incoming i WHERE i.owner_id = ac.id AND i.kind = 'company'
                ),
                updated_at = NOW()
        """))
        return connection.execute(text("""
            DELETE FROM abstract_customer
            WHERE COALESCE(jsonb_array_length(amo_customers), 0) = 0
              AND COALESCE(jsonb_array_length(amo_leads), 0) = 0
        """)).rowcount


    for kind in _COLUMNS:
        _upsert_json_array(connection, kind)
    return 0


def apply_snapshot(connection, run_id, *, allow_empty_full=False, batch_size=250):
    del batch_size

    connection.execute(text("SELECT pg_advisory_xact_lock(2026, 61)"))
    connection.exec_driver_sql("LOCK TABLE abstract_customer IN SHARE ROW EXCLUSIVE MODE")
    run = connection.execute(text("""
        SELECT seq, status, is_full, customers_count, leads_count
        FROM crm_sync_runs
        WHERE run_id = :run_id FOR UPDATE
    """), {"run_id": run_id}).mappings().one()
    if run["status"] != "ready":
        raise ValueError("CRM sync run is not ready")
    newer = connection.execute(text("""
        SELECT EXISTS (
            SELECT 1 FROM crm_sync_runs
            WHERE status = 'completed' AND seq > :seq
        )
    """), {"seq": run["seq"]}).scalar_one()
    if newer:
        raise ValueError("Newer CRM sync run has already completed")
    if run["is_full"] and not allow_empty_full and not (
        run["customers_count"] or run["leads_count"]
    ):
        raise ValueError("Refusing to apply an empty full CRM snapshot")

    _create_work_tables(connection)
    _refresh_members(connection)

    created_customers = _match_customers(connection, run_id)
    _put_customers_into_incoming(connection, run_id)
    _upsert_json_array(connection, "customer")
    _refresh_members(connection)

    merged = _merge_lead_conflicts(connection, run_id)
    created_leads = _match_leads(connection, run_id)
    _put_all_into_incoming(connection, run_id)
    deleted = _apply_all(connection, run["is_full"])

    connection.execute(text("""
        UPDATE crm_sync_runs
        SET status = 'completed', completed_at = NOW()
        WHERE run_id = :run_id
    """), {"run_id": run_id})
    connection.execute(text("DELETE FROM crm_sync_customers WHERE run_id = :run_id"),
                       {"run_id": run_id})
    connection.execute(text("DELETE FROM crm_sync_leads WHERE run_id = :run_id"),
                       {"run_id": run_id})
    return {
        "created": created_customers + created_leads,
        "updated": run["customers_count"] + run["leads_count"] - created_customers - created_leads,
        "merged": merged,
        "deleted": deleted,
    }
