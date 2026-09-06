#!/usr/bin/env bash
#
# Prueba la version GREEN por su Service privado, antes de darle un solo usuario.
#
# ESTO ES LO QUE DISTINGUE AL BLUE/GREEN DE UN ROLLING UPDATE
# En un rolling update un Pod nuevo empieza a recibir usuarios apenas queda Ready:
# si la version nueva esta rota pero arranca, los usuarios se comen el error. Aca
# green esta corriendo hace rato y se le puede pegar todo lo que haga falta,
# porque el Service publico todavia apunta a blue.
#
# Las pruebas corren DENTRO del cluster, en un Pod temporal: es la unica forma de
# alcanzar un Service de tipo ClusterIP, que no esta expuesto hacia afuera.
#
# Devuelve 0 si green esta lista para recibir trafico, distinto de 0 si no.
set -euo pipefail

NS=statuspage
SERVICIO=statuspage-green

fallo() { echo "  FALLO: $1" >&2; exit 1; }

# Corre un script de Python en un Pod temporal y devuelve su salida.
#
# No se usa "kubectl run --rm -i" a proposito: esa forma se queda esperando en
# stdin cuando no hay terminal interactiva (por ejemplo dentro de otro script o en
# un pipeline de CI) y el comando nunca termina. Aca se lanza el Pod, se espera a
# que termine, se leen sus logs y se lo borra a mano.
ejecutar() {
    local codigo="$1"
    local nombre="smoke-$$-$RANDOM"
    local fase=""

    kubectl -n "$NS" run "$nombre" \
        --restart=Never \
        --image=python:3.12-slim \
        --image-pull-policy=IfNotPresent \
        --command -- python -c "$codigo" >/dev/null 2>&1

    for _ in $(seq 1 40); do
        fase="$(kubectl -n "$NS" get pod "$nombre" -o jsonpath='{.status.phase}' 2>/dev/null || true)"
        if [ "$fase" = "Succeeded" ] || [ "$fase" = "Failed" ]; then break; fi
        sleep 2
    done

    local salida
    salida="$(kubectl -n "$NS" logs "$nombre" 2>/dev/null || true)"
    kubectl -n "$NS" delete pod "$nombre" --now >/dev/null 2>&1 || true

    [ "$fase" = "Succeeded" ] || return 1
    printf '%s' "$salida"
}

echo "==> Smoke test contra green ($SERVICIO)"

echo "--> 1/3 responde /health y dice ser la version 2.0"
salud="$(ejecutar "
import urllib.request, json
with urllib.request.urlopen('http://$SERVICIO:8080/health', timeout=10) as r:
    print(json.load(r)['version'])
")" || fallo "green no responde /health"
salud="$(printf '%s' "$salud" | tr -d '[:space:]')"
echo "    contesta la version: $salud"
[ "$salud" = "2.0" ] || fallo "se esperaba la version 2.0 y contesto '$salud'"

echo "--> 2/3 llega a la base (/ready)"
ejecutar "
import urllib.request
urllib.request.urlopen('http://$SERVICIO:8080/ready', timeout=10)
print('ok')
" >/dev/null || fallo "green no esta Ready: no llega a la base"
echo "    la base responde"

echo "--> 3/3 el tablero devuelve datos (/api/status)"
cantidad="$(ejecutar "
import urllib.request, json
with urllib.request.urlopen('http://$SERVICIO:8080/api/status', timeout=15) as r:
    print(len(json.load(r)))
")" || fallo "green no puede leer los servicios monitoreados"
cantidad="$(printf '%s' "$cantidad" | tr -d '[:space:]')"
echo "    servicios monitoreados: $cantidad"
[ "$cantidad" -gt 0 ] 2>/dev/null || fallo "el tablero no devolvio ningun servicio"

echo
echo "Smoke test OK: green esta lista para recibir trafico."
