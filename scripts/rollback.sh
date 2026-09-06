#!/usr/bin/env bash
#
# Vuelve el trafico a la version anterior.
#
# Es el mismo patch que switch.sh pero al reves, y es instantaneo porque la
# version anterior nunca se apago: sus Pods siguieron corriendo todo el tiempo
# sin recibir trafico. Esa es la ventaja concreta del blue/green frente al
# rolling update, donde volver atras implica recrear Pods y esperar.
set -euo pipefail

NS=statuspage
SVC=statuspage

actual="$(kubectl -n "$NS" get svc "$SVC" -o jsonpath='{.spec.selector.version}')"
if [ "$actual" = "green" ]; then anterior=blue; else anterior=green; fi

echo "Rollback: $actual -> $anterior"

listos="$(kubectl -n "$NS" get deploy "statuspage-$anterior" -o jsonpath='{.status.readyReplicas}')"
if [ -z "$listos" ] || [ "$listos" -lt 1 ]; then
    echo "FALLO: statuspage-$anterior no tiene replicas Ready. No hay a donde volver." >&2
    exit 1
fi

kubectl -n "$NS" patch svc "$SVC" \
    -p "{\"spec\":{\"selector\":{\"app\":\"statuspage\",\"version\":\"$anterior\"}}}" >/dev/null

echo "Hecho. El trafico volvio a: $(kubectl -n "$NS" get svc "$SVC" -o jsonpath='{.spec.selector.version}')"
