#!/usr/bin/env bash
#
# EL COMANDO UNICO. Levanta el proyecto entero desde cero en minikube.
#
#   ./scripts/deploy.sh
#
# Construye las dos imagenes, aplica todos los manifiestos en orden, espera a que
# todo este Ready y devuelve la URL del tablero. Al terminar, el trafico esta en
# blue (la version 1) y green ya esta corriendo en paralelo, lista para el switch.
set -euo pipefail

NS=statuspage
RAIZ="$(cd "$(dirname "$0")/.." && pwd)"
cd "$RAIZ"

echo "=========================================="
echo " Status page: despliegue completo"
echo "=========================================="
echo

if ! command -v kubectl >/dev/null 2>&1; then
    echo "Falta kubectl." >&2; exit 1
fi
if ! minikube status >/dev/null 2>&1; then
    echo "minikube no esta corriendo. Arrancalo con:" >&2
    echo "    minikube start" >&2
    exit 1
fi

echo "### 1/4  Construyendo las imagenes dentro de minikube"
"$RAIZ/scripts/build-imagenes.sh"
echo

echo "### 2/4  Aplicando los manifiestos"
# Se aplican en orden por el numero del nombre: el namespace primero, despues el
# Secret y el ConfigMap (que los Pods necesitan para arrancar), despues la base y
# recien al final la aplicacion.
kubectl apply -f k8s/
echo

echo "### 3/4  Esperando a que todo este listo"
echo "--> Postgres"
kubectl -n "$NS" rollout status deploy/postgres --timeout=180s
echo "--> Servicios monitoreados"
kubectl -n "$NS" rollout status deploy/nginx-demo --timeout=120s
kubectl -n "$NS" rollout status deploy/servicio-caotico --timeout=120s
echo "--> Aplicacion (blue y green en paralelo)"
kubectl -n "$NS" rollout status deploy/statuspage-blue --timeout=180s
kubectl -n "$NS" rollout status deploy/statuspage-green --timeout=180s
echo

echo "### 4/4  Primera ronda de chequeos"
# El CronJob corre una vez por minuto. Para no arrancar con el tablero vacio se
# dispara una ronda a mano.
#
# No se usa "kubectl run --rm -i": esa forma espera en stdin cuando no hay
# terminal interactiva y el script se cuelga. Se lanza el Pod, se espera a que
# termine y se lo borra.
kubectl -n "$NS" delete pod primera-ronda --now >/dev/null 2>&1 || true
kubectl -n "$NS" run primera-ronda \
    --restart=Never \
    --image=python:3.12-slim \
    --image-pull-policy=IfNotPresent \
    --command -- python -c "
import urllib.request
p = urllib.request.Request('http://statuspage/api/check-now', method='POST')
with urllib.request.urlopen(p, timeout=45) as r:
    print(r.read().decode())
" >/dev/null 2>&1 || true

for _ in $(seq 1 40); do
    fase="$(kubectl -n "$NS" get pod primera-ronda -o jsonpath='{.status.phase}' 2>/dev/null || true)"
    if [ "$fase" = "Succeeded" ] || [ "$fase" = "Failed" ]; then break; fi
    sleep 2
done
kubectl -n "$NS" logs primera-ronda 2>/dev/null || echo "(la primera ronda no salio; el CronJob la repite en menos de un minuto)"
kubectl -n "$NS" delete pod primera-ronda --now >/dev/null 2>&1 || true
echo

echo "=========================================="
kubectl -n "$NS" get deploy,svc,cronjob
echo
echo "Trafico actual: $(kubectl -n "$NS" get svc statuspage -o jsonpath='{.spec.selector.version}')"
echo
echo "Para abrir el tablero en el navegador:"
echo "    minikube service statuspage -n $NS"
echo
# Con el driver docker en macOS, la IP del nodo (192.168.49.2) NO es alcanzable
# desde el host: hace falta el tunel que abre "minikube service", y ese comando
# tiene que quedar corriendo en su propia terminal.
echo "Siguiente paso: ./scripts/demo-blue-green.sh"
echo "=========================================="
