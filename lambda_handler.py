"""Entry point Lambda para el webhook de Telegram (Kori Agent).

API Gateway HTTP API v2 -> este handler -> orquestador Strands -> Telegram.

Nada de esto debe romperse en vivo, así que todo el flujo va envuelto en
try/except y SIEMPRE respondemos 200 a API Gateway (si le devolvemos algo
distinto de 2xx, Telegram reintenta el mismo update y duplicamos mensajes).
"""

import json
import logging
import os
import urllib.error
import urllib.request
from typing import Any

# import a nivel de módulo: en Lambda el contenedor se reusa entre
# invocaciones, así el cold start (carga de Strands/Bedrock) se paga una
# sola vez, no en cada mensaje.
from agents.orchestrator import procesar_mensaje

logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)

TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_WEBHOOK_SECRET = os.environ.get("TELEGRAM_WEBHOOK_SECRET", "")

TELEGRAM_API_BASE = "https://api.telegram.org"
MAX_TELEGRAM_MESSAGE_LEN = 4096
TRUNCATE_SUFFIX = "...\n\n(respuesta truncada, pide más detalle si quieres)"

# header en HTTP API v2 llega en minúsculas
SECRET_HEADER = "x-telegram-bot-api-secret-token"


def _ok(body: dict[str, Any]) -> dict[str, Any]:
    # siempre 200 hacia API GW, pase lo que pase adentro
    return {"statusCode": 200, "body": json.dumps(body)}


def _forbidden() -> dict[str, Any]:
    return {"statusCode": 403, "body": json.dumps({"ok": False, "error": "invalid secret"})}


def _truncate_message(text: str) -> str:
    if len(text) <= MAX_TELEGRAM_MESSAGE_LEN:
        return text
    # dejamos espacio para el sufijo
    cutoff = MAX_TELEGRAM_MESSAGE_LEN - len(TRUNCATE_SUFFIX)
    return text[:cutoff] + TRUNCATE_SUFFIX


def _send_telegram_message(chat_id: int, text: str) -> bool:
    """Manda sendMessage a la API de Telegram. Devuelve False si falla, no lanza."""
    if not TELEGRAM_BOT_TOKEN:
        logger.error("TELEGRAM_BOT_TOKEN no configurado, no puedo responder")
        return False

    url = f"{TELEGRAM_API_BASE}/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = json.dumps({"chat_id": chat_id, "text": _truncate_message(text)}).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            resp.read()  # drenar la respuesta
        return True
    except urllib.error.URLError as exc:
        # Bedrock/DynamoDB caídos ya los maneja el orquestador; esto es solo
        # el envío final a Telegram fallando (rate limit, red, etc).
        logger.error("fallo enviando mensaje a Telegram: %s", exc)
        return False


def _extract_secret_header(headers: dict[str, str] | None) -> str:
    if not headers:
        return ""
    # por las dudas buscamos case-insensitive, aunque HTTP API v2 ya normaliza a minúsculas
    for key, value in headers.items():
        if key.lower() == SECRET_HEADER:
            return value or ""
    return ""


def lambda_handler(event: dict[str, Any], context: Any) -> dict[str, Any]:
    try:
        headers = event.get("headers") or {}
        incoming_secret = _extract_secret_header(headers)

        if not TELEGRAM_WEBHOOK_SECRET or incoming_secret != TELEGRAM_WEBHOOK_SECRET:
            logger.warning("secret inválido o ausente en request de webhook")
            return _forbidden()

        raw_body = event.get("body") or "{}"
        # API GW puede mandar el body en base64 (poco común en POST JSON, pero por si acaso)
        if event.get("isBase64Encoded"):
            import base64

            raw_body = base64.b64decode(raw_body).decode("utf-8")

        try:
            update = json.loads(raw_body)
        except json.JSONDecodeError:
            logger.warning("body del webhook no es JSON válido")
            return _ok({"ok": True})

        message = update.get("message") or {}
        text = message.get("text")
        chat = message.get("chat") or {}
        chat_id = chat.get("id")

        # updates sin texto (fotos, stickers, ediciones, etc) los ignoramos
        # pero igual respondemos 200 para que Telegram no reintente
        if not text or chat_id is None:
            logger.info("update sin message.text, se ignora")
            return _ok({"ok": True})

        logger.info("mensaje de chat_id=%s: %s", chat_id, text)

        # procesar_mensaje ya trunca a 4096 y tiene su propio try/except con
        # fallback limpio (ver agents/orchestrator.py), no lo duplicamos acá
        reply = procesar_mensaje(chat_id=str(chat_id), mensaje=text)
        sent = _send_telegram_message(chat_id, reply)
        if not sent:
            logger.error("no se pudo entregar la respuesta a chat_id=%s", chat_id)

        return _ok({"ok": True})

    except Exception:
        # cualquier cosa no prevista cae acá - nunca dejamos que reviente el handler
        logger.exception("error no controlado procesando webhook")
        return _ok({"ok": True})
