"""Tests de la logica de chequeo.

No tocan Postgres ni FastAPI: levantan servidores HTTP de verdad en un puerto
libre y le piden al checker que los mire. Por eso corren en menos de un segundo
y no necesitan que el entorno este desplegado.

    pip install pytest && pytest tests/
"""

import socket
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from app import checker


def _puerto_libre() -> int:
    """Pide al sistema operativo un puerto que nadie este usando."""
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture
def servidor():
    """Levanta un servidor HTTP que responde con el codigo que se le pida.

    Se usa como: url = servidor(200)  ->  devuelve la URL de un server que
    contesta 200. Se apaga solo al terminar el test.
    """
    servidores = []

    def crear(codigo: int):
        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                self.send_response(codigo)
                self.send_header("Content-Length", "2")
                self.end_headers()
                self.wfile.write(b"ok")

            def log_message(self, *args):
                pass  # silencio: si no, cada test ensucia la salida

        puerto = _puerto_libre()
        httpd = HTTPServer(("127.0.0.1", puerto), Handler)
        threading.Thread(target=httpd.serve_forever, daemon=True).start()
        servidores.append(httpd)
        return f"http://127.0.0.1:{puerto}/"

    yield crear

    for httpd in servidores:
        httpd.shutdown()


# --- HTTP --------------------------------------------------------------------

def test_http_sano(servidor):
    resultado = checker.chequear({"kind": "http", "url": servidor(200)})
    assert resultado["ok"] is True
    assert resultado["status_code"] == 200
    assert resultado["error"] is None


def test_http_con_error_del_servidor(servidor):
    resultado = checker.chequear({"kind": "http", "url": servidor(500)})
    assert resultado["ok"] is False
    assert resultado["status_code"] == 500


def test_http_codigo_esperado_distinto_de_200(servidor):
    """Un 301 puede ser la respuesta correcta si es la que se espera."""
    target = {"kind": "http", "url": servidor(301), "expected_status": 301}
    assert checker.chequear(target)["ok"] is True


def test_http_sin_nadie_escuchando():
    """Conexion rechazada: es una caida, no una excepcion que rompa la ronda."""
    resultado = checker.chequear(
        {"kind": "http", "url": f"http://127.0.0.1:{_puerto_libre()}/", "timeout_s": 1}
    )
    assert resultado["ok"] is False
    assert resultado["status_code"] is None
    assert resultado["error"]


# --- TCP ---------------------------------------------------------------------

def test_tcp_con_puerto_abierto(servidor):
    url = servidor(200)
    puerto = int(url.rsplit(":", 1)[1].rstrip("/"))
    resultado = checker.chequear({"kind": "tcp", "host": "127.0.0.1", "port": puerto})
    assert resultado["ok"] is True


def test_tcp_con_puerto_cerrado():
    resultado = checker.chequear(
        {"kind": "tcp", "host": "127.0.0.1", "port": _puerto_libre(), "timeout_s": 1}
    )
    assert resultado["ok"] is False
    assert resultado["error"]


# --- Ronda completa ----------------------------------------------------------

def test_chequear_todos_respeta_el_orden(servidor):
    """El orden importa: quien llama usa el indice para saber a que servicio
    corresponde cada resultado antes de guardarlo en la base."""
    targets = [
        {"name": "sano", "kind": "http", "url": servidor(200)},
        {"name": "roto", "kind": "http", "url": servidor(500)},
        {"name": "sano-2", "kind": "http", "url": servidor(200)},
    ]
    pares = checker.chequear_todos(targets)

    assert [t["name"] for t, _ in pares] == ["sano", "roto", "sano-2"]
    assert [r["ok"] for _, r in pares] == [True, False, True]


def test_chequear_todos_sin_servicios():
    assert checker.chequear_todos([]) == []
