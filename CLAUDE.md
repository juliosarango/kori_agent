# CLAUDE.md — Kori Agent
## Proyecto: AWS Community Day Ecuador 2026 — Cuenca, 5 de septiembre

---

## Contexto del proyecto

**Kori Agent** es un sistema multi-agente construido sobre AWS que recibe mensajes por Telegram, clasifica la intención del cliente y delega automáticamente a sub-agentes especializados — todo visible en un dashboard en tiempo real.

El nombre viene del quechua *kori* (oro). Representa decisiones que valen oro: autonomía, precisión, sin intervención humana.

Este repositorio es el demo en vivo para la charla:
**"De mensaje a negocio: orquestación multi-agente con AWS para automatizar lo que más te cuesta"**
AWS Community Day Ecuador 2026 — Cuenca, 5 de septiembre.

El handle de Telegram es `@koriagente_bot`. El nombre visible para los usuarios es **Kori Agent**.

**Restricción crítica:** el demo debe funcionar sin errores en vivo frente a ~50 personas. Estabilidad primero, sofisticación después.

---

## Stack

| Capa | Tecnología |
|---|---|
| Lenguaje | Python 3.12 |
| Mensajería | Telegram Bot API (webhook) |
| Gateway | AWS API Gateway (HTTP API) |
| Agentes | Strands Agents SDK |
| LLM orquestador | Amazon Bedrock — Claude Sonnet (`us.anthropic.claude-sonnet-4-6`, inference profile) |
| LLM sub-agentes | Amazon Bedrock — Claude Haiku (`us.anthropic.claude-haiku-4-5-20251001-v1:0`, inference profile) |
| Base de datos | DynamoDB (3 tablas — ver abajo) |
| Documentos RAG | S3 (context stuffing, sin embeddings) |
| Dashboard | Streamlit — reusa la instancia EC2 `n8n-app` (`i-052bd40fd9843cc27`, t3.small) ya corriendo, puerto 8501. No se levanta instancia nueva. |
| Observabilidad | Strands hooks + CloudWatch |
| Infra como código | AWS SAM (template.yaml) |

**No usar:** n8n, LangChain, LangGraph, OpenSearch, embeddings, Bedrock Knowledge Bases. El scope está cerrado.

---

## Arquitectura

```
Telegram
    ↓
API Gateway (webhook POST)
    ↓
Lambda — lambda_handler.py
    ↓
Orquestador (Strands Agent + Bedrock Sonnet)
    ↓ clasifica intención y llama el tool correcto
    ├── atencion_tool()     → RAG sobre S3 (FAQ + catálogo)
    ├── cotizacion_tool()   → consulta DynamoDB tabla productos
    └── seguimiento_tool()  → consulta DynamoDB tabla leads
    ↓
Hook AfterToolCallEvent → escribe traza en DynamoDB tabla trazas
    ↓
Respuesta → Telegram
    ↓ (paralelo)
Streamlit lee tabla trazas cada 2s → dashboard en pantalla del speaker
```

---

## Estructura del proyecto

```
demo-multiagente-aws/
├── CLAUDE.md                  # Este archivo
├── README.md                  # Guía práctica de despliegue (setup, build, deploy, seed, webhook)
├── agents/
│   └── orchestrator.py        # Strands Agent principal con los 3 tools
├── tools/
│   ├── atencion_tool.py       # RAG: carga docs de S3, pasa como contexto
│   ├── cotizacion_tool.py     # Consulta tabla 'productos' en DynamoDB
│   └── seguimiento_tool.py    # Consulta tabla 'leads' en DynamoDB
├── hooks/
│   └── tracer.py              # AfterToolCallEvent → escribe en tabla 'trazas'
├── data/
│   ├── catalogo.json          # Productos de la empresa ficticia
│   ├── precios.json           # Tabla de precios
│   └── faq.md                 # Preguntas frecuentes para el RAG
├── dashboard/
│   └── app.py                 # Streamlit — 2 columnas: mensajes + trazas
├── scripts/
│   ├── seed_dynamodb.py       # Poblar las 3 tablas con datos de prueba
│   └── set_webhook.py         # Registrar URL del webhook en Telegram
├── lambda_handler.py          # Entry point Lambda — recibe webhook Telegram
├── requirements.txt
├── template.yaml              # SAM — define Lambda, API GW, DynamoDB, S3
└── .env.example               # Variables de entorno requeridas
```

---

## DynamoDB — 3 tablas

### `demo_productos`
Catálogo y precios de la empresa ficticia.
```
PK: producto_id (String)
Atributos: nombre, descripcion, precio_unitario, unidad, stock_disponible
```

### `demo_leads`
Registro de cada interacción. Persiste entre rondas del demo.
```
PK: telegram_id (String)
SK: timestamp (String — ISO 8601)
Atributos: mensaje_usuario, sub_agente_usado, respuesta, duracion_ms
```

### `demo_trazas`
Alimenta el dashboard Streamlit en tiempo real.
```
PK: evento_id (String — uuid4)
Atributos: timestamp, sub_agente, duracion_ms, status, resumen
```
Streamlit hace `scan` de esta tabla cada 2 segundos ordenado por timestamp desc.

---

## Sincronización de datos: catálogo (DynamoDB ↔ S3)

El catálogo de productos vive **duplicado en dos lugares**, a propósito, por decisión de arquitectura (ver "Lo que NO hacer" — sin embeddings ni Knowledge Bases):

- **DynamoDB `demo_productos`** — consultado en vivo por `cotizacion_tool` (precio/stock exactos).
- **S3 `catalogo.json`** — snapshot estático leído por `atencion_tool` para RAG por context stuffing (respuestas informativas generales).

Ambos se siembran **desde el mismo archivo local `data/catalogo.json`**, en la misma corrida de `scripts/seed_dynamodb.py`. Eso es lo único que los mantiene sincronizados.

**Regla del proyecto: `data/catalogo.json` es la única fuente de verdad. Nunca se edita `demo_productos` directamente** (ni por consola AWS, ni por CLI, ni por ningún script que no sea `seed_dynamodb.py`).

**Flujo correcto para agregar o cambiar un producto:**
1. Editar `data/catalogo.json` (agregar producto, cambiar precio/stock).
2. Correr `python scripts/seed_dynamodb.py` de nuevo — es idempotente (`put_item` por `producto_id`), sobrescribe DynamoDB y re-sube el JSON a S3 en la misma ejecución.

**Por qué importa:** si se edita `demo_productos` directamente sin pasar por este flujo, `cotizacion_tool` (lee DynamoDB) y `atencion_tool` (lee el snapshot viejo en S3) quedan desincronizados — el bot cotizaría un producto que, según el FAQ/catálogo que recita, "no existe" (o viceversa).

**Nota de cache:** `atencion_tool` cachea el contenido de S3 en memoria del proceso Lambda (`_docs_cache`, a nivel de módulo). Un contenedor Lambda ya "caliente" no ve un `catalogo.json` actualizado hasta que ese contenedor se recicle (cold start). Para el demo no es problema — los datos se siembran antes del evento, no durante — pero es relevante si se resiembra data cerca de un ensayo en vivo.

---

## Variables de entorno (.env / Lambda environment)

```
TELEGRAM_BOT_TOKEN=
TELEGRAM_WEBHOOK_SECRET=
AWS_REGION=us-east-1
BEDROCK_ORCHESTRATOR_MODEL=us.anthropic.claude-sonnet-4-6
BEDROCK_SUBAGENT_MODEL=us.anthropic.claude-haiku-4-5-20251001-v1:0
DYNAMO_TABLE_PRODUCTOS=demo_productos
DYNAMO_TABLE_LEADS=demo_leads
DYNAMO_TABLE_TRAZAS=demo_trazas
S3_BUCKET_DOCS=demo-multiagente-docs
```

---

## Despliegue

Guía completa (setup, primer deploy, seed, webhook) en `README.md`. Acá solo los dos gotchas que ya mordieron una vez, para no repetirlos:

**Build:** siempre `sam build --use-container`, nunca sin ese flag. El `Makefile` (`Metadata.BuildMethod: makefile` en `template.yaml`) instala `requirements.txt` con `pip3 install` directo — sin contenedor, dependencias con extensiones compiladas (ej. `pydantic_core`, de Rust/maturin) generan wheels con el ABI del sistema local en vez del runtime real de Lambda (Amazon Linux + Python 3.12), y la Lambda revienta al arrancar.

**Deploy:** `samconfig.toml` está trackeado en git y sus `parameter_overrides` para `TelegramBotToken`/`TelegramWebhookSecret` son placeholders (`"PENDIENTE-configurar-..."`) **a propósito** — nunca se completan ahí con valores reales, eso comitearía secretos en texto plano al repo. Cada deploy sobre el stack ya existente tiene que reusar los valores ya guardados, sin reescribirlos:

```bash
sam deploy \
  --parameter-overrides \
    'ParameterKey=TelegramBotToken,UsePreviousValue=true' \
    'ParameterKey=TelegramWebhookSecret,UsePreviousValue=true' \
  --no-confirm-changeset
```

`UsePreviousValue=true` le dice a CloudFormation que no toque ese parámetro. Sin esto, el deploy tomaría el placeholder literal del `samconfig.toml` y rompería `@koriagente_bot` en producción. Verificado con dry-run (`--no-execute-changeset` + `describe-change-set`): con este flag el changeset solo toca `Code` de la Lambda, nunca `Environment`.

---

## Empresa ficticia del demo

**Cerámica Austral** — empresa cuencana de materiales de construcción y cerámica.

Usar este contexto para todos los datos de prueba, el FAQ y el catálogo. El empresario de Cuenca reconoce este tipo de negocio de inmediato.

**Catálogo ejemplo:**
- Cerámica de piso 45x45 Colección Andina — $18.50/m²
- Cerámica de pared 30x60 Blanco Liso — $12.00/m²
- Bloque de cemento 15x20x40 — $0.45/unidad
- Cemento Portland 50kg — $9.80/saco
- Porcelanato 60x60 Mármol Carrara — $32.00/m²

**FAQ ejemplo:**
- Horario de atención: lunes a sábado 8h–18h
- Despacho a domicilio: disponible en Cuenca y área metropolitana
- Tiempo de entrega: 24–48 horas para pedidos en stock
- Formas de pago: efectivo, transferencia, tarjeta de crédito

---

## Flujos del demo en vivo

**Ronda 1 — Atención:**
Usuario: *"¿Qué tipos de cerámica tienen disponibles?"*
→ Orquestador detecta intención CONSULTA
→ Llama `atencion_tool`
→ RAG responde con catálogo desde S3

**Ronda 2 — Cotización:**
Usuario: *"Necesito cotizar 80m² de porcelanato 60x60"*
→ Orquestador detecta intención COTIZACIÓN
→ Llama `cotizacion_tool`
→ Extrae producto y cantidad, calcula total, responde con desglose

**Ronda 3 — Seguimiento:**
Usuario: *"¿Cuál fue mi solicitud anterior?"*
→ Orquestador detecta intención SEGUIMIENTO
→ Llama `seguimiento_tool`
→ Consulta DynamoDB por telegram_id, devuelve historial

---

## Convenciones de código

- **Tipado:** usar type hints en todas las funciones
- **Async:** Lambda handler síncrono; los tools son síncronos para simplicidad
- **Errores:** cada tool debe tener try/except con fallback limpio — nunca exponer traceback al usuario de Telegram
- **Logging:** usar `logger = logging.getLogger(__name__)` en cada módulo; los logs van a CloudWatch automáticamente desde Lambda
- **Respuestas Telegram:** máximo 4096 caracteres; si la respuesta es larga, truncar con "..." y ofrecer más detalle
- **Tokens Bedrock:** limitar max_tokens a 512 en sub-agentes, 256 en orquestador (solo clasifica, no genera texto largo)

---

## Strands Agents — patrón base

```python
from strands import Agent, tool
from strands.hooks import AfterToolCallEvent

@tool
def atencion_tool(pregunta: str) -> str:
    """Responde preguntas generales sobre Cerámica Austral usando el catálogo y FAQ."""
    ...

@tool
def cotizacion_tool(producto: str, cantidad: float, unidad: str) -> str:
    """Genera una cotización con precios actuales de DynamoDB."""
    ...

@tool
def seguimiento_tool(telegram_id: str) -> str:
    """Consulta el historial de solicitudes previas del cliente."""
    ...

orchestrator = Agent(
    model="claude-sonnet-4-6",  # via Bedrock
    tools=[atencion_tool, cotizacion_tool, seguimiento_tool],
    system_prompt=SYSTEM_PROMPT,
    hooks=[tracer_hook],
)
```

---

## Dashboard Streamlit — estructura

```python
# dashboard/app.py
# Layout: 2 columnas
# Columna izquierda:  mensajes recientes de Telegram (tabla demo_leads)
# Columna derecha:    trazas de orquestación (tabla demo_trazas)
# Refresh: st.rerun() cada 2 segundos
```

**Paleta de marca (Julio Sarango — "IA con identidad"):**

| Nombre | Hex | Uso |
|---|---|---|
| Noche Andina | `#0F1419` | 60% — fondo |
| Turquesa Cloud | `#00B4D8` | 25% — acento principal (tool calls normales: atención/cotización/seguimiento) |
| Púrpura Saraguro | `#7B2D8E` | 10% — acento secundario (trazas `guardrail` bloqueado — para que salte a la vista) |
| Oro Inti | `#FFD700` | 5% — highlight puntual de éxito, no fondo |
| Hueso | `#F5F3EF` | texto sobre fondos oscuros |

**Tipografía:** Montserrat (titulares) / Open Sans (cuerpo) / JetBrains Mono (datos técnicos: `duracion_ms`, `evento_id`, `chat_id`, etc.)

---

## Fases de construcción

### Semana 1 — 15 al 22 ago | Infraestructura base — ✅ COMPLETA (15 ago)
- [x] `template.yaml` SAM con Lambda + API GW + 3 tablas DynamoDB + S3
- [x] `lambda_handler.py` recibe webhook y responde echo a Telegram
- [x] `scripts/set_webhook.py` registra URL en BotFather
- [x] `scripts/seed_dynamodb.py` puebla tablas con datos de Cerámica Austral
- [x] Subir `faq.md` y `catalogo.json` a S3
- [x] Validar: `/start` en Telegram → Lambda responde

### Semana 2 — 22 al 29 ago | Agentes — ✅ COMPLETA (16 ago, adelantada)
- [x] `tools/atencion_tool.py` con RAG (S3 context stuffing) + Bedrock Guardrail
- [x] `tools/cotizacion_tool.py` con consulta DynamoDB
- [x] `tools/seguimiento_tool.py` con consulta DynamoDB
- [x] `agents/orchestrator.py` con Strands + 3 tools + hook trazas + guardrail
- [x] Integrar orquestador en `lambda_handler.py`
- [x] Pruebas end-to-end de los 3 flujos desde Telegram real

### Semana 3 — 29 ago al 4 sep | Dashboard y ensayo — EN CURSO
- [x] `dashboard/app.py` Streamlit funcional con datos reales (16 ago)
- [ ] Deploy Streamlit en EC2 — reusando instancia `n8n-app` ya corriendo (`i-052bd40fd9843cc27`), no una t3.micro nueva. Puerto 8501 ya abierto en el security group `launch-wizard-7`. Falta: instalar deps + correr el proceso en la instancia (ver `README.md`).
- [ ] Ensayo completo cronometrado (objetivo: 3 rondas en 14 minutos)
- [ ] Documentar Plan B (qué hacer si Telegram falla: usar curl o Postman en pantalla)
- [ ] Ensayo grabado sin errores → señal de go para el evento

**Próximo paso al retomar:** desplegar `dashboard/app.py` en la instancia EC2 `n8n-app` (pasos en `README.md`) — falta resolver cómo la instancia obtiene credenciales de AWS para leer DynamoDB (no tiene IAM instance profile asociado hoy). El sistema completo (webhook → orquestador → 3 tools → guardrail → trazas) ya funciona en producción, probado desde Telegram real.

---

## Plan B para el evento

Si Telegram falla por conectividad en el venue:
1. Tener Postman configurado con requests pre-cargados simulando los 3 mensajes
2. El dashboard Streamlit sigue funcionando — la orquestación es visible igual
3. Mencionar en voz alta: *"Telegram es el canal, no el sistema — el sistema sigue corriendo"*

---

## Presupuesto estimado

| Servicio | Costo/mes |
|---|---|
| Lambda + API Gateway | < $0.10 |
| Bedrock Haiku (sub-agentes) | ~$0.40 |
| Bedrock Sonnet (orquestador) | ~$3.00 |
| DynamoDB on-demand | ~$0.50 |
| S3 | ~$0.01 |
| EC2 (Streamlit) | $0 incremental — reusa `n8n-app`, ya corriendo por otro motivo |
| **Total** | **~$4.01/mes** |

Con créditos AWS Community Builder: $0 durante desarrollo y el evento.
Costo del demo en vivo (50 personas, 3 rondas): ~$0.15.

---

## Lo que NO hacer

- No usar embeddings ni Bedrock Knowledge Bases — context stuffing es suficiente y más simple
- No agregar n8n — Lambda + Strands cubre todo sin capas extra
- No usar Step Functions — el orquestador de Strands maneja el routing
- No generar respuestas largas en sub-agentes — max_tokens 512, respuestas directas
- No exponer errores técnicos al usuario de Telegram — siempre fallback limpio

---

*Proyecto: Kori Agent*
*Repositorio: kori-agent*
*Evento: AWS Community Day Ecuador 2026 — Cuenca, 5 de septiembre*
*Owner: Julio Sarango — IA con identidad*