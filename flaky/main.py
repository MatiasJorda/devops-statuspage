"""Servicio que falla a proposito, para que el tablero tenga algo que mostrar.

Sin este servicio todos los semaforos estarian siempre en verde, el historial
seria una linea plana y todos los porcentajes dirian 100%: no se veria que el
tablero realmente distingue un servicio sano de uno caido.

Responde 200 el 80% de las veces y 500 el 20% restante. El porcentaje se
configura con la variable de entorno TASA_FALLA (0 a 1), que en Kubernetes llega
desde el ConfigMap.

Usa solo la biblioteca estandar de Python: sin dependencias, sin requirements.txt
y la imagen es la base pelada.
"""

import os
import random
from http.server import BaseHTTPRequestHandler, HTTPServer

TASA_FALLA = float(os.getenv("TASA_FALLA", "0.2"))
PUERTO = int(os.getenv("PUERTO", "8080"))


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        if random.random() < TASA_FALLA:
            self._responder(500, "caos: este servicio fallo a proposito")
        else:
            self._responder(200, "ok")

    def _responder(self, codigo: int, mensaje: str):
        cuerpo = mensaje.encode()
        self.send_response(codigo)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Content-Length", str(len(cuerpo)))
        self.end_headers()
        self.wfile.write(cuerpo)

    def log_message(self, formato, *args):
        # El log por defecto escribe en stderr con un formato raro; se pasa a
        # stdout para que "kubectl logs" lo muestre igual que el resto.
        print(formato % args, flush=True)


if __name__ == "__main__":
    print(f"servicio caotico escuchando en :{PUERTO}, tasa de falla {TASA_FALLA}", flush=True)
    HTTPServer(("0.0.0.0", PUERTO), Handler).serve_forever()
