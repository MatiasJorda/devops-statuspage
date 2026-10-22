"""Metricas en formato Prometheus.

Prometheus no recibe datos, los va a buscar: cada 15 segundos le pega a /metrics
de cada Pod y se guarda una foto de los contadores. Con esas fotos despues puede
responder cuanto subio un numero entre dos momentos, que es lo que necesita la
regla de alerta.

Se usan dos tipos:

Counter, un numero que solo sube. No dice cuanto hay ahora, dice cuanto subio
entre un momento y otro. Esa resta la hace Prometheus con increase().

Gauge, un numero que sube y baja. Sirve para el estado de ahora mismo.

Si un Pod se reinicia sus contadores vuelven a cero. increase() lo nota, porque
un Counter no puede bajar, y lo trata como reinicio en vez de como numero
negativo. Un deploy en medio de la ventana no genera una alerta falsa.
"""

from prometheus_client import CollectorRegistry, Counter, Gauge, Histogram

from . import config

# Registro propio en lugar del global, para que /metrics no venga con las
# metricas que la libreria agrega sola sobre el proceso de Python.
REGISTRO = CollectorRegistry()

# Sobre esta metrica se arma la alerta: la regla pregunta cuanto subio la serie
# con resultado="fallo" en los ultimos Y minutos y la compara contra X.
CHEQUEOS = Counter(
    "statuspage_chequeos_total",
    "Chequeos ejecutados, por servicio y resultado",
    ["servicio", "resultado"],
    registry=REGISTRO,
)

ARRIBA = Gauge(
    "statuspage_servicio_arriba",
    "1 si el ultimo chequeo del servicio salio bien, 0 si fallo",
    ["servicio"],
    registry=REGISTRO,
)

# Un histograma reparte cada medicion en cajones, y con eso se pueden calcular
# percentiles. El promedio esconde al 5% que espera tres segundos, el percentil
# 95 no.
LATENCIA = Histogram(
    "statuspage_latencia_segundos",
    "Tiempo de respuesta de cada servicio",
    ["servicio"],
    buckets=(0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0),
    registry=REGISTRO,
)

RONDAS = Counter(
    "statuspage_rondas_total",
    "Rondas de chequeo completadas",
    registry=REGISTRO,
)

MONITOREADOS = Gauge(
    "statuspage_servicios_monitoreados",
    "Servicios activos en la ultima ronda",
    registry=REGISTRO,
)

# Un Gauge que siempre vale 1 y cuya informacion esta en las etiquetas. Permite
# ver en Grafana cual de los dos colores atendio cada ronda, y por lo tanto en
# que momento se hizo el switch blue/green.
INFO = Gauge(
    "statuspage_info",
    "Version y color del despliegue que esta atendiendo",
    ["version", "color"],
    registry=REGISTRO,
)
INFO.labels(version=config.APP_VERSION, color=config.APP_COLOR).set(1)

ALERTAS = Counter(
    "statuspage_alertas_recibidas_total",
    "Alertas que llegaron desde Alertmanager",
    ["alerta", "estado"],
    registry=REGISTRO,
)


def registrar_ronda(resultados: list[tuple[dict, dict]]) -> None:
    """Vuelca en las metricas lo que paso en una ronda de chequeos."""
    RONDAS.inc()
    MONITOREADOS.set(len(resultados))

    for target, resultado in resultados:
        nombre = target["name"]
        estado = "ok" if resultado["ok"] else "fallo"

        CHEQUEOS.labels(servicio=nombre, resultado=estado).inc()
        ARRIBA.labels(servicio=nombre).set(1 if resultado["ok"] else 0)

        # La latencia solo la mide la v2, y solo cuando hubo respuesta.
        # Prometheus espera segundos por convencion.
        latencia_ms = resultado.get("latency_ms")
        if latencia_ms is not None:
            LATENCIA.labels(servicio=nombre).observe(latencia_ms / 1000)
