"""Puebla las tablas DynamoDB de Kori Agent y sube los docs de RAG a S3.

Uso:
    python scripts/seed_dynamodb.py

Corre put_item por PK/SK, así que ejecutarlo varias veces sobrescribe los
mismos ítems en vez de duplicarlos (idempotente). No toca demo_trazas: esa
tabla la llena el hook de trazado en tiempo real, no el seed.
"""

import json
import logging
import os
from decimal import Decimal
from pathlib import Path
from typing import Any

import boto3

logger = logging.getLogger(__name__)
logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")

AWS_REGION = os.environ.get("AWS_REGION", "us-east-1")
DYNAMO_TABLE_PRODUCTOS = os.environ.get("DYNAMO_TABLE_PRODUCTOS", "demo_productos")
DYNAMO_TABLE_LEADS = os.environ.get("DYNAMO_TABLE_LEADS", "demo_leads")
S3_BUCKET_DOCS = os.environ.get("S3_BUCKET_DOCS", "demo-multiagente-docs")

DATA_DIR = Path(__file__).resolve().parent.parent / "data"
CATALOGO_PATH = DATA_DIR / "catalogo.json"
FAQ_PATH = DATA_DIR / "faq.md"

# telegram_id de prueba, no corresponde a un chat real - solo para que
# seguimiento_tool (Tarea #6) tenga historial contra el cual consultar
TEST_TELEGRAM_ID = "123456789"

LEADS_DE_PRUEBA: list[dict[str, Any]] = [
    {
        "telegram_id": TEST_TELEGRAM_ID,
        "timestamp": "2026-08-14T09:15:32Z",
        "mensaje_usuario": "¿Qué tipos de cerámica tienen disponibles?",
        "sub_agente_usado": "atencion_tool",
        "respuesta": "Tenemos cerámica de piso 45x45 Andina, cerámica de pared 30x60 y porcelanato 60x60 Mármol Carrara, entre otros.",
        "duracion_ms": 850,
    },
    {
        "telegram_id": TEST_TELEGRAM_ID,
        "timestamp": "2026-08-14T09:18:10Z",
        "mensaje_usuario": "Necesito cotizar 80m² de porcelanato 60x60",
        "sub_agente_usado": "cotizacion_tool",
        "respuesta": "80m² de Porcelanato 60x60 Mármol Carrara a $32.00/m² = $2,560.00.",
        "duracion_ms": 1120,
    },
]


def seed_productos(dynamodb: Any) -> int:
    """Carga catalogo.json en demo_productos, un put_item por producto."""
    table = dynamodb.Table(DYNAMO_TABLE_PRODUCTOS)
    # DynamoDB no acepta float, hay que pasar precio_unitario como Decimal
    productos = json.loads(CATALOGO_PATH.read_text(encoding="utf-8"), parse_float=Decimal)

    for producto in productos:
        table.put_item(Item=producto)
        logger.info("producto sembrado: %s", producto["producto_id"])

    return len(productos)


def seed_leads(dynamodb: Any) -> int:
    """Carga leads de prueba en demo_leads (telegram_id + timestamp como PK/SK)."""
    table = dynamodb.Table(DYNAMO_TABLE_LEADS)

    for lead in LEADS_DE_PRUEBA:
        table.put_item(Item=lead)
        logger.info("lead sembrado: %s / %s", lead["telegram_id"], lead["timestamp"])

    return len(LEADS_DE_PRUEBA)


def upload_docs_to_s3(s3_client: Any) -> int:
    """Sube faq.md y catalogo.json al bucket de docs para el RAG de atencion_tool."""
    archivos = [
        (FAQ_PATH, "faq.md", "text/markdown"),
        (CATALOGO_PATH, "catalogo.json", "application/json"),
    ]

    subidos = 0
    for path, key, content_type in archivos:
        s3_client.upload_file(
            str(path),
            S3_BUCKET_DOCS,
            key,
            ExtraArgs={"ContentType": content_type},
        )
        logger.info("subido a s3://%s/%s", S3_BUCKET_DOCS, key)
        subidos += 1

    return subidos


def main() -> int:
    session = boto3.Session(region_name=AWS_REGION)
    dynamodb = session.resource("dynamodb")
    s3_client = session.client("s3")

    productos_count = seed_productos(dynamodb)
    leads_count = seed_leads(dynamodb)
    docs_count = upload_docs_to_s3(s3_client)

    print("\n--- resumen ---")
    print(f"{DYNAMO_TABLE_PRODUCTOS}: {productos_count} ítems (PK producto_id)")
    print(f"{DYNAMO_TABLE_LEADS}: {leads_count} ítems (PK telegram_id={TEST_TELEGRAM_ID}, SK timestamp)")
    print(f"s3://{S3_BUCKET_DOCS}: {docs_count} archivos (faq.md, catalogo.json)")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
