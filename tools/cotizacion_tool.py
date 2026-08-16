"""Tool de cotización (Kori Agent) — sin LLM, solo DynamoDB + cálculo.

El producto llega como texto libre (lo que el orquestador extrajo del
mensaje), así que hacemos matching aproximado contra nombre/producto_id
de la tabla. La tabla es chica (9 productos), un scan completo y comparar
en Python alcanza — no hace falta índice ni búsqueda sofisticada.
"""

import difflib
import logging
import os
import unicodedata
from decimal import Decimal
from typing import Any

import boto3
from botocore.exceptions import BotoCoreError, ClientError
from strands import tool

logger = logging.getLogger(__name__)

AWS_REGION = os.environ.get("AWS_REGION", "us-east-1")
DYNAMO_TABLE_PRODUCTOS = os.environ.get("DYNAMO_TABLE_PRODUCTOS", "demo_productos")

# umbral de similitud para aceptar un match aproximado por difflib
MATCH_CUTOFF = 0.45

MENSAJE_FALLBACK = (
    "No pude generar la cotización en este momento. Por favor intenta de "
    "nuevo en unos segundos."
)


def _normalizar(texto: str) -> str:
    """Minúsculas y sin tildes, para comparar más tolerante."""
    sin_tildes = unicodedata.normalize("NFKD", texto).encode("ascii", "ignore").decode("ascii")
    return sin_tildes.lower().strip()


def _obtener_productos() -> list[dict[str, Any]]:
    dynamodb = boto3.resource("dynamodb", region_name=AWS_REGION)
    table = dynamodb.Table(DYNAMO_TABLE_PRODUCTOS)
    respuesta = table.scan()
    return respuesta.get("Items", [])


def _buscar_producto(producto: str, productos: list[dict[str, Any]]) -> dict[str, Any] | None:
    """Matching aproximado por substring y por similitud (difflib)."""
    consulta = _normalizar(producto)
    if not consulta:
        return None

    # 1. substring directo sobre nombre o producto_id (más confiable)
    for item in productos:
        nombre_norm = _normalizar(str(item.get("nombre", "")))
        id_norm = _normalizar(str(item.get("producto_id", "")))
        if consulta in nombre_norm or consulta in id_norm:
            return item

    # 2. substring inverso — la consulta trae más palabras que el nombre
    #    (ej. "porcelanato 60x60 marmol carrara premium")
    for item in productos:
        nombre_norm = _normalizar(str(item.get("nombre", "")))
        if nombre_norm and nombre_norm in consulta:
            return item

    # 3. similitud aproximada vía difflib como último recurso
    nombres_norm = [_normalizar(str(item.get("nombre", ""))) for item in productos]
    coincidencias = difflib.get_close_matches(consulta, nombres_norm, n=1, cutoff=MATCH_CUTOFF)
    if coincidencias:
        idx = nombres_norm.index(coincidencias[0])
        return productos[idx]

    return None


def _armar_desglose(
    item: dict[str, Any],
    cantidad: float,
    unidad_pedida: str,
) -> str:
    nombre = item.get("nombre", "producto")
    unidad_real = str(item.get("unidad", ""))
    precio_unitario = Decimal(str(item.get("precio_unitario", 0)))
    stock = item.get("stock_disponible")

    total = precio_unitario * Decimal(str(cantidad))

    lineas = [
        f"Cotización — {nombre}",
        f"Cantidad: {cantidad} {unidad_pedida}",
        f"Precio unitario: ${precio_unitario:.2f}/{unidad_real}",
        f"Total: ${total:.2f}",
    ]

    if unidad_real and _normalizar(unidad_pedida) != _normalizar(unidad_real):
        lineas.append(
            f"Nota: este producto se vende por {unidad_real}, no por {unidad_pedida}. "
            f"El cálculo asume {cantidad} {unidad_real}, confírmalo antes de cerrar el pedido."
        )

    if stock is not None:
        try:
            stock_num = float(stock)
            if cantidad > stock_num:
                lineas.append(
                    f"Ojo: solo tenemos {stock_num:g} {unidad_real} disponibles en stock, "
                    f"menos de lo que pides. Podemos coordinar reposición o entrega parcial."
                )
        except (TypeError, ValueError):
            pass

    return "\n".join(lineas)


@tool
def cotizacion_tool(producto: str, cantidad: float, unidad: str) -> str:
    """Genera una cotización de un producto de Cerámica Austral con precios
    actuales de DynamoDB. Úsalo cuando el cliente pida cotizar, calcular
    precio o total de un producto específico, indicando cantidad (ej. "80m²
    de porcelanato 60x60", "20 sacos de cemento"). El producto puede venir
    como texto libre, no hace falta el id exacto.
    """
    try:
        productos = _obtener_productos()
        item = _buscar_producto(producto, productos)

        if item is None:
            logger.info("cotizacion_tool: sin match para producto=%r", producto)
            return (
                f"No encontré '{producto}' en nuestro catálogo. "
                "¿Puedes confirmarme el nombre exacto del producto? Por ejemplo: "
                "cerámica de piso, porcelanato, cemento Portland, bloque de cemento."
            )

        return _armar_desglose(item, cantidad, unidad)

    except (ClientError, BotoCoreError) as exc:
        logger.exception("fallo de AWS en cotizacion_tool: %s", exc)
        return MENSAJE_FALLBACK
    except Exception:
        logger.exception("error inesperado en cotizacion_tool")
        return MENSAJE_FALLBACK
