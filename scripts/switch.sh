#!/usr/bin/env bash
#
# EL DEPLOY. Pasa el trafico de blue a green (o al reves).
#
# No reemplaza Pods ni reconstruye nada: cambia una etiqueta en el selector del
# Service publico. Los Pods de las dos versiones ya estan corriendo, sanos y
# conectados a la misma base. Por eso tarda milisegundos y por eso el rollback es
# instantaneo: la version anterior nunca se apago.
#
# El smoke test corre ANTES del cambio. Si la version nueva no pasa, no se mueve
# el trafico y nadie se entera de que hubo un intento de deploy.
#
#   ./scripts/switch.sh          cambia a la version que hoy NO atiende
#   ./scripts/switch.sh green    fuerza el cambio a green
#   ./scripts/switch.sh blue     fuerza el cambio a blue
set -euo pipefail

NS=statuspage
SVC=statuspage
RAIZ="$(cd "$(dirname "$0")/.." && pwd)"

actual="$(kubectl -n "$NS" get svc "$SVC" -o jsonpath='{.spec.selector.version}')"
echo "Version que atiende ahora: $actual"

if [ $# -ge 1 ]; then
    destino="$1"
else
    # Sin argumento, alterna al otro color.
    if [ "$actual" = "blue" ]; then destino=green; else destino=blue; fi
fi

if [ "$destino" = "$actual" ]; then
    echo "El trafico ya esta en $destino. No hay nada que hacer."
    exit 0
fi

case "$destino" in
    blue|green) ;;
    *) echo "Destino invalido: '$destino'. Usa blue o green." >&2; exit 1 ;;
esac

echo "Destino: $destino"
echo

# Antes de mover un solo usuario, se comprueba que el destino este sano. Solo
# tiene sentido para green, que es la version nueva y tiene su Service de prueba.
if [ "$destino" = "green" ]; then
    "$RAIZ/scripts/smoke-test.sh"
    echo
else
    echo "==> Volviendo a blue: se omite el smoke test (es la version que ya estaba en produccion)."
    echo
fi

echo "==> Comprobando que $destino tenga Pods listos"
listos="$(kubectl -n "$NS" get deploy "statuspage-$destino" -o jsonpath='{.status.readyReplicas}')"
if [ -z "$listos" ] || [ "$listos" -lt 1 ]; then
    echo "FALLO: statuspage-$destino no tiene ninguna replica Ready. No se cambia el trafico." >&2
    exit 1
fi
echo "    replicas listas: $listos"

echo "==> Moviendo el selector del Service"
kubectl -n "$NS" patch svc "$SVC" \
    -p "{\"spec\":{\"selector\":{\"app\":\"statuspage\",\"version\":\"$destino\"}}}" >/dev/null

nuevo="$(kubectl -n "$NS" get svc "$SVC" -o jsonpath='{.spec.selector.version}')"
echo
echo "Listo. El trafico ahora va a: $nuevo"
echo
echo "Para ver el tablero:  minikube service statuspage -n $NS"
echo "Si algo sale mal:     ./scripts/rollback.sh"
