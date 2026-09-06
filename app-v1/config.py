"""Configuracion de la aplicacion, leida desde variables de entorno.

Nada de valores hardcodeados: en Kubernetes estos valores llegan desde un
ConfigMap (version, color, ventanas) y desde un Secret (credenciales de la base).
"""

import os

# Identidad del despliegue. La imagen se construye con --build-arg y estos
# valores terminan como ENV dentro del contenedor.
APP_VERSION = os.getenv("APP_VERSION", "1.0")
APP_COLOR = os.getenv("APP_COLOR", "blue")

# Conexion a Postgres. En k8s se arma con el Secret.
DATABASE_URL = os.getenv(
    "DATABASE_URL", "postgresql://status:status@localhost:5432/statuspage"
)

# Ruta al archivo de servicios precargados (montado desde un ConfigMap).
# Si no existe, la app arranca igual y solo usa los servicios cargados por UI.
TARGETS_FILE = os.getenv("TARGETS_FILE", "/etc/statuspage/targets.json")

# Timeout por defecto de cada chequeo, en segundos.
DEFAULT_TIMEOUT = float(os.getenv("DEFAULT_TIMEOUT", "3"))
