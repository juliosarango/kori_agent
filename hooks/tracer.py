"""Hook de trazado (Kori Agent) — alimenta el dashboard Streamlit en vivo.

Escribe un ítem en demo_trazas por cada tool que termina de ejecutarse.
Streamlit hace scan() de esa tabla cada 2s, así que esto tiene que ser
rápido y, sobre todo, nunca puede tumbar la respuesta al usuario de
Telegram por un problema de trazado (tabla caída, permisos, etc.).

Notas sobre la API real de strands-agents 1.52 (el snippet de CLAUDE.md
es pseudocódigo, esto es lo que hay en el SDK instalado):

- `AfterToolCallEvent` sí existe en `strands.hooks` con esa firma, pero
  NO trae duración. Trae: selected_tool, tool_use (dict con name,
  toolUseId, input), result (ToolResult: content/status/toolUseId),
  exception (Exception | None si el tool lanzó), cancel_message, retry.
- Para medir duracion_ms hace falta emparejar con `BeforeToolCallEvent`
  (mismo tool_use["toolUseId"]) y calcular el delta nosotros mismos —
  el SDK no lo expone directo.
- Registrar dos eventos (before + after) requiere una clase que
  implemente el protocolo `HookProvider` (método `register_hooks`), no
  alcanza con pasar una función suelta como en el ejemplo de CLAUDE.md.
  Se sigue registrando igual: `Agent(hooks=[KoriTracerHook()])`.
- Importante: `HookRegistry.invoke_callbacks()` NO atrapa excepciones
  de los callbacks, las propaga tal cual al loop del agente. O sea que
  si este hook revienta, revienta la respuesta a Telegram. Por eso todo
  el cuerpo de los callbacks va envuelto en try/except.
- status: como nuestros 3 tools atrapan sus propios errores y devuelven
  un string de fallback en vez de lanzar, `result["status"]` desde la
  perspectiva de este hook prácticamente siempre va a ser "success" y
  `exception` va a ser None. "error" solo se vería si el lookup del tool
  falla, el usuario cancela la llamada, o se escapa una excepción no
  atrapada — casos raros pero se contemplan igual.
"""

import logging
import os
import time
import uuid
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any

import boto3
from botocore.exceptions import BotoCoreError, ClientError
from strands.hooks import AfterToolCallEvent, BeforeToolCallEvent, HookProvider, HookRegistry

logger = logging.getLogger(__name__)

AWS_REGION = os.environ.get("AWS_REGION", "us-east-1")
DYNAMO_TABLE_TRAZAS = os.environ.get("DYNAMO_TABLE_TRAZAS", "demo_trazas")

RESUMEN_MAX_CHARS = 100


def _extraer_texto_resultado(event: AfterToolCallEvent) -> str:
    """Saca el texto plano del ToolResult para armar el resumen de la traza."""
    if event.exception is not None:
        return f"error: {event.exception}"

    try:
        bloques = event.result.get("content", [])
        texto = "".join(bloque.get("text", "") for bloque in bloques if isinstance(bloque, dict))
        return texto.strip()
    except Exception:
        # el result no vino con la forma esperada — no es motivo para fallar la traza
        return ""


def _armar_resumen(event: AfterToolCallEvent) -> str:
    texto = _extraer_texto_resultado(event)
    if not texto:
        return "(sin contenido)"
    if len(texto) <= RESUMEN_MAX_CHARS:
        return texto
    return texto[:RESUMEN_MAX_CHARS - 3] + "..."


def _armar_status(event: AfterToolCallEvent) -> str:
    if event.exception is not None:
        return "error"
    if event.result.get("status") == "error":
        return "error"
    return "ok"


def escribir_traza(
    sub_agente: str,
    duracion_ms: float,
    status: str,
    resumen: str,
    *,
    usuario: str | None = None,
    mensaje_usuario: str | None = None,
) -> None:
    """Arma y escribe un item en demo_trazas con el shape estándar del
    dashboard. Reusable: además de KoriTracerHook (tools), la usa
    orchestrator.py para los dos casos que no pasan por AfterToolCallEvent
    (guardrail a nivel del orquestador, mensaje fuera de alcance sin tool).

    usuario/mensaje_usuario son opcionales a propósito: solo los llena
    orchestrator.py para esos dos casos (queremos poder mostrar "Julio
    preguntó X, quedó bloqueado" en el dashboard). Las trazas de tool calls
    (KoriTracerHook, abajo) no los pasan — DynamoDB no tiene schema fijo,
    así que esas filas simplemente no llevan esos atributos, sin tocar su
    shape de siempre."""
    item = {
        "evento_id": str(uuid.uuid4()),
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "sub_agente": sub_agente,
        "duracion_ms": Decimal(str(round(duracion_ms, 2))),
        "status": status,
        "resumen": resumen,
    }
    if usuario is not None:
        item["usuario"] = usuario
    if mensaje_usuario is not None:
        item["mensaje_usuario"] = mensaje_usuario

    try:
        dynamodb = boto3.resource("dynamodb", region_name=AWS_REGION)
        table = dynamodb.Table(DYNAMO_TABLE_TRAZAS)
        table.put_item(Item=item)
    except (ClientError, BotoCoreError) as exc:
        logger.exception("tracer: fallo de AWS escribiendo traza en %s: %s", DYNAMO_TABLE_TRAZAS, exc)
    except Exception:
        logger.exception("tracer: error inesperado escribiendo traza")


class KoriTracerHook(HookProvider):
    """HookProvider que registra BeforeToolCallEvent + AfterToolCallEvent
    para medir duración y escribir una traza en demo_trazas por cada tool
    que se ejecuta.

    Uso en agents/orchestrator.py (Tarea #11):
        orchestrator = Agent(
            model=...,
            tools=[atencion_tool, cotizacion_tool, seguimiento_tool],
            system_prompt=SYSTEM_PROMPT,
            hooks=[KoriTracerHook()],
        )
    """

    def __init__(self) -> None:
        # toolUseId -> timestamp de inicio (perf_counter, no reloj de pared)
        self._inicios: dict[str, float] = {}

    def register_hooks(self, registry: HookRegistry) -> None:
        registry.add_callback(BeforeToolCallEvent, self._on_before_tool)
        registry.add_callback(AfterToolCallEvent, self._on_after_tool)

    def _on_before_tool(self, event: BeforeToolCallEvent) -> None:
        try:
            tool_use_id = event.tool_use.get("toolUseId")
            if tool_use_id:
                self._inicios[tool_use_id] = time.perf_counter()
        except Exception:
            # ni esto puede tumbar el flujo — sin inicio registrado, el
            # cálculo de duración en el "after" cae a un fallback de 0ms
            logger.exception("tracer: fallo registrando inicio de tool call")

    def _on_after_tool(self, event: AfterToolCallEvent) -> None:
        try:
            self._escribir_traza(event)
        except Exception:
            # última línea de defensa — el trazado nunca debe romper la
            # respuesta al usuario de Telegram
            logger.exception("tracer: fallo inesperado procesando AfterToolCallEvent")

    def _escribir_traza(self, event: AfterToolCallEvent) -> None:
        tool_use_id = event.tool_use.get("toolUseId", "")
        sub_agente = event.tool_use.get("name", "desconocido")

        inicio = self._inicios.pop(tool_use_id, None)
        if inicio is not None:
            duracion_ms = round((time.perf_counter() - inicio) * 1000, 2)
        else:
            logger.warning("tracer: sin BeforeToolCallEvent pareja para toolUseId=%s", tool_use_id)
            duracion_ms = 0.0

        escribir_traza(sub_agente, duracion_ms, _armar_status(event), _armar_resumen(event))


# instancia lista para usar directo: Agent(hooks=[tracer_hook])
tracer_hook = KoriTracerHook()
