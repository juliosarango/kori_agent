"""Tool de atención al cliente (Kori Agent) — RAG por context stuffing.

Sin embeddings, sin Knowledge Bases: bajamos faq.md y catalogo.json de S3,
los pegamos como contexto en el prompt y dejamos que Bedrock Haiku responda
solo con esa información. Los docs son chicos y no cambian durante la
ejecución de la Lambda, así que se cachean en memoria de proceso.
"""

import logging
import os

import boto3
from botocore.exceptions import BotoCoreError, ClientError
from strands import tool

logger = logging.getLogger(__name__)

AWS_REGION = os.environ.get("AWS_REGION", "us-east-1")
S3_BUCKET_DOCS = os.environ.get("S3_BUCKET_DOCS", "demo-multiagente-docs")
BEDROCK_SUBAGENT_MODEL = os.environ.get("BEDROCK_SUBAGENT_MODEL", "claude-haiku-4-5-20251001")
BEDROCK_GUARDRAIL_ID = os.environ.get("BEDROCK_GUARDRAIL_ID", "")
BEDROCK_GUARDRAIL_VERSION = os.environ.get("BEDROCK_GUARDRAIL_VERSION", "")

FAQ_KEY = "faq.md"
CATALOGO_KEY = "catalogo.json"

MAX_TOKENS = 512

MENSAJE_FALLBACK = (
    "No pude consultar la información de Cerámica Austral en este momento. "
    "Por favor intenta de nuevo en unos segundos."
)

SYSTEM_PROMPT = (
    "Eres el asistente de atención al cliente de Cerámica Austral, empresa "
    "cuencana de materiales de construcción y cerámica. Responde la pregunta "
    "del cliente usando ÚNICAMENTE la información del FAQ y el catálogo que "
    "se te entrega a continuación. Si la respuesta no está en ese contexto, "
    "dilo con honestidad y sugiere contactar directamente al local. Responde "
    "en español, de forma breve y directa."
)

# cache a nivel de módulo — sobrevive entre invocaciones dentro del mismo
# ciclo de vida de la Lambda (contenedor caliente)
_docs_cache: dict[str, str] | None = None


def _cargar_documentos() -> dict[str, str]:
    """Descarga faq.md y catalogo.json de S3, cacheados en memoria de proceso."""
    global _docs_cache
    if _docs_cache is not None:
        return _docs_cache

    s3_client = boto3.client("s3", region_name=AWS_REGION)

    faq_obj = s3_client.get_object(Bucket=S3_BUCKET_DOCS, Key=FAQ_KEY)
    faq_texto = faq_obj["Body"].read().decode("utf-8")

    catalogo_obj = s3_client.get_object(Bucket=S3_BUCKET_DOCS, Key=CATALOGO_KEY)
    catalogo_texto = catalogo_obj["Body"].read().decode("utf-8")

    _docs_cache = {"faq": faq_texto, "catalogo": catalogo_texto}
    logger.info("documentos de S3 cargados y cacheados (faq.md, catalogo.json)")
    return _docs_cache


def _armar_prompt(pregunta: str, docs: dict[str, str]) -> str:
    return (
        f"--- FAQ ---\n{docs['faq']}\n\n"
        f"--- CATÁLOGO (JSON) ---\n{docs['catalogo']}\n\n"
        f"--- PREGUNTA DEL CLIENTE ---\n{pregunta}"
    )


def _preguntar_a_bedrock(prompt: str) -> str:
    """Llama a Bedrock (Claude Haiku) con el contexto ya stuffeado."""
    bedrock = boto3.client("bedrock-runtime", region_name=AWS_REGION)

    response = bedrock.converse(
        modelId=BEDROCK_SUBAGENT_MODEL,
        system=[{"text": SYSTEM_PROMPT}],
        messages=[{"role": "user", "content": [{"text": prompt}]}],
        inferenceConfig={"maxTokens": MAX_TOKENS, "temperature": 0.3},
        guardrailConfig={
            "guardrailIdentifier": BEDROCK_GUARDRAIL_ID,
            "guardrailVersion": BEDROCK_GUARDRAIL_VERSION,
            "trace": "enabled",
        },
    )

    contenido = response["output"]["message"]["content"]
    texto = "".join(bloque.get("text", "") for bloque in contenido).strip()

    # el guardrail intervino (input o output) — no es un fallo técnico, es el
    # guardrail haciendo su trabajo. el texto ya viene reemplazado con el
    # BlockedInputMessaging/BlockedOutputsMessaging configurado en el guardrail.
    if response.get("stopReason") == "guardrail_intervened":
        logger.info("guardrail intervino en atencion_tool, respuesta bloqueada por diseño")

    return texto


@tool
def atencion_tool(pregunta: str) -> str:
    """Responde preguntas generales de clientes sobre Cerámica Austral: horarios,
    despacho, formas de pago, garantías, catálogo de productos disponibles y
    dudas similares. Usa el FAQ y el catálogo de la empresa como contexto
    (RAG por context stuffing, sin embeddings). Úsalo cuando el cliente
    pregunte algo informativo y no esté pidiendo una cotización específica
    ni el historial de sus solicitudes anteriores.
    """
    try:
        docs = _cargar_documentos()
        prompt = _armar_prompt(pregunta, docs)
        return _preguntar_a_bedrock(prompt)
    except (ClientError, BotoCoreError) as exc:
        logger.exception("fallo de AWS en atencion_tool: %s", exc)
        return MENSAJE_FALLBACK
    except Exception:
        logger.exception("error inesperado en atencion_tool")
        return MENSAJE_FALLBACK
