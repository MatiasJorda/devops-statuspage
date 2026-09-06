#!/usr/bin/env bash
#
# LA DEMOSTRACION PRINCIPAL DEL TRABAJO.
#
# Mientras un loop le pega al Service publico sin parar, se hace el switch de una
# version a la otra. Al final imprime cuantas respuestas contesto cada version y
# cuantos requests fallaron.
#
# ESE ULTIMO NUMERO TIENE QUE SER 0: es la prueba de que el cambio de version no
# corto el servicio.
#
# Como se sabe que version contesto cada request: se le pega a /health, que existe
# en las dos versiones y devuelve su numero de version. Asi cualquier respuesta
# distinta de 200 es un error real y no un endpoint que la otra version no tiene.
set -euo pipefail

NS=statuspage
RAIZ="$(cd "$(dirname "$0")/.." && pwd)"
INTERVALO="${INTERVALO:-0.2}"

TMP="$(mktemp -d)"
REGISTRO="$TMP/registro.txt"
PID_MONITOR=""
PID_TUNEL=""

limpiar() {
    [ -n "$PID_MONITOR" ] && kill "$PID_MONITOR" 2>/dev/null || true
    [ -n "$PID_TUNEL" ] && kill "$PID_TUNEL" 2>/dev/null || true
    rm -rf "$TMP"
}
trap limpiar EXIT INT TERM

# --------------------------------------------------------------------------
# URL del Service publico. Con el driver docker en macOS hay que abrir un tunel,
# que queda corriendo en segundo plano hasta el final del script.
# --------------------------------------------------------------------------
echo "==> Abriendo la conexion al Service publico"
minikube service statuspage -n "$NS" --url > "$TMP/url.txt" 2>/dev/null &
PID_TUNEL=$!

for _ in $(seq 1 30); do
    URL="$(head -1 "$TMP/url.txt" 2>/dev/null || true)"
    [ -n "$URL" ] && break
    sleep 1
done

if [ -z "${URL:-}" ]; then
    echo "No se pudo obtener la URL del Service. ¿Corriste ./scripts/deploy.sh?" >&2
    exit 1
fi
echo "    $URL"

origen="$(kubectl -n "$NS" get svc statuspage -o jsonpath='{.spec.selector.version}')"
if [ "$origen" = "blue" ]; then destino=green; else destino=blue; fi

echo
echo "=========================================="
echo " Switch blue/green: $origen -> $destino"
echo "=========================================="
echo

# --------------------------------------------------------------------------
# El monitor: pega sin parar y anota que version contesto o si fallo.
# --------------------------------------------------------------------------
monitorear() {
    while true; do
        cuerpo="$(curl -s -m 2 "$URL/health" 2>/dev/null || true)"
        if printf '%s' "$cuerpo" | grep -q '"version"'; then
            printf '%s\n' "$cuerpo" | sed -n 's/.*"version":"\([^"]*\)".*/\1/p' >> "$REGISTRO"
        else
            echo "ERROR" >> "$REGISTRO"
        fi
        sleep "$INTERVALO"
    done
}

echo "==> Monitor encendido (un request cada ${INTERVALO}s)"
monitorear &
PID_MONITOR=$!

echo "==> Midiendo 5 segundos ANTES del switch"
sleep 5
previos=$(wc -l < "$REGISTRO" | tr -d ' ')
echo "    $previos requests hasta aca"
echo

echo "==> Ejecutando el switch"
"$RAIZ/scripts/switch.sh" "$destino"
echo

echo "==> Midiendo 8 segundos DESPUES del switch"
sleep 8

kill "$PID_MONITOR" 2>/dev/null || true
PID_MONITOR=""

# --------------------------------------------------------------------------
# Resultado
# --------------------------------------------------------------------------
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
echo "Secuencia de versiones que fueron contestando (una linea por request):"
tr '\n' ' ' < "$REGISTRO" | fold -w 76 | sed 's/^/  /'
echo
echo
echo "Para volver atras: ./scripts/rollback.sh"
