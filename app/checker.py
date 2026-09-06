"""Logica de chequeo: le pega a un servicio y decide si esta sano.

Es el corazon de la aplicacion y a proposito NO sabe nada de Postgres ni de
FastAPI: recibe un diccionario que describe un servicio y devuelve otro con el
resultado. Asi se puede testear sin base de datos y sin levantar el servidor
(ver tests/test_checker.py).

VERSION 1: se registra si el servicio respondio y con que codigo HTTP.
La v2 agrega la medicion de latencia.
"""

import socket
from concurrent.futures import ThreadPoolExecutor

import httpx

# Cuantos servicios se chequean en paralelo. Con un timeout de 3s, chequear 10
# servicios de a uno tardaria hasta 30 segundos; en paralelo tarda 3.
MAX_PARALELO = 10


def chequear(target: dict) -> dict:
    """Chequea un servicio. Devuelve {'ok', 'status_code', 'error'}.

    Nunca lanza excepciones: un servicio caido es un resultado valido del
    chequeo, no un error del programa. Si esta funcion tirara una excepcion,
    un solo servicio roto cortaria la ronda entera de chequeos.
    """
    if target.get("kind") == "tcp":
        return _chequear_tcp(target)
    return _chequear_http(target)


def _chequear_http(target: dict) -> dict:
    url = target["url"]
    timeout = target.get("timeout_s") or 3
    esperado = target.get("expected_status") or 200

    try:
        # follow_redirects: un 301 hacia una pagina sana no es una caida.
        respuesta = httpx.get(url, timeout=timeout, follow_redirects=True)
    except httpx.TimeoutException:
        return {"ok": False, "status_code": None, "error": f"timeout ({timeout}s)"}
    except httpx.RequestError as exc:
        # Cubre DNS que no resuelve, conexion rechazada, TLS invalido, etc.
        return {"ok": False, "status_code": None, "error": type(exc).__name__}

    ok = respuesta.status_code == esperado
    return {
        "ok": ok,
        "status_code": respuesta.status_code,
        "error": None if ok else f"se esperaba {esperado}",
    }


def _chequear_tcp(target: dict) -> dict:
    """Para servicios que no hablan HTTP, como Postgres.

    No se manda ningun dato: alcanza con ver si el puerto acepta la conexion.
    """
    host = target["host"]
    port = target["port"]
    timeout = target.get("timeout_s") or 3

    try:
        with socket.create_connection((host, port), timeout=timeout):
            return {"ok": True, "status_code": None, "error": None}
    except socket.timeout:
        return {"ok": False, "status_code": None, "error": f"timeout ({timeout}s)"}
    except OSError as exc:
        return {"ok": False, "status_code": None, "error": exc.strerror or str(exc)}


def chequear_todos(targets: list[dict]) -> list[tuple[dict, dict]]:
    """Chequea varios servicios en paralelo.

    Devuelve pares (target, resultado) en el mismo orden que entraron, para que
    quien llama sepa a que servicio corresponde cada resultado.
    """
    if not targets:
        return []

    with ThreadPoolExecutor(max_workers=MAX_PARALELO) as pool:
        resultados = list(pool.map(chequear, targets))

    return list(zip(targets, resultados))
