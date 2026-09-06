# Status Page

Tablero que monitorea servicios y muestra si están arriba o caídos, con historial
de disponibilidad y tiempos de respuesta.

Proyecto de la materia DevOps: aplicación contenerizada, desplegada en Kubernetes,
con dos versiones que se alternan usando la estrategia **blue/green**.

## Qué hace

Cada minuto le pega a una lista de servicios y anota tres cosas: si respondió, con
qué código y cuánto tardó. Con eso el tablero muestra:

- Semáforo por servicio (verde arriba, rojo caído)
- Porcentaje de disponibilidad en tres ventanas: 1 hora, 24 horas, 7 días
- Barra de historial con los últimos 40 chequeos
- Latencia promedio (solo en la v2)
- Alta, baja, pausa y reanudación de servicios desde la interfaz

Los servicios monitoreados son los que corren en el propio cluster. Eso hace que
el tablero sirva como instrumento de medición del proyecto: durante el switch
blue/green se ve en vivo que el servicio no se cae.

## Arquitectura

```
                       ┌──────────────────────────────┐
   navegador ────────► │  Service statuspage          │
   (ver "Abrir el      │  selector: version=blue      │ ◄── el switch cambia
    tablero")          └───────────┬──────────────────┘     esta etiqueta
                                   │
                ┌──────────────────┴──────────────────┐
                ▼                                     ▼
       ┌─────────────────┐                   ┌─────────────────┐
       │ statuspage-blue │                   │ statuspage-green│
       │ imagen 1.0      │                   │ imagen 2.0      │
       │ 2 réplicas      │                   │ 2 réplicas      │
       └────────┬────────┘                   └────────┬────────┘
                │                                     │
                └──────────────┬──────────────────────┘
                               ▼
                     ┌───────────────────┐
                     │ Postgres + PVC    │  el estado vive acá,
                     │ Service ClusterIP │  fuera de los Pods
                     └───────────────────┘

       CronJob (cada minuto) ──► POST /api/check-now al Service público
                                 (lo ejecuta la versión que tiene el tráfico)

       Servicios monitoreados: nginx-demo, servicio-caotico, postgres,
                               statuspage-blue, statuspage-green
```

## Requisitos

- Docker
- minikube corriendo (`minikube start`)
- kubectl

No hace falta cuenta en ningún registry ni conexión a internet durante la demo:
las imágenes se construyen dentro del daemon de minikube.

## Levantar todo

```bash
./scripts/deploy.sh
```

Construye las dos imágenes, aplica los manifiestos en orden, espera a que todo
esté listo y dispara la primera ronda de chequeos para que el tablero no arranque
vacío.

Al terminar: el tráfico está en **blue** (v1) y **green** (v2) ya está corriendo
en paralelo, sana y sin recibir usuarios.

## Abrir el tablero

```bash
minikube service statuspage -n statuspage
```

Ese comando abre el navegador y **hay que dejarlo corriendo en su terminal**: es
el túnel. Si lo cortás, el tablero deja de responder.

Puede parecer raro teniendo un NodePort, así que vale la explicación: con el
driver Docker en macOS y en Windows, la IP del nodo de minikube (algo como
`192.168.49.2`) vive dentro de la red de Docker y **no es alcanzable desde la
máquina**. Entrar directo a `http://192.168.49.2:30090` no funciona, aunque el
Service esté perfecto. `minikube service` levanta un túnel a un puerto local
(`127.0.0.1:PUERTO_AL_AZAR`) y por ahí sí se llega.

En Linux con el driver Docker, o con los drivers de máquina virtual, la IP del
nodo sí es alcanzable y se puede entrar directo:

```bash
echo "http://$(minikube ip):30090"
```

Para ver solo la URL del túnel, sin abrir el navegador:

```bash
minikube service statuspage -n statuspage --url
```

## Probar el blue/green

```bash
./scripts/demo-blue-green.sh
```

Levanta un Pod monitor **dentro del cluster** que le pega al Service público cada
200 ms, hace el switch, y al final imprime cuántas respuestas dio cada versión y
**cuántos requests fallaron**. Ese número tiene que ser 0.

El monitor corre adentro y no en la máquina por un motivo concreto: un túnel desde
afuera (`minikube service`, `kubectl port-forward`) resuelve el Service una sola
vez, al abrirse, y después manda todos los requests al mismo Pod. Si el switch
cambia el selector, el túnel seguiría hablando con el Pod viejo y la demostración
mostraría que no pasó nada. Desde adentro, cada request pasa por kube-proxy y se
resuelve contra el selector **actual**.

Para volver atrás:

```bash
./scripts/rollback.sh
```

## Las dos versiones

| | v1 (blue) | v2 (green) |
|---|---|---|
| Imagen | `statuspage:1.0` | `statuspage:2.0` |
| Color de la cabecera | azul | verde |
| Guarda latencia | no | sí |
| Muestra latencia | no | sí |

El cambio es mínimo y visible, pero no es cosmético: la v2 necesita una columna
nueva en la base que blue y green comparten.

### Por qué la migración es aditiva

`ALTER TABLE checks ADD COLUMN IF NOT EXISTS latency_ms REAL`

La columna admite NULL. Los INSERT de la v1, que ni la mencionan, siguen siendo
válidos. Si después del switch hay que volver a blue, blue sigue escribiendo sin
problemas sobre filas que la v2 dejó con latencia.

Si el cambio hubiera sido destructivo (renombrar una columna, cambiarle el tipo,
agregarla como NOT NULL), el rollback sería imposible: quedarías obligado a seguir
adelante con una versión rota. Es el patrón expand/contract.

## Blue/green y rolling update

|  | Rolling update | Blue/green |
|---|---|---|
| Conviven las versiones | Sí, durante todo el rollout | No, el corte es atómico |
| Probar antes de exponer | No se puede | Sí, por el Service de preview |
| Velocidad del cambio | Minutos, Pod por Pod | Milisegundos, un patch |
| Rollback | Recrear los Pods viejos | Otro patch, instantáneo |
| Recursos | N + maxSurge, solo durante el rollout | 2N de forma permanente |

La ventaja concreta acá es el smoke test: `scripts/smoke-test.sh` prueba green por
su Service privado y solo si pasa se mueve el tráfico. En un rolling update un Pod
nuevo empieza a recibir usuarios apenas queda Ready.

### Reaplicar los manifiestos vuelve el tráfico a blue

Si después de un switch corrés `deploy.sh` otra vez, o un `kubectl apply -f k8s/`,
**el tráfico vuelve a blue**. No es un error: `06-services.yaml` declara
`selector: version=blue`, y `apply` hace que el cluster coincida con lo que dice
el archivo.

Es la tensión entre las dos formas de operar un cluster. El repositorio declara
cuál es el estado deseado, y el switch es un cambio imperativo hecho por fuera de
ese repositorio: el `patch` de `switch.sh` no queda escrito en ningún lado, así
que el siguiente `apply` lo pisa.

En un proyecto real se resuelve de dos maneras. O el color activo se versiona en
el repositorio y el switch es un commit (que es la idea de GitOps), o el Service
lo maneja una herramienta de despliegue progresivo (Argo Rollouts, Flagger) que
sabe que ese campo no le pertenece al manifiesto.

Para la entrega alcanza con saberlo: **después de un `apply`, revisá quién tiene
el tráfico** antes de dar la demostración.

```bash
kubectl -n statuspage get svc statuspage -o jsonpath='{.spec.selector.version}'
```

## Variables de entorno

Las lee `app/config.py`. En Kubernetes llegan desde el ConfigMap, el Secret y el
Dockerfile; en desarrollo, desde `docker-compose.yml`.

| Variable | De dónde sale | Para qué |
|---|---|---|
| `APP_VERSION` | Dockerfile (`ARG APP_VERSION`) | Qué versión dice ser. Es propiedad de la imagen, no del entorno |
| `APP_COLOR` | Dockerfile (`ARG APP_COLOR`) | Color de la cabecera: `blue` o `green` |
| `DATABASE_URL` | Deployment, armada con el Secret | Conexión a Postgres |
| `TARGETS_FILE` | Deployment | Ruta al `targets.json` montado desde el ConfigMap |
| `DEFAULT_TIMEOUT` | ConfigMap | Segundos de espera por chequeo |

`APP_VERSION` y `APP_COLOR` van en la imagen y no en el ConfigMap a propósito:
blue y green comparten el mismo ConfigMap, así que si el color viniera de ahí las
dos versiones se pintarían igual y el switch no se vería.

## Bajar todo

```bash
kubectl delete namespace statuspage
```

Borra los Deployments, los Services, el CronJob, el ConfigMap, el Secret y el
volumen de la base. Las imágenes quedan en el daemon de minikube.

Para apagar el cluster entero:

```bash
minikube stop
```

## Límites conocidos

- **La tabla `checks` crece sin límite.** Con cinco servicios y un chequeo por
  minuto son unas 7000 filas por día. No molesta en la escala del trabajo, pero en
  un uso real haría falta borrar lo viejo (un CronJob de limpieza) o pasar a una
  base pensada para series temporales.
- **La contraseña de Postgres está en el repositorio**, dentro del Secret. Es a
  propósito, para que el proyecto se levante con un solo comando, y es una
  contraseña de juguete que no protege nada. En un proyecto real se usa Sealed
  Secrets, External Secrets o Vault.
- **El switch no queda registrado en el repositorio** (ver arriba).

## Objetos de Kubernetes

| Archivo | Qué define |
|---|---|
| `00-namespace.yaml` | Namespace `statuspage` |
| `01-secret.yaml` | Contraseña de Postgres |
| `02-configmap.yaml` | Lista de servicios a monitorear y timeout |
| `03-postgres.yaml` | Deployment, PVC y Service de la base |
| `04-deployment-blue.yaml` | Versión 1, etiqueta `version: blue` |
| `05-deployment-green.yaml` | Versión 2, etiqueta `version: green` |
| `06-services.yaml` | Service público, preview de green, interno de blue |
| `07-cronjob.yaml` | Dispara los chequeos cada minuto |
| `08-demo-services.yaml` | nginx y el servicio que falla a propósito |

### Dos probes distintas y por qué

- `/health` (**liveness**) responde 200 mientras el proceso viva, aunque la base
  esté caída. Si devolviera error por falta de base, Kubernetes reiniciaría el Pod
  en loop sin arreglar nada, porque el problema está afuera.
- `/ready` (**readiness**) sí consulta la base. Sin base el Pod se saca del Service
  hasta que vuelva, en vez de mandarle usuarios que van a ver un error.

### El CronJob no tiene el código de chequeo

Solo hace `POST /api/check-now` contra el Service público. Los chequeos los ejecuta
siempre la versión que tiene el tráfico. Después del switch, la ronda siguiente
empieza a guardar latencia sin haber tocado el CronJob.

Si el CronJob trajera su propia copia del código habría que versionarlo y
patchearlo en cada switch.

## Scripts

| Script | Qué hace |
|---|---|
| `deploy.sh` | Levanta todo desde cero |
| `build-imagenes.sh` | Construye las tres imágenes dentro de minikube |
| `smoke-test.sh` | Prueba green por su Service privado |
| `switch.sh` | Smoke test y después mueve el selector del Service |
| `rollback.sh` | Vuelve a la versión anterior |
| `demo-blue-green.sh` | Switch con monitor de requests y resumen de errores |

## Desarrollo sin Kubernetes

```bash
docker compose up --build
```

Levanta la aplicación con Postgres, nginx y el servicio caótico. Tablero en
http://localhost:8080

Para levantar la v1 en vez de la v2:

```bash
APP_DIR=app-v1 APP_VERSION=1.0 APP_COLOR=blue docker compose up --build
```

## Tests

```bash
pip install -r requirements-dev.txt
pytest tests/
```

34 tests en tres archivos:

| Archivo | Qué prueba | Necesita |
|---|---|---|
| `test_checker.py` | La lógica de chequeo HTTP y TCP | Nada. Levanta servidores reales en puertos libres |
| `test_api.py` | Los códigos HTTP de la API | Nada. Reemplaza la base por funciones falsas |
| `test_db_integracion.py` | El SQL del cálculo de disponibilidad | Postgres |

Los de integración **se saltean solos** si no hay base a mano, así que
`pytest tests/` funciona en cualquier máquina. Para correrlos también:

```bash
kubectl -n statuspage port-forward svc/postgres 5432:5432 &
DATABASE_URL=postgresql://status:statuspage-demo@localhost:5432/statuspage pytest tests/
```

Cada test de integración crea su propio servicio con un nombre único y lo borra
al terminar, así que no ensucia los datos que ya estaban.

Por qué el SQL se prueba contra una base de verdad: el cálculo de disponibilidad
vive entero en una consulta, y una consulta contra objetos falsos no prueba nada.

## Estructura

```
app/                    código de la v2
app-v1/                 código de la v1, congelado
flaky/                  servicio que falla el 20% de las veces
dev/targets.json        servicios a monitorear en desarrollo local
Dockerfile              un solo Dockerfile para las dos versiones (ARG APP_DIR)
docker-compose.yml      desarrollo local sin Kubernetes
requirements-dev.txt    dependencias de los tests
k8s/                    manifiestos
scripts/                build, deploy, switch, rollback y demo
tests/                  chequeo, API e integración con la base
```

## Demostración en vivo

1. **El tablero andando.** Servicios en verde, el caótico alternando.
2. **Resiliencia.** `kubectl delete pod -n statuspage -l app=nginx-demo`: el
   semáforo se pone rojo, Kubernetes recrea el Pod, vuelve a verde solo.
3. **El switch.** `./scripts/demo-blue-green.sh`: la cabecera cambia de azul a
   verde, aparece la columna de latencia y el resumen dice 0 errores.
4. **El rollback.** `./scripts/rollback.sh`: instantáneo, y la v1 sigue
   funcionando sobre las filas que la v2 dejó con latencia.
