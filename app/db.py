"""Acceso a Postgres: esquema, pool de conexiones y consultas."""

import json
import os
from contextlib import contextmanager

import psycopg
from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool

from . import config

_pool: ConnectionPool | None = None


class NombreRepetido(Exception):
    """Ya existe un servicio con ese nombre (hay un UNIQUE en la tabla).

    Existe para que la capa de la API pueda distinguir este caso, que es un error
    del usuario y merece un 409, de una falla de la base, que es un problema del
    servidor y merece un 503. Sin esto habria que atrapar cualquier excepcion y
    contestar siempre lo mismo, que es enganoso y ademas filtra al cliente los
    mensajes internos de Postgres.
    """


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
# LA MIGRACION DE LA V2, QUE ES EL PUNTO MAS IMPORTANTE DE TODO EL PROYECTO
#
# Blue (v1) y green (v2) comparten ESTA MISMA base. La v2 necesita una columna
# nueva, latency_ms, que la v1 no conoce. Si el cambio de esquema fuera
# destructivo (renombrar una columna, cambiarle el tipo, agregarla como NOT NULL),
# despues del switch la v1 dejaria de poder escribir y el rollback seria
# imposible: quedarias obligado a seguir adelante con una version rota.
#
# Por eso la migracion es ADITIVA:
#   - se AGREGA una columna, no se toca ninguna existente
#   - la columna admite NULL, asi que los INSERT de la v1 (que no la mencionan)
#     siguen siendo validos y dejan el valor vacio
#   - las filas que ya escribio la v2 no le molestan a la v1: simplemente no las
#     lee
#
# Es el patron que en la industria se llama expand/contract: primero se expande el
# esquema de forma compatible, y recien cuando ya no queda ninguna version vieja
# corriendo se limpia lo que sobra.

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
    error       TEXT,
    -- Agregada por la v2. Va sin NOT NULL a proposito: es lo que permite que la
    -- v1 siga insertando sin mencionarla.
    latency_ms  REAL
);

CREATE INDEX IF NOT EXISTS idx_checks_target_ts ON checks (target_id, ts DESC);
"""


# CREATE TABLE IF NOT EXISTS no sirve para agregar una columna a una tabla que ya
# existe: si la base la creo la v1, la tabla esta pero sin latency_ms. Por eso la
# migracion va aparte y se ejecuta siempre.
#
# ADD COLUMN IF NOT EXISTS la hace idempotente: se puede correr mil veces y en
# varias replicas a la vez sin romper nada (la segunda espera el lock, ve que la
# columna ya esta y no hace nada).
MIGRACIONES = """
ALTER TABLE checks ADD COLUMN IF NOT EXISTS latency_ms REAL;
"""


def init_schema() -> None:
    with cursor() as cur:
        cur.execute(SCHEMA)
        cur.execute(MIGRACIONES)


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
    try:
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
    except psycopg.errors.UniqueViolation as exc:
        raise NombreRepetido(data["name"]) from exc


def set_enabled(target_id: int, enabled: bool) -> bool:
    """Pausa o reanuda el monitoreo de un servicio.

    Un servicio pausado sigue en la lista con su historial intacto, pero
    chequear_ahora() lo saltea porque consulta con only_enabled=True. Sirve para
    un mantenimiento programado: se pausa, no ensucia el porcentaje de
    disponibilidad con caidas esperadas, y despues se reanuda.
    """
    with cursor() as cur:
        cur.execute(
            "UPDATE targets SET enabled = %s WHERE id = %s", (enabled, target_id)
        )
        return cur.rowcount > 0


def delete_target(target_id: int) -> bool:
    with cursor() as cur:
        cur.execute("DELETE FROM targets WHERE id = %s", (target_id,))
        return cur.rowcount > 0


def record_check(target_id: int, ok: bool, status_code, error, latency_ms=None) -> None:
    """Guarda un chequeo. latency_ms es el agregado de la v2."""
    with cursor() as cur:
        cur.execute(
            "INSERT INTO checks (target_id, ok, status_code, error, latency_ms) "
            "VALUES (%s, %s, %s, %s, %s)",
            (target_id, ok, status_code, error, latency_ms),
        )


def status_summary() -> list[dict]:
    """Estado actual de cada servicio + porcentaje de disponibilidad por ventana."""
    with cursor() as cur:
        cur.execute(
            """
            WITH ultimo AS (
                SELECT DISTINCT ON (target_id)
                       target_id, ts, ok, status_code, error, latency_ms
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
                    count(*) FILTER (WHERE ts > now() - interval '7 days'  AND ok)  AS ok7d,
                    -- Promedio de latencia de la ultima hora. avg() ignora los
                    -- NULL solo: las filas que dejo la v1 no bajan el promedio,
                    -- simplemente no cuentan.
                    avg(latency_ms) FILTER (WHERE ts > now() - interval '1 hour')   AS lat1h
                FROM checks
                GROUP BY target_id
            )
            SELECT t.id, t.name, t.kind, t.url, t.host, t.port, t.source, t.enabled,
                   u.ts AS last_ts, u.ok AS last_ok,
                   u.status_code AS last_status, u.error AS last_error,
                   u.latency_ms AS last_latency,
                   round(v.lat1h::numeric, 1) AS latencia_media_1h,
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
            "SELECT ts, ok, status_code, error, latency_ms FROM checks "
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
