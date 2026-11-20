from sqlalchemy import text


_COLUMNS = {
    "customer": "amo_customers",
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
