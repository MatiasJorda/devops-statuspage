// Tablero del status page. JavaScript sin frameworks: se sirve tal cual, no hay
// paso de build y la imagen Docker no necesita node.
//
// VERSION 1: muestra estado y disponibilidad. La v2 agrega la latencia.

const REFRESCO_MS = 5000;   // cada cuanto se vuelve a pedir el estado
const BARRITAS = 40;        // cuantos chequeos se dibujan en la barra de historial

const $ = (sel) => document.querySelector(sel);

// Ultimo estado recibido del servidor. Lo usan los botones para saber a que
// servicio corresponden sin tener que guardar datos del usuario en el DOM.
let serviciosActuales = [];

// --- Version y color del despliegue -----------------------------------------
// Es lo que hace visible el blue/green: la v1 corre con APP_COLOR=blue y la v2
// con APP_COLOR=green, asi que al mover el selector del Service la cabecera
// cambia de color sola en el proximo refresco.
async function cargarVersion() {
  try {
    const r = await fetch("/api/version");
    const { version, color } = await r.json();
    $("#cabecera").className = color === "green" ? "green" : "";
    $("#etiqueta-version").textContent = `v${version} · ${color}`;
  } catch {
    $("#etiqueta-version").textContent = "sin conexion";
  }
}

// --- Estado de los servicios -------------------------------------------------
async function cargarEstado() {
  let servicios;
  try {
    const r = await fetch("/api/status");
    servicios = await r.json();
  } catch {
    return; // el proximo ciclo reintenta; no se rompe el tablero
  }

  if (!servicios.length) {
    $("#servicios").innerHTML =
      '<p class="vacio">No hay servicios monitoreados todavia. Agrega uno abajo.</p>';
    return;
  }

  // Se piden los historiales en paralelo: con pocos servicios es mas simple que
  // armar un endpoint que devuelva todo junto, y una demora no frena a las demas.
  const historiales = await Promise.all(
    servicios.map((s) =>
      fetch(`/api/history/${s.id}?limit=${BARRITAS}`)
        .then((r) => r.json())
        .catch(() => [])
    )
  );

  serviciosActuales = servicios;

  $("#servicios").innerHTML = servicios
    .map((s, i) => tarjeta(s, historiales[i]))
    .join("");

  // El nombre NO viaja en un atributo del boton: se busca por id en los datos que
  // ya estan en memoria. Asi ningun texto escrito por el usuario termina adentro
  // de un atributo HTML, que es donde el escapado es mas facil de equivocar.
  document.querySelectorAll(".btn-borrar").forEach((btn) => {
    const id = Number(btn.dataset.id);
    const servicio = serviciosActuales.find((s) => s.id === id);
    btn.onclick = () => borrarServicio(id, servicio ? servicio.name : "este servicio");
  });

  $("#ultimo-refresco").textContent =
    "actualizado " + new Date().toLocaleTimeString("es-AR");
}

function tarjeta(s, historial) {
  const estado = s.last_ok === null || s.last_ok === undefined
    ? "" : (s.last_ok ? "ok" : "caido");

  const destino = s.kind === "tcp" ? `${s.host}:${s.port}` : s.url;

  // Las barritas se alinean a la derecha: la mas nueva queda al final, como en
  // cualquier status page. Si hay menos chequeos que espacio, se rellena con
  // barritas grises de "sin datos".
  const relleno = Math.max(0, BARRITAS - historial.length);
  const barritas =
    '<div class="barrita"></div>'.repeat(relleno) +
    historial
      .map((c) => `<div class="barrita ${c.ok ? "ok" : "caido"}"></div>`)
      .join("");

  return `
    <article class="tarjeta">
      <div class="encabezado-tarjeta">
        <span class="punto ${estado}"></span>
        <span class="nombre">${escapar(s.name)}</span>
        <span class="destino">${escapar(destino || "")}</span>
        <span class="origen">${s.source === "configmap" ? "configmap" : "manual"}</span>
        <button class="btn-borrar" data-id="${s.id}">quitar</button>
      </div>
      <div class="historial">${barritas}</div>
      <div class="metricas">
        <span>1h <b>${pct(s.uptime_1h)}</b></span>
        <span>24h <b>${pct(s.uptime_24h)}</b></span>
        <span>7d <b>${pct(s.uptime_7d)}</b></span>
        ${s.last_status ? `<span>ultimo codigo <b>${s.last_status}</b></span>` : ""}
      </div>
      ${s.last_ok === false && s.last_error
        ? `<div class="motivo-caida">${escapar(s.last_error)}</div>` : ""}
    </article>`;
}

const pct = (v) => (v === null || v === undefined ? "s/d" : `${Number(v).toFixed(1)}%`);

// Los nombres y las URLs los escribe el usuario: se escapan antes de meterlos en
// el HTML para no abrir un XSS en el propio tablero.
//
// OJO CON textContent SOLO: escapa &, < y >, pero NO las comillas. Eso alcanza
// cuando el valor va como texto entre etiquetas, pero NO cuando va adentro de un
// atributo: un nombre como  x" onmouseover="alert(1)  cerraria el atributo e
// inyectaria un manejador de eventos. Por eso se escapan tambien las comillas.
function escapar(texto) {
  const div = document.createElement("div");
  div.textContent = texto ?? "";
  return div.innerHTML.replace(/"/g, "&quot;").replace(/'/g, "&#39;");
}

// --- Acciones ----------------------------------------------------------------
async function chequearAhora() {
  const btn = $("#btn-chequear");
  btn.disabled = true;
  $("#aviso").textContent = "chequeando...";
  try {
    const r = await fetch("/api/check-now", { method: "POST" });
    const { chequeados, caidos } = await r.json();
    $("#aviso").textContent = `${chequeados} chequeados, ${caidos} caidos`;
    await cargarEstado();
  } catch {
    $("#aviso").textContent = "no se pudo chequear";
  } finally {
    btn.disabled = false;
    setTimeout(() => ($("#aviso").textContent = ""), 4000);
  }
}

async function borrarServicio(id, nombre) {
  if (!confirm(`¿Dejar de monitorear "${nombre}"?`)) return;
  await fetch(`/api/targets/${id}`, { method: "DELETE" });
  cargarEstado();
}

$("#select-tipo").onchange = (e) => {
  const esTcp = e.target.value === "tcp";
  $("#campos-http").classList.toggle("oculto", esTcp);
  $("#campos-tcp").classList.toggle("oculto", !esTcp);
};

$("#form-alta").onsubmit = async (e) => {
  e.preventDefault();
  $("#error-alta").textContent = "";

  const datos = Object.fromEntries(new FormData(e.target));
  const cuerpo = { name: datos.name, kind: datos.kind };
  if (datos.kind === "tcp") {
    cuerpo.host = datos.host;
    cuerpo.port = Number(datos.port);
  } else {
    cuerpo.url = datos.url;
  }

  const r = await fetch("/api/targets", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(cuerpo),
  });

  if (r.ok) {
    e.target.reset();
    cargarEstado();
  } else {
    const err = await r.json().catch(() => ({}));
    $("#error-alta").textContent = err.detail || "no se pudo agregar";
  }
};

$("#btn-chequear").onclick = chequearAhora;

cargarVersion();
cargarEstado();
setInterval(cargarEstado, REFRESCO_MS);
setInterval(cargarVersion, REFRESCO_MS);   // detecta el switch blue/green
