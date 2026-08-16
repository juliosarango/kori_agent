"""Tool de seguimiento (Kori Agent) — historial de solicitudes previas del cliente.

Determinístico, sin LLM: es un Query directo a demo_leads por telegram_id (PK),
ordenado por timestamp (SK) descendente. No hay razonamiento que hacer acá,
solo formatear lo que ya está en DynamoDB.
"""

import logging
import os
from typing import Any

import boto3
from botocore.exceptions import BotoCoreError, ClientError
from strands import tool

logger = logging.getLogger(__name__)

AWS_REGION = os.environ.get("AWS_REGION", "us-east-1")
DYNAMO_TABLE_LEADS = os.environ.get("DYNAMO_TABLE_LEADS", "demo_leads")

MAX_RESULTADOS = 5

MENSAJE_SIN_HISTORIAL = (
    "No tienes solicitudes previas registradas todavía. "
    "¡Escríbenos con tu consulta y quedará guardada para la próxima vez!"
)

MENSAJE_FALLBACK = (
    "No pude consultar tu historial en este momento. "
    "Por favor intenta de nuevo en unos segundos."
)


def _formatear_historial(items: list[dict[str, Any]]) -> str:
    lineas = ["Este es tu historial de solicitudes recientes con Cerámica Austral:\n"]
    for item in items:
        fecha = item.get("timestamp", "fecha desconocida")
        mensaje = str(item.get("mensaje_usuario", "")).strip()
        sub_agente = item.get("sub_agente_usado", "kori")
        # recorte corto del mensaje, no hace falta repetirlo completo
        resumen = mensaje if len(mensaje) <= 100 else mensaje[:97] + "..."
        lineas.append(f"- {fecha} · {sub_agente}: \"{resumen}\"")
    return "\n".join(lineas)


def _consultar_historial(telegram_id: str) -> str:
    """Query a demo_leads por telegram_id. Lógica reutilizable, sin decorar,
    para que el orquestador la envuelva en un tool vinculado por closure
    (ver agents/orchestrator.py) en vez de exponer telegram_id al LLM.
    """
    try:
        dynamodb = boto3.resource("dynamodb", region_name=AWS_REGION)
        table = dynamodb.Table(DYNAMO_TABLE_LEADS)

        response = table.query(
            KeyConditionExpression="telegram_id = :tid",
            ExpressionAttributeValues={":tid": telegram_id},
            ScanIndexForward=False,  # timestamp descendente, más reciente primero
            Limit=MAX_RESULTADOS,
        )

        items = response.get("Items", [])
        if not items:
            return MENSAJE_SIN_HISTORIAL

        return _formatear_historial(items)

    except (ClientError, BotoCoreError) as exc:
        logger.exception("fallo de AWS en seguimiento_tool: %s", exc)
        return MENSAJE_FALLBACK
    except Exception:
        logger.exception("error inesperado en seguimiento_tool")
        return MENSAJE_FALLBACK


@tool
def seguimiento_tool(telegram_id: str) -> str:
    """Consulta el historial de solicitudes previas del cliente en Cerámica Austral.
    Úsalo cuando el cliente pregunte por su solicitud/consulta anterior o quiera
    saber qué habló con nosotros antes. Devuelve las últimas solicitudes con
    fecha y qué tipo de consulta fue (atención, cotización, seguimiento).
    """
    # NOTA DE SEGURIDAD: telegram_id NUNCA debe salir de texto libre del
    # usuario/orquestador — eso permite pedir el historial de otra persona
    # (IDOR vía tool-calling). Debe venir inyectado desde el chat_id
    # autenticado del webhook de Telegram. En producción, agents/orchestrator.py
    # NO usa este tool tal cual: arma uno vinculado por closure al chat_id real
    # y sin este parámetro expuesto al LLM (ver _crear_tools_para_chat). Este
    # wrapper queda disponible para pruebas directas/manuales del módulo.
    return _consultar_historial(telegram_id)
