# Dockerfile del status page.
#
# UN SOLO DOCKERFILE PARA LAS DOS VERSIONES
# Igual que en la actividad 3: cambia el directorio de codigo que se copia, no el
# Dockerfile. Asi las dos imagenes salen del mismo proceso de build (misma base,
# mismo usuario, mismos puertos) y la unica diferencia entre blue y green es el
# codigo, que es exactamente lo que se quiere demostrar.
#
#   v2 (default):  docker build -t statuspage:2.0 .
#   v1 congelada:  docker build --build-arg APP_DIR=app-v1 \
#                               --build-arg APP_VERSION=1.0 \
#                               --build-arg APP_COLOR=blue -t statuspage:1.0 .
#
# BUILD EN DOS ETAPAS
# La etapa "builder" instala las dependencias en un entorno virtual y la etapa
# final solo se lleva ese entorno ya armado. Resultado: la imagen que se despliega
# no tiene pip, ni el cache de descargas, ni los archivos temporales del build.
# Menos peso y menos superficie de ataque.

# ---------- Etapa 1: construir las dependencias ----------
FROM python:3.12-slim AS builder

# Los ARG van DESPUES del FROM: un ARG declarado antes pertenece a la resolucion
# de la imagen base y no sobrevive al FROM (llegaria vacio a los COPY).
ARG APP_DIR=app

WORKDIR /build

# Entorno virtual propio: es la unidad que se copia a la imagen final.
RUN python -m venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"

# Las dependencias se copian PRIMERO y el codigo despues. Docker cachea cada
# instruccion como una capa: mientras requirements.txt no cambie, reusa la capa
# del pip install y el build tarda segundos en vez de minutos.
COPY ${APP_DIR}/requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

# ---------- Etapa 2: la imagen que se despliega ----------
FROM python:3.12-slim AS runtime

ARG APP_DIR=app
ARG APP_VERSION=2.0
ARG APP_COLOR=green

WORKDIR /usr/local/app

# PYTHONDONTWRITEBYTECODE: no genera .pyc (no sirven, la imagen es inmutable).
# PYTHONUNBUFFERED: manda los logs directo a stdout sin buffer, para verlos en
# tiempo real con "docker logs" y "kubectl logs".
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PATH="/opt/venv/bin:$PATH"

# La version y el color son PROPIEDAD DE LA IMAGEN, no configuracion del entorno:
# por eso viajan adentro y no en el ConfigMap, que blue y green comparten. Si el
# color viniera del ConfigMap, las dos versiones se pintarian igual y el switch
# no se veria.
ENV APP_VERSION=${APP_VERSION} \
    APP_COLOR=${APP_COLOR}

LABEL org.opencontainers.image.title="statuspage" \
      org.opencontainers.image.version="${APP_VERSION}" \
      org.opencontainers.image.description="Status page de la materia DevOps" \
      devops.materia.codigo="${APP_DIR}"

# Solo el entorno virtual ya construido: sin pip, sin cache, sin compiladores.
COPY --from=builder /opt/venv /opt/venv

# El destino siempre se llama ./app, venga de app/ o de app-v1/: asi el CMD de
# abajo ("app.main:app") es identico en las dos imagenes.
COPY ${APP_DIR} ./app

# Usuario sin privilegios: si alguien vulnera la app, no es root adentro del
# contenedor. Se fija el UID 1000 para que coincida con el securityContext de los
# manifiestos de Kubernetes.
RUN useradd --create-home --uid 1000 appuser \
    && chown -R appuser:appuser /usr/local/app

USER appuser

EXPOSE 8080

# Chequeo de salud para "docker run" y docker compose (en Kubernetes esta tarea
# la hacen las probes del manifiesto). Se usa urllib de la stdlib para no tener
# que meter curl en la imagen solo para esto.
HEALTHCHECK --interval=15s --timeout=3s --start-period=10s --retries=3 \
    CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8080/health', timeout=2).status==200 else 1)"

# --host 0.0.0.0 es obligatorio: escuchando en 127.0.0.1 solo aceptaria
# conexiones desde adentro del contenedor y nadie podria entrar.
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8080"]
