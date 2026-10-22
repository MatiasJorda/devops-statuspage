"""Tests de las metricas y del webhook de alertas.

Lo que se prueba aca es que la metrica sobre la que corre la alerta cuente bien.
Si statuspage_chequeos_total no sube cuando un servicio falla, la regla de
Prometheus nunca se dispara y el sistema de alertas no sirve, aunque todo lo
demas este bien configurado.
"""

import pytest
from fastapi.testclient import TestClient
from prometheus_client import generate_latest

from app import db, main, metricas


def valor(metrica, **etiquetas) -> float:
    """Lee el valor actual de una serie, o 0 si todavia no existe."""
    try:
        return metrica.labels(**etiquetas)._value.get()
    except Exception:  # noqa: BLE001
        return 0.0


@pytest.fixture
def cliente(monkeypatch):
    monkeypatch.setattr(main, "_listo", True)
    monkeypatch.setattr(db, "ping", lambda: True)
    return TestClient(main.app)


# --- La metrica que dispara la alerta ----------------------------------------

def test_un_fallo_suma_al_contador_de_fallos():
    antes = valor(metricas.CHEQUEOS, servicio="web", resultado="fallo")

    metricas.registrar_ronda([
        ({"name": "web"}, {"ok": False, "status_code": 500, "error": "roto"}),
    ])

    assert valor(metricas.CHEQUEOS, servicio="web", resultado="fallo") == antes + 1


def test_un_chequeo_bueno_no_suma_al_contador_de_fallos():
    antes = valor(metricas.CHEQUEOS, servicio="sana", resultado="fallo")

    metricas.registrar_ronda([
        ({"name": "sana"}, {"ok": True, "status_code": 200, "error": None}),
    ])

    assert valor(metricas.CHEQUEOS, servicio="sana", resultado="fallo") == antes
    assert valor(metricas.CHEQUEOS, servicio="sana", resultado="ok") >= 1


def test_el_estado_del_servicio_queda_en_cero_cuando_falla():
    metricas.registrar_ronda([
        ({"name": "intermitente"}, {"ok": True, "status_code": 200, "error": None}),
    ])
    assert valor(metricas.ARRIBA, servicio="intermitente") == 1

    metricas.registrar_ronda([
        ({"name": "intermitente"}, {"ok": False, "status_code": None, "error": "timeout"}),
    ])
    assert valor(metricas.ARRIBA, servicio="intermitente") == 0


def test_la_latencia_solo_se_registra_cuando_hubo_respuesta():
    """Sin respuesta no hay tiempo que medir. Guardar el valor del timeout
    inflaria el percentil con un numero que no es del servicio."""
    metricas.registrar_ronda([
        ({"name": "caido"}, {"ok": False, "status_code": None, "error": "timeout",
                             "latency_ms": None}),
    ])

    texto = generate_latest(metricas.REGISTRO).decode()
    assert 'statuspage_latencia_segundos_count{servicio="caido"}' not in texto


def test_el_endpoint_de_metricas_devuelve_el_formato_de_prometheus(cliente):
    respuesta = cliente.get("/metrics")

    assert respuesta.status_code == 200
    # Sin este Content-Type, Prometheus descarta la respuesta.
    assert "text/plain" in respuesta.headers["content-type"]
    assert "statuspage_chequeos_total" in respuesta.text
    # El registro es propio, asi que no deberian aparecer las metricas que la
    # libreria agrega sola sobre el proceso de Python.
    assert "python_gc_objects_collected_total" not in respuesta.text


# --- Webhook de Alertmanager -------------------------------------------------

AVISO = {
    "status": "firing",
    "alerts": [
        {
            "status": "firing",
            "fingerprint": "abc123",
            "labels": {
                "alertname": "DemasiadosFallos",
                "severity": "critica",
                "servicio": "servicio-caotico",
            },
            "annotations": {
                "summary": "Demasiados fallos",
                "description": "acumulo 7 fallos en 2 minutos",
            },
            "startsAt": "2026-10-22T18:00:00Z",
        }
    ],
}


def test_el_webhook_guarda_la_alerta(cliente, monkeypatch):
    guardadas = []
    monkeypatch.setattr(db, "registrar_alerta", lambda a: guardadas.append(a))

    respuesta = cliente.post("/api/alertas", json=AVISO)

    assert respuesta.status_code == 202
    assert respuesta.json() == {"recibidas": 1}
    assert guardadas[0]["nombre"] == "DemasiadosFallos"
    assert guardadas[0]["servicio"] == "servicio-caotico"
    assert guardadas[0]["severidad"] == "critica"


def test_el_webhook_no_devuelve_error_aunque_falle_la_base(cliente, monkeypatch):
    """Si este endpoint fallara, Alertmanager reintentaria en loop y el problema
    de monitoreo se volveria un segundo problema."""
    def explota(_):
        raise RuntimeError("la base no responde")

    monkeypatch.setattr(db, "registrar_alerta", explota)

    respuesta = cliente.post("/api/alertas", json=AVISO)

    assert respuesta.status_code == 202
    assert respuesta.json() == {"recibidas": 0}


def test_un_aviso_sin_alertas_no_rompe(cliente):
    respuesta = cliente.post("/api/alertas", json={"status": "firing", "alerts": []})
    assert respuesta.status_code == 202
    assert respuesta.json() == {"recibidas": 0}


def test_listar_alertas_separa_activas_de_recientes(cliente, monkeypatch):
    monkeypatch.setattr(db, "alertas_activas", lambda: [{"nombre": "DemasiadosFallos"}])
    monkeypatch.setattr(db, "alertas_recientes", lambda limit: [{"nombre": "X"}, {"nombre": "Y"}])

    datos = cliente.get("/api/alertas").json()

    assert len(datos["activas"]) == 1
    assert len(datos["recientes"]) == 2


def test_un_fallo_en_las_metricas_no_tumba_la_ronda(cliente, monkeypatch):
    """Los chequeos ya estan guardados cuando se registran las metricas. Si el
    registro explotara y no se atrapara, el endpoint devolveria 500 y el CronJob
    daria por fallida una ronda que en realidad salio bien."""
    monkeypatch.setattr(
        db, "list_targets",
        lambda only_enabled=False: [{"id": 1, "name": "web", "kind": "http", "url": "http://x/"}],
    )
    monkeypatch.setattr(
        main.checker, "chequear_todos",
        lambda targets: [(targets[0], {"ok": True, "status_code": 200, "error": None})],
    )
    monkeypatch.setattr(db, "record_check", lambda *a: None)

    def explota(_):
        raise RuntimeError("se rompio el registro de metricas")

    monkeypatch.setattr(main.metricas, "registrar_ronda", explota)

    respuesta = cliente.post("/api/check-now")

    assert respuesta.status_code == 200
    assert respuesta.json() == {"chequeados": 1, "caidos": 0}
