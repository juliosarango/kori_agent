"""Registra la URL del webhook de Telegram para @koriagente_bot.

Uso:
    python scripts/set_webhook.py --url https://xxxx.execute-api.us-east-1.amazonaws.com/prod/webhook/telegram
    TELEGRAM_WEBHOOK_URL=https://... python scripts/set_webhook.py

Corre setWebhook y después getWebhookInfo para confirmar que quedó bien
registrado, sin tener que ir a chequear manualmente en la API de Telegram.
"""

import argparse
import json
import os
import sys
import urllib.error
import urllib.request
from typing import Any

TELEGRAM_API_BASE = "https://api.telegram.org"


def _call_telegram(token: str, method: str, payload: dict[str, Any]) -> dict[str, Any]:
    """POST genérico a la Bot API. Lanza RuntimeError con mensaje legible si algo falla."""
    url = f"{TELEGRAM_API_BASE}/bot{token}/{method}"
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=data,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            body = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        # Telegram manda el detalle del error en el body aunque el status no sea 2xx
        try:
            body = json.loads(exc.read().decode("utf-8"))
        except Exception:
            raise RuntimeError(f"{method} falló con HTTP {exc.code} y sin body legible") from exc
    except urllib.error.URLError as exc:
        raise RuntimeError(f"no se pudo conectar a Telegram para {method}: {exc}") from exc
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"{method} devolvió una respuesta no JSON") from exc

    if not body.get("ok"):
        raise RuntimeError(f"{method} respondió ok=false: {body.get('description', 'sin descripción')}")

    return body


def _resolve_webhook_url(cli_url: str | None) -> str:
    url = cli_url or os.environ.get("TELEGRAM_WEBHOOK_URL", "")
    if not url:
        raise RuntimeError(
            "falta la URL del webhook: pásala con --url o exporta TELEGRAM_WEBHOOK_URL "
            "(usa el output WebhookUrl del stack SAM, no inventes una)"
        )
    return url


def main() -> int:
    parser = argparse.ArgumentParser(description="Registra el webhook de Telegram para Kori Agent.")
    parser.add_argument("--url", help="URL de API Gateway a registrar como webhook", default=None)
    args = parser.parse_args()

    token = os.environ.get("TELEGRAM_BOT_TOKEN", "")
    secret = os.environ.get("TELEGRAM_WEBHOOK_SECRET", "")

    if not token:
        print("ERROR: falta TELEGRAM_BOT_TOKEN en el entorno", file=sys.stderr)
        return 1
    if not secret:
        print("ERROR: falta TELEGRAM_WEBHOOK_SECRET en el entorno", file=sys.stderr)
        return 1

    try:
        webhook_url = _resolve_webhook_url(args.url)

        print(f"Registrando webhook: {webhook_url}")
        _call_telegram(token, "setWebhook", {"url": webhook_url, "secret_token": secret})
        print("setWebhook OK")

        info = _call_telegram(token, "getWebhookInfo", {})
        result = info.get("result", {})
        print("\n--- getWebhookInfo ---")
        print(f"url: {result.get('url', '(vacío)')}")
        print(f"pending_update_count: {result.get('pending_update_count', 0)}")
        last_error = result.get("last_error_message")
        if last_error:
            print(f"last_error_message: {last_error}")
        else:
            print("last_error_message: ninguno")

        return 0

    except RuntimeError as exc:
        # cualquier fallo de validación o de la API de Telegram cae acá,
        # nunca dejamos que sea un traceback crudo la única salida
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
