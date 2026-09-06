#!/usr/bin/env bash
#
# Construye las tres imagenes del proyecto DENTRO del daemon de minikube.
#
# POR QUE "eval $(minikube docker-env)"
# minikube corre su propio daemon de Docker, separado del de la maquina. Una
# imagen construida afuera no existe adentro del cluster: los Pods quedarian en
# ErrImageNeverPull. Ese eval apunta el cliente de docker al daemon de minikube,
# asi la imagen nace ya del lado correcto y no hace falta ningun registry, ni
# cuenta en Docker Hub, ni internet.
#
# Es tambien el motivo de imagePullPolicy: IfNotPresent en los manifiestos: sin
# eso Kubernetes intentaria descargar statuspage:1.0 de Docker Hub, donde no
# existe.
set -euo pipefail

RAIZ="$(cd "$(dirname "$0")/.." && pwd)"
cd "$RAIZ"

echo "==> Apuntando docker al daemon de minikube"
if ! minikube status >/dev/null 2>&1; then
    echo "minikube no esta corriendo. Arrancalo con: minikube start" >&2
    exit 1
fi
eval "$(minikube docker-env)"

echo "==> statuspage:1.0  (version 1, azul, codigo congelado en app-v1/)"
docker build \
    --build-arg APP_DIR=app-v1 \
    --build-arg APP_VERSION=1.0 \
    --build-arg APP_COLOR=blue \
    -t statuspage:1.0 .

echo "==> statuspage:2.0  (version 2, verde, codigo actual en app/)"
docker build \
    --build-arg APP_DIR=app \
    --build-arg APP_VERSION=2.0 \
    --build-arg APP_COLOR=green \
    -t statuspage:2.0 .

echo "==> servicio-caotico:1.0"
docker build -t servicio-caotico:1.0 ./flaky

echo
echo "Listo. Imagenes dentro de minikube:"
docker images | grep -E '^(statuspage|servicio-caotico) ' || true
