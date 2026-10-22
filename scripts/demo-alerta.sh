#!/usr/bin/env bash
#
# Demostracion del entregable 2: provocar la alerta y verla llegar.
#
# Rompe el servicio caotico a proposito (lo pone a fallar el 100% de las veces),
# fuerza varias rondas de chequeo seguidas hasta pasar el umbral, y espera a que
# la alerta recorra el camino completo: Prometheus la detecta, Alertmanager la
# entrega por webhook y la aplicacion la muestra en el tablero.
#
# Al terminar deja el servicio caotico como estaba.
#
# El umbral es mas de 5 fallos en 2 minutos y esta definido en
# k8s/11-prometheus-config.yaml.
set -euo pipefail

NS=statuspage
RONDAS="${RONDAS:-8}"
DISPARADOR="disparador-alerta"

limpiar() {
    kubectl -n "$NS" delete pod "$DISPARADOR" --now >/dev/null 2>&1 || true
}
trap limpiar EXIT INT TERM
limpiar

echo "=========================================="
echo " Demostracion de monitoreo y alertas"
echo "=========================================="
echo

echo "==> Estado antes de empezar"
activas="$(kubectl -n "$NS" exec deploy/statuspage-blue -- python -c "
import json, urllib.request
with urllib.request.urlopen('http://localhost:8080/api/alertas', timeout=10) as r:
    print(len(json.load(r)['activas']))
" 2>/dev/null || echo "?")"
echo "    alertas activas: $activas"
echo

echo "==> Rompiendo el servicio caotico (pasa a fallar siempre)"
kubectl -n "$NS" set env deploy/servicio-caotico TASA_FALLA=1.0 >/dev/null
kubectl -n "$NS" rollout status deploy/servicio-caotico --timeout=120s | tail -1
echo

echo "==> Forzando $RONDAS rondas de chequeo"
# Se corren desde adentro del cluster para pegarle al Service publico, que es
# quien reparte entre las replicas de la version activa.
kubectl -n "$NS" run "$DISPARADOR" \
    --restart=Never \
    --image=python:3.12-slim \
    --image-pull-policy=IfNotPresent \
    --command -- python -c "
import json, time, urllib.request

for i in range($RONDAS):
    pedido = urllib.request.Request('http://statuspage/api/check-now', method='POST')
    try:
        with urllib.request.urlopen(pedido, timeout=45) as r:
            datos = json.load(r)
            print('ronda', i + 1, '->', datos['chequeados'], 'chequeados,', datos['caidos'], 'caidos', flush=True)
    except Exception as e:
        print('ronda', i + 1, 'fallo:', e, flush=True)
    time.sleep(5)
" >/dev/null

kubectl -n "$NS" wait --for=condition=Ready "pod/$DISPARADOR" --timeout=120s >/dev/null
kubectl -n "$NS" logs -f "$DISPARADOR" 2>/dev/null | sed 's/^/    /'
echo

echo "==> Esperando a que la alerta recorra el camino"
echo "    Prometheus consulta cada 15s, la regla pide 30s sostenidos y"
echo "    Alertmanager agrupa 10s antes de entregar."

# Se espera a DemasiadosFallos en particular, que es la del enunciado. La otra
# alerta (ServicioCaido) suele llegar antes, porque le alcanza con que el
# servicio este en cero, y si el script cortara con la primera que aparece
# nunca mostraria la que importa.
encontrada=no
for intento in $(seq 1 30); do
    if kubectl -n "$NS" exec deploy/statuspage-blue -- python -c "
import json, sys, urllib.request
with urllib.request.urlopen('http://localhost:8080/api/alertas', timeout=10) as r:
    activas = json.load(r)['activas']
sys.exit(0 if any(a['nombre'] == 'DemasiadosFallos' for a in activas) else 1)
" >/dev/null 2>&1; then
        encontrada=si
        echo
        echo "    DemasiadosFallos llego a la aplicacion"
        break
    fi
    printf '.'
    sleep 5
done

if [ "$encontrada" = "no" ]; then
    echo
    echo "    La alerta todavia no llego. Valor actual de la expresion:"
    kubectl -n "$NS" exec deploy/prometheus -- wget -qO- \
        --post-data='query=sum by (servicio) (increase(statuspage_chequeos_total{resultado="fallo"}[2m]))' \
        'http://localhost:9090/api/v1/query' 2>/dev/null | sed 's/^/      /'
fi
echo

echo "=========================================="
echo " Alertas activas ahora"
echo "=========================================="
kubectl -n "$NS" exec deploy/statuspage-blue -- python -c "
import json, urllib.request
with urllib.request.urlopen('http://localhost:8080/api/alertas', timeout=10) as r:
    datos = json.load(r)
for a in datos['activas']:
    print(' ', a['nombre'], '|', a['severidad'], '|', a['servicio'])
    print('   ', a['descripcion'])
if not datos['activas']:
    print('  ninguna todavia. Mira el estado en Prometheus:')
    print('    minikube service prometheus -n statuspage')
" 2>/dev/null || echo "  no se pudo consultar"
echo

echo "==> Devolviendo el servicio caotico a su estado normal"
kubectl -n "$NS" set env deploy/servicio-caotico TASA_FALLA=0.2 >/dev/null
kubectl -n "$NS" rollout status deploy/servicio-caotico --timeout=120s | tail -1
echo
echo "La alerta se resuelve sola cuando dejen de acumularse fallos."
echo "Alertmanager avisa de la resolucion y el tablero la saca de la lista."
