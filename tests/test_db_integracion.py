"""Tests del SQL contra una base de verdad.

El calculo de disponibilidad vive entero en una consulta SQL, asi que no se puede
probar con objetos falsos: hay que correrlo contra Postgres. Estos tests se
saltean solos si no hay una base a mano, para que "pytest tests/" siga andando en
una maquina donde no hay nada levantado.

Para correrlos:

    kubectl -n statuspage port-forward svc/postgres 5432:5432 &
    DATABASE_URL=postgresql://status:statuspage-demo@localhost:5432/statuspage \\
        pytest tests/test_db_integracion.py

Cada test crea su propio servicio con un nombre unico y lo borra al final. Los
chequeos se borran solos con el ON DELETE CASCADE, asi que no queda basura ni se
tocan los datos que ya estaban.
"""

import os

import pytest

SIN_BASE = not os.getenv("DATABASE_URL")
pytestmark = pytest.mark.skipif(SIN_BASE, reason="no hay DATABASE_URL configurada")

if not SIN_BASE:
    from app import db


@pytest.fixture
def target():
    """Un servicio de prueba, con su nombre unico, que se borra al terminar."""
    db.init_pool()
    db.init_schema()
    creado = db.create_target(
        {
            "name": f"test-{os.getpid()}-{id(object())}",
            "kind": "http",
            "url": "http://ejemplo-de-prueba/",
            "host": None,
            "port": None,
            "timeout_s": 3,
            "expected_status": 200,
        }
    )
    yield creado
    db.delete_target(creado["id"])


def _fila_de(resumen, target_id):
    return next(f for f in resumen if f["id"] == target_id)


def test_uptime_con_todos_los_chequeos_bien(target):
    for _ in range(4):
        db.record_check(target["id"], True, 200, None)

    fila = _fila_de(db.status_summary(), target["id"])
    assert float(fila["uptime_1h"]) == 100.0
    assert fila["last_ok"] is True


def test_uptime_con_una_caida_de_cuatro(target):
    """Tres bien y una mal tienen que dar 75%."""
    db.record_check(target["id"], True, 200, None)
    db.record_check(target["id"], True, 200, None)
    db.record_check(target["id"], False, 500, "roto")
    db.record_check(target["id"], True, 200, None)

    fila = _fila_de(db.status_summary(), target["id"])
    assert float(fila["uptime_1h"]) == 75.0


def test_un_servicio_sin_chequeos_no_rompe_la_consulta(target):
    """El LEFT JOIN tiene que devolver la fila con los porcentajes en NULL, no
    hacer desaparecer al servicio del tablero."""
    fila = _fila_de(db.status_summary(), target["id"])
    assert fila["uptime_1h"] is None
    assert fila["last_ok"] is None


def test_el_ultimo_estado_es_el_mas_reciente(target):
    db.record_check(target["id"], True, 200, None)
    db.record_check(target["id"], False, 503, "se cayo recien")

    fila = _fila_de(db.status_summary(), target["id"])
    assert fila["last_ok"] is False
    assert fila["last_status"] == 503


def test_el_historial_viene_del_mas_viejo_al_mas_nuevo(target):
    """El tablero dibuja las barritas de izquierda a derecha, asi que la mas
    reciente tiene que quedar ultima."""
    db.record_check(target["id"], True, 200, None)
    db.record_check(target["id"], False, 500, "roto")

    historial = db.history(target["id"])
    assert [c["ok"] for c in historial] == [True, False]


def test_borrar_un_servicio_borra_su_historial(target):
    db.record_check(target["id"], True, 200, None)
    db.delete_target(target["id"])

    with db.cursor() as cur:
        cur.execute("SELECT count(*) AS n FROM checks WHERE target_id = %s", (target["id"],))
        assert cur.fetchone()["n"] == 0


def test_nombre_repetido_lanza_la_excepcion_propia(target):
    """Es lo que le permite a la API contestar 409 y no 503."""
    with pytest.raises(db.NombreRepetido):
        db.create_target(
            {
                "name": target["name"],
                "kind": "http",
                "url": "http://otro/",
                "host": None,
                "port": None,
                "timeout_s": 3,
                "expected_status": 200,
            }
        )


def test_pausar_saca_al_servicio_de_la_lista_de_chequeo(target):
    assert db.set_enabled(target["id"], False) is True
    activos = [t["id"] for t in db.list_targets(only_enabled=True)]
    assert target["id"] not in activos

    db.set_enabled(target["id"], True)
    activos = [t["id"] for t in db.list_targets(only_enabled=True)]
    assert target["id"] in activos
