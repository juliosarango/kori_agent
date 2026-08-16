"""Dashboard Kori Agent (Streamlit) — lo que ve el público en pantalla.

Solo lectura: scan de demo_leads (mensajes) y demo_trazas (orquestación) cada
2s. No escribe en DynamoDB ni llama a Bedrock — todo eso ya pasó en Lambda
antes de que un item aparezca acá.

Layout de 3 columnas (ver CLAUDE.md, sección "Dashboard Streamlit —
estructura"): mensajes de Telegram | trazas de tool calls normales | panel
de seguridad (guardrail bloqueado / mensaje fuera de alcance). El panel de
seguridad va separado a propósito — es el momento más vendible del demo y
no puede quedar perdido entre las trazas de negocio.

Se proyecta en vivo frente a ~50 personas, así que la regla de oro es: nunca
crashear. Arranque en frío (tablas vacías, antes de la ronda 1) y fallos
transitorios de DynamoDB son casos esperados, no excepcionales — todo scan
va envuelto en try/except con un estado vacío como fallback, jamás un
traceback en pantalla.
"""

import html
import logging
import os
import time
from datetime import datetime
from typing import Any

import boto3
import streamlit as st
from botocore.exceptions import BotoCoreError, ClientError

logger = logging.getLogger(__name__)

AWS_REGION = os.environ.get("AWS_REGION", "us-east-1")
DYNAMO_TABLE_LEADS = os.environ.get("DYNAMO_TABLE_LEADS", "demo_leads")
DYNAMO_TABLE_TRAZAS = os.environ.get("DYNAMO_TABLE_TRAZAS", "demo_trazas")

REFRESH_SECONDS = 2
MAX_LEADS_MOSTRADOS = 12
# límites independientes por columna de trazas: la de seguridad casi
# siempre va a tener muchas menos filas que la de tools (el guardrail no
# bloquea en cada mensaje), así que un límite compartido dejaba la columna
# de tools acaparando el top-N combinado y a veces vaciaba seguridad de
# eventos que sí habían pasado. 15/10 son arbitrarios pero alcanzan de
# sobra para 14 minutos de demo con 3 rondas.
MAX_TRAZAS_TOOLS_MOSTRADAS = 15
MAX_TRAZAS_SEGURIDAD_MOSTRADAS = 10
TEXTO_MAX_CHARS = 160  # recorte para que una fila larga no reviente el layout

# Paleta de marca — ver CLAUDE.md, sección "Dashboard Streamlit — estructura"
COLOR_FONDO = "#0F1419"
COLOR_TEXTO = "#F5F3EF"
COLOR_TURQUESA = "#00B4D8"  # tools normales
COLOR_PURPURA = "#7B2D8E"  # guardrail bloqueado — el momento dramático del demo
COLOR_ORO = "#FFD700"  # highlight puntual de status "ok", 5% de la paleta

SUB_AGENTES_TOOLS = {"atencion_tool", "cotizacion_tool", "seguimiento_tool"}
# los dos casos que agrega agents/orchestrator.py sin pasar por un tool:
# bloqueo del guardrail antes de cualquier tool, y mensaje fuera de alcance
# respondido directo por el orquestador
SUB_AGENTES_SEGURIDAD = {"guardrail", "orquestador"}


# ---------------------------------------------------------------------------
# Lectura de datos — nunca deja que un fallo de AWS tumbe el render
# ---------------------------------------------------------------------------


@st.cache_resource
def _dynamodb_resource() -> Any:
    # cacheado por proceso: crear el resource no cuesta nada pero no hace
    # falta repetirlo en cada rerun de 2s
    return boto3.resource("dynamodb", region_name=AWS_REGION)


def _escanear_tabla(nombre_tabla: str) -> list[dict[str, Any]]:
    """Scan crudo de una tabla. Cualquier fallo (tabla no existe todavía,
    throttling, permisos, red) devuelve lista vacía — el caller lo pinta
    como "esperando actividad", nunca como error en pantalla.
    """
    try:
        tabla = _dynamodb_resource().Table(nombre_tabla)
        respuesta = tabla.scan()
        return respuesta.get("Items", [])
    except (ClientError, BotoCoreError) as exc:
        logger.warning("dashboard: fallo de AWS escaneando %s: %s", nombre_tabla, exc)
        return []
    except Exception:
        logger.exception("dashboard: error inesperado escaneando %s", nombre_tabla)
        return []


def _timestamp_ordenable(item: dict[str, Any]) -> str:
    # timestamp es ISO 8601 en string, así que orden lexicográfico == orden
    # cronológico. Si falta (fila corrupta/manual), la mandamos al fondo.
    return str(item.get("timestamp", ""))


def _obtener_leads() -> list[dict[str, Any]]:
    items = _escanear_tabla(DYNAMO_TABLE_LEADS)
    items.sort(key=_timestamp_ordenable, reverse=True)
    return items[:MAX_LEADS_MOSTRADOS]


def _obtener_trazas() -> list[dict[str, Any]]:
    # sin slice de top-N acá a propósito: el límite por cantidad de filas
    # se aplica por columna en _particionar_trazas, después de filtrar por
    # tipo — si cortáramos acá, una racha de tool calls podría empujar las
    # filas de seguridad (más escasas) fuera de la ventana antes de que
    # lleguen a particionarse
    items = _escanear_tabla(DYNAMO_TABLE_TRAZAS)
    items.sort(key=_timestamp_ordenable, reverse=True)
    return items


def _particionar_trazas(
    trazas: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Un solo scan de demo_trazas por ciclo (ver _obtener_trazas) — acá se
    reparte en Python entre la columna de tools y la de seguridad, cada una
    con su propio límite de filas mostradas.
    """
    tools = [t for t in trazas if str(t.get("sub_agente")) in SUB_AGENTES_TOOLS]
    seguridad = [t for t in trazas if str(t.get("sub_agente")) in SUB_AGENTES_SEGURIDAD]
    return tools[:MAX_TRAZAS_TOOLS_MOSTRADAS], seguridad[:MAX_TRAZAS_SEGURIDAD_MOSTRADAS]


# ---------------------------------------------------------------------------
# Formateo
# ---------------------------------------------------------------------------


def _hora_legible(timestamp_iso: str) -> str:
    """HH:MM:SS para lectura rápida desde la distancia — la fecha completa
    no aporta nada en un demo de 14 minutos, la hora sí.
    """
    try:
        dt = datetime.fromisoformat(timestamp_iso)
        return dt.strftime("%H:%M:%S")
    except (ValueError, TypeError):
        return timestamp_iso or "--:--:--"


def _duracion_legible(duracion_ms: Any) -> str:
    try:
        ms = float(duracion_ms)
    except (TypeError, ValueError):
        return "-- ms"
    if ms >= 1000:
        return f"{ms / 1000:.2f} s"
    return f"{ms:.0f} ms"


def _recortar(texto: str, maximo: int = TEXTO_MAX_CHARS) -> str:
    # colapsa saltos de línea/espacios a uno solo: resumen/mensaje_usuario
    # vienen de respuestas de Claude con markdown (negritas, listas, párrafos
    # con línea en blanco) y una línea en blanco adentro de nuestro <div>
    # corta el bloque HTML según CommonMark — el resto se renderiza como
    # texto literal (tags de cierre incluidos) en vez de la tarjeta. Con
    # todo en una sola línea, ese problema no puede pasar.
    texto = " ".join(texto.split())
    if len(texto) <= maximo:
        return texto
    return texto[: maximo - 3] + "..."


def _escapar(texto: Any) -> str:
    # todo lo que viene de DynamoDB pudo haber sido tipeado por un usuario
    # de Telegram (incluido el guardrail bypass intentado) — nunca se
    # inyecta crudo en el HTML del dashboard
    return html.escape(str(texto))


def _html_una_linea(bloque_html: str) -> str:
    """Defensa en profundidad además de _recortar: colapsa cualquier tarjeta
    completa a una sola línea antes de pasarla a st.markdown. Así, aunque
    algún campo nuevo se agregue sin pasar por _recortar, no puede meter una
    línea en blanco que le rompa el bloque HTML.
    """
    return " ".join(linea.strip() for linea in bloque_html.strip().splitlines() if linea.strip())


# ---------------------------------------------------------------------------
# Render — columna izquierda (demo_leads)
# ---------------------------------------------------------------------------


def _render_lead(item: dict[str, Any]) -> None:
    hora = _hora_legible(str(item.get("timestamp", "")))
    # first_name de Telegram en vez de chat_id — más presentable en pantalla
    # y no expone el ID interno. Fallback a "Cliente" para filas viejas de
    # seed_dynamodb.py que no tienen este campo (se agregó después).
    usuario = _escapar(item.get("usuario") or "Cliente")
    mensaje = _escapar(_recortar(str(item.get("mensaje_usuario", ""))))
    respuesta = _escapar(_recortar(str(item.get("respuesta", ""))))
    sub_agente = _escapar(item.get("sub_agente_usado", "kori"))
    duracion = _duracion_legible(item.get("duracion_ms"))

    st.markdown(
        _html_una_linea(
            f"""
            <div class="lead-card">
                <div class="lead-header">
                    <span class="mono-tag">{usuario}</span>
                    <span class="mono-tag mono-tiempo">{hora}</span>
                </div>
                <div class="lead-mensaje">&#128172; {mensaje or '<em>(sin mensaje)</em>'}</div>
                <div class="lead-respuesta">&#8618; {respuesta or '<em>(sin respuesta)</em>'}</div>
                <div class="lead-footer">
                    <span class="badge-agente">{sub_agente}</span>
                    <span class="mono-tag">{duracion}</span>
                </div>
            </div>
            """
        ),
        unsafe_allow_html=True,
    )


def _render_columna_leads() -> None:
    st.markdown('<h2 class="col-titulo">Mensajes de Telegram</h2>', unsafe_allow_html=True)
    leads = _obtener_leads()
    if not leads:
        st.markdown(
            '<div class="estado-vacio">Esperando actividad... aún no llegan mensajes.</div>',
            unsafe_allow_html=True,
        )
        return
    for item in leads:
        _render_lead(item)


# ---------------------------------------------------------------------------
# Render — columna derecha (demo_trazas)
# ---------------------------------------------------------------------------


def _clase_traza(sub_agente: str) -> str:
    if sub_agente == "guardrail":
        return "traza-guardrail"
    if sub_agente in SUB_AGENTES_TOOLS:
        return "traza-tool"
    return "traza-orquestador"  # orquestador sin tool (fuera de alcance, etc.)


def _render_traza(item: dict[str, Any]) -> None:
    sub_agente = str(item.get("sub_agente", "desconocido"))
    status = str(item.get("status", ""))
    hora = _hora_legible(str(item.get("timestamp", "")))
    duracion = _duracion_legible(item.get("duracion_ms"))
    evento_id = _escapar(str(item.get("evento_id", ""))[:8])
    resumen = _escapar(_recortar(str(item.get("resumen", ""))))

    clase = _clase_traza(sub_agente)
    # oro puntual solo en status "ok" — es acento, no fondo, para no saturar
    icono_status = "&#9733;" if status == "ok" else ""
    etiqueta_agente = "GUARDRAIL BLOQUEADO" if sub_agente == "guardrail" else sub_agente

    # usuario/mensaje_usuario solo existen en filas de guardrail/orquestador
    # (ver CLAUDE.md) — si no vienen, no se muestra ni un placeholder feo
    usuario = item.get("usuario")
    mensaje_usuario = item.get("mensaje_usuario")
    bloque_usuario = ""
    if usuario or mensaje_usuario:
        partes = []
        if usuario:
            partes.append(f'<span class="mono-tag">{_escapar(usuario)}</span>')
        if mensaje_usuario:
            partes.append(f'<span class="traza-mensaje-usuario">&#8220;{_escapar(_recortar(str(mensaje_usuario)))}&#8221;</span>')
        bloque_usuario = f'<div class="traza-usuario">{" ".join(partes)}</div>'

    st.markdown(
        _html_una_linea(
            f"""
            <div class="traza-card {clase}">
                <div class="traza-header">
                    <span class="badge-agente">{_escapar(etiqueta_agente)}</span>
                    <span class="mono-tag mono-tiempo">{hora}</span>
                </div>
                {bloque_usuario}
                <div class="traza-resumen">{resumen or '<em>(sin resumen)</em>'}</div>
                <div class="traza-footer">
                    <span class="mono-tag">{evento_id}</span>
                    <span class="mono-tag">{duracion}</span>
                    <span class="traza-status">{icono_status} {_escapar(status)}</span>
                </div>
            </div>
            """
        ),
        unsafe_allow_html=True,
    )


def _render_columna_trazas(titulo_html: str, trazas: list[dict[str, Any]], texto_vacio: str) -> None:
    # una sola función de render para las dos columnas de trazas (tools y
    # seguridad) — la diferencia visual entre guardrail/orquestador/tool ya
    # la resuelve _clase_traza por fila, no hace falta duplicar el bucle
    st.markdown(f'<h2 class="col-titulo">{titulo_html}</h2>', unsafe_allow_html=True)
    if not trazas:
        st.markdown(
            f'<div class="estado-vacio">{texto_vacio}</div>',
            unsafe_allow_html=True,
        )
        return
    for item in trazas:
        _render_traza(item)


# ---------------------------------------------------------------------------
# CSS — paleta de marca + tipografía. Google Fonts con @import: si el venue
# no tiene internet, el navegador simplemente cae al fallback de system
# fonts de la pila (no rompe el layout, solo no se ve tan lindo).
# ---------------------------------------------------------------------------

CSS = f"""
<style>
@import url('https://fonts.googleapis.com/css2?family=Montserrat:wght@600;700;800&family=Open+Sans:wght@400;600&family=JetBrains+Mono:wght@400;500&display=swap');

html, body, [class*="css"] {{
    background-color: {COLOR_FONDO} !important;
    color: {COLOR_TEXTO} !important;
    font-family: 'Open Sans', 'Segoe UI', sans-serif;
}}

.stApp {{
    background-color: {COLOR_FONDO};
}}

/* oculta el chrome de Streamlit que no aporta nada en pantalla de demo */
#MainMenu, footer, header {{ visibility: hidden; }}

h1, h2, h3, .col-titulo {{
    font-family: 'Montserrat', 'Segoe UI', sans-serif;
    font-weight: 700;
    color: {COLOR_TEXTO};
}}

.titulo-principal {{
    font-family: 'Montserrat', 'Segoe UI', sans-serif;
    font-weight: 800;
    font-size: 2.4rem;
    color: {COLOR_ORO};
    margin-bottom: 0;
}}

.subtitulo-principal {{
    font-family: 'Open Sans', 'Segoe UI', sans-serif;
    font-size: 1.1rem;
    color: {COLOR_TURQUESA};
    margin-top: 0;
}}

.col-titulo {{
    font-size: 1.5rem;
    border-bottom: 2px solid {COLOR_TURQUESA};
    padding-bottom: 0.4rem;
    margin-bottom: 1rem;
}}

.mono-tag {{
    font-family: 'JetBrains Mono', 'Courier New', monospace;
    font-size: 0.85rem;
    color: {COLOR_TEXTO};
    opacity: 0.85;
}}

.mono-tiempo {{
    color: {COLOR_TURQUESA};
    opacity: 1;
}}

.estado-vacio {{
    font-family: 'Open Sans', 'Segoe UI', sans-serif;
    font-size: 1.2rem;
    color: {COLOR_TEXTO};
    opacity: 0.6;
    text-align: center;
    padding: 3rem 1rem;
    border: 2px dashed {COLOR_TURQUESA};
    border-radius: 10px;
}}

/* --- tarjetas de mensajes (demo_leads) --- */
.lead-card {{
    background-color: #16202B;
    border-left: 5px solid {COLOR_TURQUESA};
    border-radius: 8px;
    padding: 0.9rem 1.1rem;
    margin-bottom: 0.9rem;
}}

.lead-header, .lead-footer {{
    display: flex;
    justify-content: space-between;
    align-items: center;
}}

.lead-mensaje {{
    font-size: 1.05rem;
    margin: 0.5rem 0 0.3rem 0;
    color: {COLOR_TEXTO};
}}

.lead-respuesta {{
    font-size: 0.95rem;
    color: {COLOR_TEXTO};
    opacity: 0.8;
    margin-bottom: 0.5rem;
}}

.badge-agente {{
    font-family: 'JetBrains Mono', 'Courier New', monospace;
    font-size: 0.8rem;
    background-color: rgba(0, 180, 216, 0.15);
    color: {COLOR_TURQUESA};
    border-radius: 999px;
    padding: 0.15rem 0.7rem;
    text-transform: uppercase;
    letter-spacing: 0.03em;
}}

/* --- tarjetas de trazas (demo_trazas) --- */
.traza-card {{
    background-color: #16202B;
    border-left: 5px solid {COLOR_TURQUESA};
    border-radius: 8px;
    padding: 0.9rem 1.1rem;
    margin-bottom: 0.9rem;
}}

.traza-tool {{
    border-left-color: {COLOR_TURQUESA};
}}

.traza-orquestador {{
    border-left-color: {COLOR_TURQUESA};
    opacity: 0.9;
}}

/* el momento dramático del demo: bloqueo del guardrail tiene que saltar
   a la vista frente al resto de las tarjetas turquesas */
.traza-guardrail {{
    border-left-color: {COLOR_PURPURA};
    border: 1px solid {COLOR_PURPURA};
    background-color: rgba(123, 45, 142, 0.18);
    box-shadow: 0 0 14px rgba(123, 45, 142, 0.45);
}}

.traza-guardrail .badge-agente {{
    background-color: {COLOR_PURPURA};
    color: {COLOR_TEXTO};
}}

.traza-usuario {{
    margin: 0.4rem 0;
    font-size: 0.95rem;
}}

.traza-mensaje-usuario {{
    font-style: italic;
    opacity: 0.9;
    margin-left: 0.4rem;
}}

.traza-resumen {{
    font-size: 1.0rem;
    margin: 0.4rem 0 0.5rem 0;
}}

.traza-footer {{
    display: flex;
    justify-content: space-between;
    align-items: center;
}}

.traza-status {{
    font-family: 'JetBrains Mono', 'Courier New', monospace;
    font-size: 0.85rem;
    color: {COLOR_ORO};
}}
</style>
"""


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def main() -> None:
    st.set_page_config(
        page_title="Kori Agent — Dashboard en vivo",
        page_icon="\U0001FA99",  # moneda dorada, gesto al nombre kori (oro)
        layout="wide",
        initial_sidebar_state="collapsed",
    )
    st.markdown(CSS, unsafe_allow_html=True)

    st.markdown('<p class="titulo-principal">Kori Agent</p>', unsafe_allow_html=True)
    st.markdown(
        '<p class="subtitulo-principal">Orquestación multi-agente en vivo — AWS Community Day Ecuador 2026</p>',
        unsafe_allow_html=True,
    )

    trazas_tools, trazas_seguridad = _particionar_trazas(_obtener_trazas())

    # anchos asimétricos: la columna de seguridad muestra menos filas y
    # tarjetas más chicas (sin bloque de usuario en el caso "tool"), no
    # necesita el mismo ancho que mensajes/tools
    col_izquierda, col_centro, col_derecha = st.columns([1, 1.15, 0.85], gap="large")
    with col_izquierda:
        _render_columna_leads()
    with col_centro:
        _render_columna_trazas(
            "Trazas de orquestación",
            trazas_tools,
            "Esperando actividad... el orquestador no registró tool calls todavía.",
        )
    with col_derecha:
        _render_columna_trazas(
            "&#128680; Seguridad",
            trazas_seguridad,
            "Esperando actividad... sin bloqueos de guardrail ni mensajes fuera de alcance.",
        )

    # refresh de 2s: nota de costo (ver CLAUDE.md) — un solo scan() de
    # demo_trazas por ciclo (se particiona en Python entre las dos
    # columnas, no se duplica la llamada a DynamoDB) más uno de demo_leads,
    # sobre tablas chicas, muy por debajo del free tier de DynamoDB
    # on-demand; no mover este intervalo sin re-chequear el presupuesto
    # (~$0.50/mes estimado para DynamoDB en el proyecto).
    time.sleep(REFRESH_SECONDS)
    st.rerun()


if __name__ == "__main__":
    main()
