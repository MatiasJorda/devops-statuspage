#!/usr/bin/env bash
#
# LA DEMOSTRACION PRINCIPAL DEL TRABAJO.
#
# Mientras un monitor le pega al Service publico sin parar, se hace el switch de
# una version a la otra. Al final imprime cuantas respuestas contesto cada version
# y cuantos requests fallaron.
#
# ESE ULTIMO NUMERO TIENE QUE SER 0: es la prueba de que el cambio de version no
# corto el servicio.
#
# POR QUE EL MONITOR CORRE ADENTRO DEL CLUSTER Y NO EN LA MAQUINA
# Un tunel desde afuera (kubectl port-forward, minikube service) resuelve el
# Service UNA sola vez, al abrirse, y despues manda todos los requests al mismo
# Pod. Si el switch cambia el selector, el tunel seguiria hablando con el Pod
# viejo y la demostracion mostraria que no paso nada.
# Desde adentro, en cambio, cada request pasa por kube-proxy y se resuelve contra
# el selector ACTUAL del Service: es la unica forma de ver el cambio de verdad.
#
# Como se sabe que version contesto cada request: se le pega a /health, que existe
# en las dos versiones y devuelve su numero de version. Asi cualquier respuesta
# distinta de 200 es un error real y no un endpoint que la otra version no tiene.
set -euo pipefail

NS=statuspage
RAIZ="$(cd "$(dirname "$0")/.." && pwd)"
MONITOR="monitor-switch"
INTERVALO="${INTERVALO:-0.2}"
DURACION="${DURACION:-45}"

limpiar() {
    kubectl -n "$NS" delete pod "$MONITOR" --now >/dev/null 2>&1 || true
}
trap limpiar EXIT INT TERM
limpiar

origen="$(kubectl -n "$NS" get svc statuspage -o jsonpath='{.spec.selector.version}')"
if [ "$origen" = "blue" ]; then destino=green; else destino=blue; fi

echo "=========================================="
echo " Switch blue/green: $origen -> $destino"
echo "=========================================="
echo

# --------------------------------------------------------------------------
# El monitor: un Pod que pega sin parar durante DURACION segundos y escribe en
# su salida que version contesto, o ERROR si el request fallo.
# --------------------------------------------------------------------------
echo "==> Encendiendo el monitor dentro del cluster (${DURACION}s, un request cada ${INTERVALO}s)"
kubectl -n "$NS" run "$MONITOR" \
    --restart=Never \
    --image=python:3.12-slim \
    --image-pull-policy=IfNotPresent \
    --command -- python -c "
import json, time, urllib.request

fin = time.time() + $DURACION
while time.time() < fin:
    try:
        with urllib.request.urlopen('http://statuspage/health', timeout=2) as r:
            print(json.load(r)['version'], flush=True)
    except Exception:
        print('ERROR', flush=True)
    time.sleep($INTERVALO)
" >/dev/null

echo "--> esperando a que el monitor arranque"
kubectl -n "$NS" wait --for=condition=Ready "pod/$MONITOR" --timeout=120s >/dev/null
echo "    monitor corriendo"
echo

echo "==> Dejando correr 6 segundos ANTES del switch"
sleep 6
echo "    $(kubectl -n "$NS" logs "$MONITOR" 2>/dev/null | wc -l | tr -d ' ') requests hasta aca"
echo

echo "==> Ejecutando el switch"
"$RAIZ/scripts/switch.sh" "$destino"
echo

echo "==> Dejando correr un rato DESPUES del switch"
# Se espera a que el Pod del monitor termine sus DURACION segundos.
for _ in $(seq 1 60); do
    fase="$(kubectl -n "$NS" get pod "$MONITOR" -o jsonpath='{.status.phase}' 2>/dev/null || true)"
    [ "$fase" = "Succeeded" ] && break
    sleep 2
done

REGISTRO="$(mktemp)"
kubectl -n "$NS" logs "$MONITOR" > "$REGISTRO" 2>/dev/null

total=$(wc -l < "$REGISTRO" | tr -d ' ')
v1=$(grep -c '^1\.0$' "$REGISTRO" || true)
v2=$(grep -c '^2\.0$' "$REGISTRO" || true)
errores=$(grep -c '^ERROR$' "$REGISTRO" || true)

echo
echo "=========================================="
echo " Resultado"
echo "=========================================="
printf ' Requests totales     %s\n' "$total"
printf ' Contesto la v1.0     %s\n' "$v1"
printf ' Contesto la v2.0     %s\n' "$v2"
printf ' Errores              %s\n' "$errores"
echo "------------------------------------------"
if [ "$errores" -eq 0 ]; then
    echo " SIN DOWNTIME: ningun request fallo durante el cambio de version."
else
    echo " ATENCION: hubo $errores requests fallidos durante el cambio."
fi
echo "=========================================="
echo
echo "Secuencia de versiones que fueron contestando:"
tr '\n' ' ' < "$REGISTRO" | fold -w 74 | sed 's/^/  /'
echo
rm -f "$REGISTRO"
echo
echo "Para volver atras: ./scripts/rollback.sh"
