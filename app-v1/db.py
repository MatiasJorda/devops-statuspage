"""Acceso a Postgres: esquema, pool de conexiones y consultas."""

import json
import os
from contextlib import contextmanager

import psycopg
from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool

from . import config

_pool: ConnectionPool | None = None


def init_pool() -> None:
    """Crea el pool. Se llama una sola vez al arrancar la app."""
    global _pool
    if _pool is None:
        _pool = ConnectionPool(config.DATABASE_URL, min_size=1, max_size=5, open=True)


def close_pool() -> None:
    global _pool
    if _pool is not None:
        _pool.close()
        _pool = None


@contextmanager
def cursor():
    """Devuelve un cursor que entrega filas como diccionarios."""
    if _pool is None:
        init_pool()
    with _pool.connection() as conn:
        with conn.cursor(row_factory=dict_row) as cur:
            yield cur


# --- Esquema -----------------------------------------------------------------
# v1: la tabla checks NO tiene columna de latencia. La v2 la agrega con una
# migracion aditiva, de modo que esta version pueda seguir escribiendo si hay
# que volver atras (rollback del blue/green).

SCHEMA = """
CREATE TABLE IF NOT EXISTS targets (
    id              SERIAL PRIMARY KEY,
    name            TEXT UNIQUE NOT NULL,
    kind            TEXT NOT NULL DEFAULT 'http',   -- http | tcp
    url             TEXT,                            -- solo para kind=http
    host            TEXT,                            -- solo para kind=tcp
    port            INTEGER,                         -- solo para kind=tcp
    timeout_s       REAL NOT NULL DEFAULT 3,
    expected_status INTEGER NOT NULL DEFAULT 200,
    source          TEXT NOT NULL DEFAULT 'ui',      -- configmap | ui
    enabled         BOOLEAN NOT NULL DEFAULT TRUE,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS checks (
    id          BIGSERIAL PRIMARY KEY,
    target_id   INTEGER NOT NULL REFERENCES targets(id) ON DELETE CASCADE,
    ts          TIMESTAMPTZ NOT NULL DEFAULT now(),
    ok          BOOLEAN NOT NULL,
    status_code INTEGER,
    error       TEXT
);

CREATE INDEX IF NOT EXISTS idx_checks_target_ts ON checks (target_id, ts DESC);
"""


def init_schema() -> None:
    with cursor() as cur:
        cur.execute(SCHEMA)


def seed_targets() -> int:
    """Carga los servicios definidos en el ConfigMap montado.

    Es idempotente: si el servicio ya existe se actualizan sus datos, no se
    duplica. Los servicios agregados desde la interfaz (source='ui') no se tocan.
    """
    path = config.TARGETS_FILE
    if not path or not os.path.exists(path):
        return 0

    with open(path) as fh:
        targets = json.load(fh)

    with cursor() as cur:
        for t in targets:
            cur.execute(
                """
                INSERT INTO targets (name, kind, url, host, port, timeout_s,
                                     expected_status, source)
                VALUES (%(name)s, %(kind)s, %(url)s, %(host)s, %(port)s,
                        %(timeout_s)s, %(expected_status)s, 'configmap')
                ON CONFLICT (name) DO UPDATE SET
                    kind = EXCLUDED.kind,
                    url = EXCLUDED.url,
                    host = EXCLUDED.host,
                    port = EXCLUDED.port,
                    timeout_s = EXCLUDED.timeout_s,
                    expected_status = EXCLUDED.expected_status
                WHERE targets.source = 'configmap'
                """,
                {
                    "name": t["name"],
                    "kind": t.get("kind", "http"),
                    "url": t.get("url"),
                    "host": t.get("host"),
                    "port": t.get("port"),
                    "timeout_s": t.get("timeout_s", config.DEFAULT_TIMEOUT),
                    "expected_status": t.get("expected_status", 200),
                },
            )
    return len(targets)


# --- Consultas ---------------------------------------------------------------

def list_targets(only_enabled: bool = False) -> list[dict]:
    sql = "SELECT * FROM targets"
    if only_enabled:
        sql += " WHERE enabled"
    sql += " ORDER BY name"
    with cursor() as cur:
        cur.execute(sql)
        return cur.fetchall()


def create_target(data: dict) -> dict:
    with cursor() as cur:
        cur.execute(
            """
            INSERT INTO targets (name, kind, url, host, port, timeout_s,
                                 expected_status, source)
            VALUES (%(name)s, %(kind)s, %(url)s, %(host)s, %(port)s,
                    %(timeout_s)s, %(expected_status)s, 'ui')
            RETURNING *
            """,
            data,
        )
        return cur.fetchone()


def delete_target(target_id: int) -> bool:
    with cursor() as cur:
        cur.execute("DELETE FROM targets WHERE id = %s", (target_id,))
        return cur.rowcount > 0


def record_check(target_id: int, ok: bool, status_code, error) -> None:
    with cursor() as cur:
        cur.execute(
            "INSERT INTO checks (target_id, ok, status_code, error) "
            "VALUES (%s, %s, %s, %s)",
            (target_id, ok, status_code, error),
        )


def status_summary() -> list[dict]:
    """Estado actual de cada servicio + porcentaje de disponibilidad por ventana."""
    with cursor() as cur:
        cur.execute(
            """
            WITH ultimo AS (
                SELECT DISTINCT ON (target_id)
                       target_id, ts, ok, status_code, error
                FROM checks
                ORDER BY target_id, ts DESC
            ),
            ventanas AS (
                SELECT target_id,
                    count(*) FILTER (WHERE ts > now() - interval '1 hour')          AS n1h,
                    count(*) FILTER (WHERE ts > now() - interval '1 hour'  AND ok)  AS ok1h,
                    count(*) FILTER (WHERE ts > now() - interval '24 hours')        AS n24h,
                    count(*) FILTER (WHERE ts > now() - interval '24 hours' AND ok) AS ok24h,
                    count(*) FILTER (WHERE ts > now() - interval '7 days')          AS n7d,
                    count(*) FILTER (WHERE ts > now() - interval '7 days'  AND ok)  AS ok7d
                FROM checks
                GROUP BY target_id
            )
            SELECT t.id, t.name, t.kind, t.url, t.host, t.port, t.source, t.enabled,
                   u.ts AS last_ts, u.ok AS last_ok,
                   u.status_code AS last_status, u.error AS last_error,
                   round(v.ok1h  * 100.0 / NULLIF(v.n1h , 0), 2) AS uptime_1h,
                   round(v.ok24h * 100.0 / NULLIF(v.n24h, 0), 2) AS uptime_24h,
                   round(v.ok7d  * 100.0 / NULLIF(v.n7d , 0), 2) AS uptime_7d
            FROM targets t
            LEFT JOIN ultimo   u ON u.target_id = t.id
            LEFT JOIN ventanas v ON v.target_id = t.id
            ORDER BY t.name
            """
        )
        return cur.fetchall()


def history(target_id: int, limit: int = 60) -> list[dict]:
    with cursor() as cur:
        cur.execute(
            "SELECT ts, ok, status_code, error FROM checks "
            "WHERE target_id = %s ORDER BY ts DESC LIMIT %s",
            (target_id, limit),
        )
        return list(reversed(cur.fetchall()))


def ping() -> bool:
    """Usado por la readiness probe: la app no esta lista sin base."""
    try:
        with cursor() as cur:
            cur.execute("SELECT 1")
            return True
    except psycopg.Error:
        return False
