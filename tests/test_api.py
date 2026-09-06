"""Tests de la API HTTP.

No necesitan Postgres: se reemplazan las funciones del modulo db por versiones
falsas. Lo que se prueba aca es la capa web, o sea que cada situacion devuelva el
codigo HTTP correcto:

  - un pedido mal armado             -> 400
  - un nombre ya usado               -> 409  (culpa del pedido)
  - la base no responde              -> 503  (culpa del servidor)
  - un id que no existe              -> 404
  - la app sin base                  -> /ready responde 503 y /health responde 200

La diferencia entre 409 y 503 no es un detalle: un 409 le dice al usuario "cambia
el nombre" y un 503 le dice "no es tu culpa, reintenta". Confundirlos manda a la
persona a arreglar algo que no esta roto.
"""

import pytest
from fastapi.testclient import TestClient

from app import db, main


@pytest.fixture
def cliente(monkeypatch):
    """Cliente de prueba con la base reemplazada por funciones falsas.

    Se instancia TestClient SIN "with" a proposito: asi no corre el lifespan de la
    aplicacion, que intentaria conectarse a Postgres de verdad.
    """
    monkeypatch.setattr(main, "_listo", True)
    monkeypatch.setattr(db, "ping", lambda: True)
    return TestClient(main.app)


# --- Probes ------------------------------------------------------------------

def test_health_no_depende_de_la_base(cliente, monkeypatch):
    """La liveness tiene que seguir en 200 aunque la base este caida.

    Si devolviera error, Kubernetes reiniciaria el Pod en loop sin arreglar nada,
    porque el problema esta afuera del contenedor.
    """
    monkeypatch.setattr(db, "ping", lambda: False)
    respuesta = cliente.get("/health")
    assert respuesta.status_code == 200
    assert respuesta.json()["status"] == "ok"


def test_ready_da_503_cuando_no_hay_base(cliente, monkeypatch):
    """La readiness si mira la base: sin base, el Pod sale del Service."""
    monkeypatch.setattr(db, "ping", lambda: False)
    assert cliente.get("/ready").status_code == 503


def test_ready_da_200_con_base(cliente):
    assert cliente.get("/ready").status_code == 200


def test_version_informa_color_y_numero(cliente):
    datos = cliente.get("/api/version").json()
    assert "version" in datos and "color" in datos


# --- Alta de servicios -------------------------------------------------------

def test_alta_valida(cliente, monkeypatch):
    monkeypatch.setattr(db, "create_target", lambda d: {"id": 1, **d})
    respuesta = cliente.post(
        "/api/targets", json={"name": "web", "kind": "http", "url": "http://x/"}
    )
    assert respuesta.status_code == 201
    assert respuesta.json()["name"] == "web"


def test_http_sin_url_da_400(cliente):
    respuesta = cliente.post("/api/targets", json={"name": "web", "kind": "http"})
    assert respuesta.status_code == 400


def test_tcp_sin_host_ni_puerto_da_400(cliente):
    respuesta = cliente.post("/api/targets", json={"name": "base", "kind": "tcp"})
    assert respuesta.status_code == 400


def test_tipo_invalido_da_422(cliente):
    """Lo rechaza pydantic antes de llegar al cuerpo de la funcion."""
    respuesta = cliente.post(
        "/api/targets", json={"name": "x", "kind": "ftp", "url": "http://x/"}
    )
    assert respuesta.status_code == 422


def test_nombre_repetido_da_409(cliente, monkeypatch):
    def repetido(_):
        raise db.NombreRepetido("web")

    monkeypatch.setattr(db, "create_target", repetido)
    respuesta = cliente.post(
        "/api/targets", json={"name": "web", "kind": "http", "url": "http://x/"}
    )
    assert respuesta.status_code == 409


def test_base_caida_da_503_y_no_filtra_el_error_interno(cliente, monkeypatch):
    """El mensaje crudo de Postgres puede revelar nombres de tablas, usuarios o
    direcciones internas: tiene que quedarse en el log, no viajar al cliente."""
    def explota(_):
        raise RuntimeError("connection to server at 10.0.0.5 failed: FATAL role x")

    monkeypatch.setattr(db, "create_target", explota)
    respuesta = cliente.post(
        "/api/targets", json={"name": "web", "kind": "http", "url": "http://x/"}
    )
    assert respuesta.status_code == 503
    assert "10.0.0.5" not in respuesta.text
    assert "FATAL" not in respuesta.text


# --- Pausar, reanudar y borrar -----------------------------------------------

def test_pausar_un_servicio(cliente, monkeypatch):
    monkeypatch.setattr(db, "set_enabled", lambda i, e: True)
    respuesta = cliente.patch("/api/targets/1", json={"enabled": False})
    assert respuesta.status_code == 200
    assert respuesta.json()["enabled"] is False


def test_pausar_algo_que_no_existe_da_404(cliente, monkeypatch):
    monkeypatch.setattr(db, "set_enabled", lambda i, e: False)
    assert cliente.patch("/api/targets/999", json={"enabled": False}).status_code == 404


def test_borrar_devuelve_204(cliente, monkeypatch):
    monkeypatch.setattr(db, "delete_target", lambda i: True)
    assert cliente.delete("/api/targets/1").status_code == 204


def test_borrar_algo_que_no_existe_da_404(cliente, monkeypatch):
    monkeypatch.setattr(db, "delete_target", lambda i: False)
    assert cliente.delete("/api/targets/999").status_code == 404


# --- Ronda de chequeos -------------------------------------------------------

def test_check_now_saltea_los_servicios_pausados(cliente, monkeypatch):
    """Un servicio pausado no se chequea: por eso la consulta pide only_enabled."""
    pedidos = {}

    def falso_list_targets(only_enabled=False):
        pedidos["only_enabled"] = only_enabled
        return []

    monkeypatch.setattr(db, "list_targets", falso_list_targets)
    respuesta = cliente.post("/api/check-now")

    assert respuesta.status_code == 200
    assert pedidos["only_enabled"] is True
    assert respuesta.json() == {"chequeados": 0, "caidos": 0}


def test_check_now_guarda_un_resultado_por_servicio(cliente, monkeypatch):
    guardados = []
    monkeypatch.setattr(
        db, "list_targets",
        lambda only_enabled=False: [
            {"id": 1, "kind": "http", "url": "http://a/"},
            {"id": 2, "kind": "http", "url": "http://b/"},
        ],
    )
    monkeypatch.setattr(
        main.checker, "chequear_todos",
        lambda targets: [
            (targets[0], {"ok": True, "status_code": 200, "error": None, "latency_ms": 5}),
            (targets[1], {"ok": False, "status_code": 500, "error": "roto", "latency_ms": None}),
        ],
    )
    monkeypatch.setattr(db, "record_check", lambda *a: guardados.append(a))

    assert cliente.post("/api/check-now").json() == {"chequeados": 2, "caidos": 1}
    assert len(guardados) == 2
