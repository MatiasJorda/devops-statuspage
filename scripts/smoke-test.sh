#!/usr/bin/env bash
#
# Prueba la version GREEN por su Service privado, antes de darle un solo usuario.
#
# ESTO ES LO QUE DISTINGUE AL BLUE/GREEN DE UN ROLLING UPDATE
# En un rolling update, un Pod nuevo empieza a recibir usuarios apenas queda Ready:
# si la version nueva esta rota pero arranca, los usuarios se comen el error. Aca
# green esta corriendo hace rato y se le puede pegar todo lo que haga falta, porque
# el Service publico todavia apunta a blue.
#
# Devuelve 0 si green esta listo para recibir trafico, distinto de 0 si no.
# Lo usa switch.sh como condicion para hacer el cambio.
set -euo pipefail

NS=statuspage
SERVICIO=statuspage-green

fallo() { echo "  FALLO: $1" >&2; exit 1; }

echo "==> Smoke test contra green ($SERVICIO)"

# Se corre desde un Pod temporal adentro del cluster: es la unica forma de pegarle
# a un Service de tipo ClusterIP, que no esta expuesto hacia afuera.
ejecutar() {
    kubectl -n "$NS" run smoke-test-$RANDOM \
        --rm -i --restart=Never --quiet \
        --image=python:3.12-slim \
        --command -- python -c "$1" 2>/dev/null
}

echo "--> 1/3 la version nueva responde /health"
salud="$(ejecutar "
import urllib.request, json
with urllib.request.urlopen('http://$SERVICIO:8080/health', timeout=10) as r:
    print(json.load(r)['version'])
")" || fallo "green no responde /health"
echo "    version que contesta: $salud"
[ "$salud" = "2.0" ] || fallo "se esperaba la version 2.0 y contesto '$salud'"

echo "--> 2/3 la version nueva tiene base (/ready)"
ejecutar "
import urllib.request
urllib.request.urlopen('http://$SERVICIO:8080/ready', timeout=10)
" >/dev/null || fallo "green no esta Ready: no llega a la base"

echo "--> 3/3 el tablero devuelve datos (/api/status)"
cantidad="$(ejecutar "
import urllib.request, json
with urllib.request.urlopen('http://$SERVICIO:8080/api/status', timeout=15) as r:
    print(len(json.load(r)))
")" || fallo "green no puede leer los servicios monitoreados"
echo "    servicios monitoreados: $cantidad"

echo
echo "Smoke test OK: green esta lista para recibir trafico."
