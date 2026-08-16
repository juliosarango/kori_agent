# Kori Agent

Sistema multi-agente sobre AWS que recibe mensajes por Telegram, clasifica la intención del cliente (atención, cotización, seguimiento) y delega a sub-agentes especializados construidos con Strands Agents + Amazon Bedrock. Demo en vivo para AWS Community Day Ecuador 2026 — Cuenca, 5 de septiembre.

Handle de Telegram: `@koriagente_bot`.

El contexto completo del proyecto (arquitectura, convenciones, estado de las fases) vive en [`CLAUDE.md`](./CLAUDE.md). Este README es solo la guía práctica para desplegar.

---

## Requisitos previos

- Python 3.12 (el runtime real de Lambda — para desarrollo local alcanza con lo que tengas, ver nota de Docker abajo).
- Docker corriendo (para `sam build --use-container` — ver por qué es obligatorio en la sección de build).
- AWS CLI configurado con credenciales que tengan permisos sobre CloudFormation, Lambda, API Gateway, DynamoDB, S3 y Bedrock.
- Un entorno con `aws-sam-cli` instalado. Este repo trae uno en `.venv/`:
  ```bash
  python3 -m venv .venv
  .venv/bin/pip install aws-sam-cli
  ```
  Todos los comandos `sam`/`aws` de abajo asumen `.venv/bin/sam` y `aws` (CLI global) — ajustá el path según tu setup.
- Un bot de Telegram creado con [@BotFather](https://t.me/BotFather) — de ahí sale el `TELEGRAM_BOT_TOKEN`. El `TELEGRAM_WEBHOOK_SECRET` lo generás vos (cualquier string random largo, ej. `openssl rand -base64 32`).
- Cuenta de Bedrock con acceso habilitado a Claude Sonnet y Haiku, y tarjeta válida asociada — Bedrock se factura vía AWS Marketplace, los créditos promocionales normales no lo cubren.

---

## Primer despliegue (stack nuevo)

El stack (`template.yaml`) crea todo: Lambda, API Gateway, las 3 tablas DynamoDB, el bucket S3, y el Bedrock Guardrail. No hay que crear nada a mano de antemano.

```bash
.venv/bin/sam build --use-container

.venv/bin/sam deploy --guided
```

`--guided` te va a preguntar `TelegramBotToken` y `TelegramWebhookSecret` (son `NoEcho`, no quedan expuestos) y va a ofrecer guardar la configuración en `samconfig.toml`. **No aceptes que guarde los valores reales de esos dos parámetros ahí** — `samconfig.toml` está trackeado en git (ver "Nota de seguridad" abajo). Si el wizard los escribe, borralos de `samconfig.toml` después a mano, dejando el resto de la config (`stack_name`, `region`, etc.).

Al terminar, el deploy imprime los Outputs del stack — vas a necesitar `WebhookUrl`.

### Poblar datos y registrar el webhook

```bash
# puebla demo_productos y demo_leads desde data/catalogo.json y data/precios.json,
# y sube faq.md + catalogo.json a S3 (ver CLAUDE.md, sección "Sincronización de datos")
python scripts/seed_dynamodb.py

# registra la URL del webhook (Output WebhookUrl del deploy) en BotFather
python scripts/set_webhook.py --url https://xxxx.execute-api.us-east-1.amazonaws.com/prod/webhook/telegram
```

Con esto el bot ya responde en Telegram.

---

## Despliegues posteriores (código ya cambiado, stack ya existe)

Esta es la forma real que usamos para el ciclo normal de iteración — dos reglas fijas:

### 1. Build siempre con `--use-container`

```bash
.venv/bin/sam build --use-container
```

**Nunca lo corras sin ese flag.** El `Makefile` (ver `Metadata.BuildMethod: makefile` en `template.yaml`) instala `requirements.txt` con `pip3 install` directo — si corre en tu máquina local en vez de en el contenedor Lambda, dependencias con extensiones compiladas (ej. `pydantic_core`, que viene de Rust/maturin) generan wheels con el ABI de tu sistema (Fedora + Python 3.13, en este entorno) en vez del runtime real de Lambda (Amazon Linux + Python 3.12) — la Lambda revienta al arrancar. Confirmado: las wheels de un build con `--use-container` traen tag `manylinux_2_17_x86_64` / `cp312`, compatibles; sin el flag, no.

### 2. Deploy sin tocar los secretos de Telegram

`samconfig.toml` está trackeado en git y sus `parameter_overrides` para `TelegramBotToken`/`TelegramWebhookSecret` son placeholders literales (`"PENDIENTE-configurar-..."`) **a propósito** — nunca se completan ahí con los valores reales, porque eso los comitearía en texto plano al historial de git para siempre.

El stack ya desplegado sí tiene los valores reales guardados (son `NoEcho`, no se pueden leer de vuelta por diseño de CloudFormation). Para no pisarlos con los placeholders del `samconfig.toml`, el deploy tiene que decirle explícitamente a CloudFormation que reuse lo que ya está:

```bash
.venv/bin/sam deploy \
  --parameter-overrides \
    'ParameterKey=TelegramBotToken,UsePreviousValue=true' \
    'ParameterKey=TelegramWebhookSecret,UsePreviousValue=true' \
  --no-confirm-changeset
```

`UsePreviousValue=true` le dice a CloudFormation "no cambies este parámetro, dejalo como está en el stack ahora mismo". Sin esto (o si solo confiás en lo que trae `samconfig.toml`), el deploy tomaría el placeholder literal y rompería `@koriagente_bot` en producción.

**Si necesitás verificar sin arriesgar nada** antes de un deploy real (ej. justo antes del evento), corré primero un dry-run:

```bash
.venv/bin/sam deploy \
  --parameter-overrides \
    'ParameterKey=TelegramBotToken,UsePreviousValue=true' \
    'ParameterKey=TelegramWebhookSecret,UsePreviousValue=true' \
  --no-execute-changeset --no-confirm-changeset

# después inspeccioná el changeset sin ejecutarlo:
aws cloudformation describe-change-set \
  --stack-name kori-agent --region us-east-1 \
  --change-set-name <arn que imprimió el comando anterior> \
  --query '{Parameters:Parameters,Changes:Changes[*].ResourceChange.{Logical:LogicalResourceId,Action:Action}}'
```

Si `Changes` solo muestra `KoriWebhookFunction` (`Code`) y opcionalmente `KoriHttpApi` (`Body`), sin nada tocando `Environment`, es seguro ejecutar el deploy real.

### 3. (Opcional) Si cambiaste algo en `data/catalogo.json`

```bash
python scripts/seed_dynamodb.py
```

Es idempotente — sobrescribe por `producto_id`, no duplica. Ver `CLAUDE.md`, sección "Sincronización de datos", para la regla de por qué `data/catalogo.json` es la única fuente de verdad.

---

## Dashboard Streamlit (EC2)

El dashboard **no levanta una instancia nueva** — corre en la misma EC2 que ya tenés viva para otra cosa (`n8n-app`, `i-052bd40fd9843cc27`, Amazon Linux 2023, IP pública `3.95.210.95`). Puerto usado: **8501** (el 80/443 ya los ocupa lo que sea que sirva n8n ahí, no se tocan).

Ya resuelto de antemano (no hace falta repetirlo):
- Security group `launch-wizard-7` → regla nueva `8501/tcp` desde `0.0.0.0/0` agregada, sin tocar las reglas existentes de 22/80/443.
- IAM: rol `kori-dashboard-ec2-role` + instance profile `kori-dashboard-ec2-profile`, con permiso de **solo lectura** (`Scan`/`GetItem`/`Query`) únicamente sobre `demo_leads` y `demo_trazas` — ya asociado a la instancia. No requiere `aws configure` ni credenciales estáticas en el disco: boto3 las toma automáticamente vía metadata del instance profile.

### Deploy (por SSH, en la instancia)

```bash
ssh -i /ruta/a/n8n-key.pem ec2-user@3.95.210.95

# clonar (o git pull si ya existe) el repo
git clone https://github.com/juliosarango/kori_agent.git
cd kori_agent

python3 -m venv .venv-dashboard
.venv-dashboard/bin/pip install -r dashboard/requirements.txt

export AWS_REGION=us-east-1
export DYNAMO_TABLE_LEADS=demo_leads
export DYNAMO_TABLE_TRAZAS=demo_trazas

# server.address 0.0.0.0 para que escuche en la IP pública, no solo localhost
.venv-dashboard/bin/streamlit run dashboard/app.py \
  --server.port 8501 --server.address 0.0.0.0
```

Con esto ya se ve en `http://3.95.210.95:8501`. Para que sobreviva el cierre de la sesión SSH y se reinicie solo si crashea a mitad del ensayo/evento, conviene un servicio systemd en vez de dejarlo corriendo en la terminal:

```bash
sudo tee /etc/systemd/system/kori-dashboard.service > /dev/null <<'EOF'
[Unit]
Description=Kori Agent - Dashboard Streamlit
After=network.target

[Service]
Type=simple
User=ec2-user
WorkingDirectory=/home/ec2-user/kori_agent
Environment=AWS_REGION=us-east-1
Environment=DYNAMO_TABLE_LEADS=demo_leads
Environment=DYNAMO_TABLE_TRAZAS=demo_trazas
ExecStart=/home/ec2-user/kori_agent/.venv-dashboard/bin/streamlit run dashboard/app.py --server.port 8501 --server.address 0.0.0.0
Restart=on-failure
RestartSec=3

[Install]
WantedBy=multi-user.target
EOF

sudo systemctl daemon-reload
sudo systemctl enable --now kori-dashboard
sudo systemctl status kori-dashboard   # confirmar "active (running)"
```

Para actualizar después de un cambio en `dashboard/app.py`:

```bash
cd ~/kori_agent && git pull
sudo systemctl restart kori-dashboard
```

---

## Verificar que el deploy quedó bien

```bash
aws cloudformation describe-stacks --stack-name kori-agent --region us-east-1 \
  --query 'Stacks[0].StackStatus'
# esperado: "UPDATE_COMPLETE"

aws logs tail /aws/lambda/kori-agent-webhook --region us-east-1 --since 5m --follow
```

Y probar mandándole un mensaje real a `@koriagente_bot` por Telegram.

---

## Nota de seguridad: `samconfig.toml`

Este archivo está trackeado en git. Sus `parameter_overrides` para `TelegramBotToken` y `TelegramWebhookSecret` deben quedarse siempre como los placeholders `"PENDIENTE-configurar-..."` — **nunca los reemplaces por los valores reales ahí**. Los valores reales solo existen en:
1. El stack de CloudFormation ya desplegado (como parámetro `NoEcho`).
2. Pasados de forma efímera por línea de comando (`--parameter-overrides`) cuando hace falta setearlos por primera vez o cambiarlos — nunca escritos a un archivo del repo.

Si en algún momento necesitás rotar alguno de los dos (ej. el bot token se filtró), generá el valor nuevo y pasalo explícito una sola vez con `--parameter-overrides 'ParameterKey=TelegramBotToken,ParameterValue=<nuevo-valor>' 'ParameterKey=TelegramWebhookSecret,UsePreviousValue=true'` (o viceversa) — no lo dejes en shell history compartido ni en `samconfig.toml`.
