"""Status page: API HTTP y tablero.

VERSION 1. Registra si cada servicio respondio y con que codigo, y muestra el
estado actual mas el porcentaje de disponibilidad por ventana de tiempo.

Endpoints:
  GET    /                     tablero (HTML)
  GET    /api/version          version y color de este despliegue
  GET    /api/status           estado + uptime de todos los servicios
  GET    /api/targets          lista de servicios monitoreados
  POST   /api/targets          agrega un servicio
  DELETE /api/targets/{id}     saca un servicio
  GET    /api/history/{id}     ultimos chequeos de un servicio
  POST   /api/check-now        dispara una ronda de chequeos ya mismo
  GET    /health               liveness probe: "¿el proceso esta vivo?"
  GET    /ready                readiness probe: "¿puedo atender pedidos?"

POR QUE HAY DOS ENDPOINTS DE SALUD Y NO UNO
/health contesta 200 mientras el proceso responda, aunque la base este caida:
si contestara 503 por falta de base, la liveness probe reiniciaria el Pod una y
otra vez sin arreglar nada, porque el problema esta afuera.
/ready si mira la base: sin base la app no puede mostrar nada util, asi que se
saca del Service hasta que la base vuelva. Es lo que evita mandar usuarios a un
Pod que todavia no puede atenderlos.
"""

import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import Body, FastAPI, HTTPException
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from . import checker, config, db

log = logging.getLogger("statuspage")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

ESTATICOS = Path(__file__).parent / "static"

# El arranque puede encontrarse con la base todavia levantando (en Kubernetes los
# Pods arrancan en paralelo). En vez de morir, la app queda "no lista" y reintenta
# en cada readiness probe: cuando Postgres aparece, el Pod pasa a Ready solo.
_listo = False


def asegurar_inicializacion() -> bool:
    """Crea el esquema y precarga los servicios del ConfigMap. Idempotente."""
    global _listo
    if _listo:
        return True
    try:
        db.init_pool()
        db.init_schema()
        cargados = db.seed_targets()
        log.info("base lista, %s servicios precargados desde el ConfigMap", cargados)
        _listo = True
    except Exception as exc:  # noqa: BLE001 - se reintenta en la proxima probe
        log.warning("la base todavia no esta disponible: %s", exc)
        _listo = False
    return _listo


@asynccontextmanager
async def lifespan(app: FastAPI):
    asegurar_inicializacion()
    yield
    db.close_pool()


app = FastAPI(title="Status Page", version=config.APP_VERSION, lifespan=lifespan)
app.mount("/static", StaticFiles(directory=ESTATICOS), name="static")


class NuevoTarget(BaseModel):
    """Datos para dar de alta un servicio desde la interfaz."""

    name: str = Field(min_length=1, max_length=60)
    kind: str = Field(default="http", pattern="^(http|tcp)$")
    url: str | None = None
    host: str | None = None
    port: int | None = Field(default=None, ge=1, le=65535)
    timeout_s: float = Field(default=config.DEFAULT_TIMEOUT, gt=0, le=30)
    expected_status: int = Field(default=200, ge=100, le=599)


# --- Tablero -----------------------------------------------------------------

@app.get("/", include_in_schema=False)
def tablero():
    return FileResponse(ESTATICOS / "index.html")


@app.get("/api/version")
def version():
    """Lo consume el front para pintar el header del color del despliegue activo."""
    return {"version": config.APP_VERSION, "color": config.APP_COLOR}


# --- Servicios monitoreados --------------------------------------------------

@app.get("/api/status")
def status():
    return db.status_summary()


@app.get("/api/targets")
def listar_targets():
    return db.list_targets()


@app.post("/api/targets", status_code=201)
def crear_target(nuevo: NuevoTarget):
    if nuevo.kind == "http" and not nuevo.url:
        raise HTTPException(400, "un servicio http necesita una url")
    if nuevo.kind == "tcp" and not (nuevo.host and nuevo.port):
        raise HTTPException(400, "un servicio tcp necesita host y puerto")

    try:
        return db.create_target(nuevo.model_dump())
    except Exception as exc:  # noqa: BLE001
        # El caso tipico es el nombre repetido (hay un UNIQUE en la tabla).
        raise HTTPException(409, f"no se pudo crear: {exc}") from exc


@app.delete("/api/targets/{target_id}", status_code=204)
def borrar_target(target_id: int):
    if not db.delete_target(target_id):
        raise HTTPException(404, "no existe ese servicio")


@app.get("/api/history/{target_id}")
def historial(target_id: int, limit: int = 60):
    return db.history(target_id, min(limit, 500))


# --- Chequeos ----------------------------------------------------------------

@app.post("/api/check-now")
def chequear_ahora():
    """Corre una ronda de chequeos y guarda los resultados.

    Lo llama el CronJob cada minuto y tambien el boton del tablero. Que el
    CronJob dispare este endpoint (en vez de traer su propia copia del codigo)
    hace que los chequeos SIEMPRE los ejecute la version que hoy tiene el
    trafico: despues de un switch blue/green, el comportamiento nuevo empieza a
    aplicarse solo, sin tocar el CronJob.
    """
    targets = db.list_targets(only_enabled=True)
    resultados = checker.chequear_todos(targets)

    for target, resultado in resultados:
        db.record_check(
            target["id"], resultado["ok"], resultado["status_code"], resultado["error"]
        )

    caidos = sum(1 for _, r in resultados if not r["ok"])
    log.info("ronda de chequeos: %s servicios, %s caidos", len(resultados), caidos)
    return {"chequeados": len(resultados), "caidos": caidos}


# --- Probes ------------------------------------------------------------------

@app.get("/health")
def health():
    """Liveness: si esto no contesta, el proceso esta colgado y hay que reiniciarlo."""
    return {"status": "ok", "version": config.APP_VERSION}


@app.get("/ready")
def ready():
    """Readiness: sin base, este Pod no deberia recibir trafico."""
    if not asegurar_inicializacion() or not db.ping():
        return JSONResponse({"status": "sin base"}, status_code=503)
    return {"status": "listo", "version": config.APP_VERSION}
