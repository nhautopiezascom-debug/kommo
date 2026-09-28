# Kommo: consultas de solo lectura

Herramientas para consultar la cuenta de Kommo CRM con la API v4. Todo es de solo lectura (únicamente pedidos GET).

- `kommo_client.py`: cliente compartido (credenciales, límite de ~5 pedidos/s, reintentos ante 429 y paginación).
- `export_leads_negociacion.py`: exporta a CSV los leads de "EN NEGOCIACION" de los últimos 30 días.
- `kommo_mcp_server.py`: servidor MCP local para consultar Kommo en lenguaje natural desde Claude Code.

## Instalación

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
```

Crear un archivo `.env` en esta carpeta (no se sube a git):

```
KOMMO_SUBDOMAIN=tu_subdominio
KOMMO_TOKEN=tu_token_de_larga_duracion
```

## Servidor MCP

Herramientas: `listar_pipelines`, `buscar_leads`, `contar_leads`, `ver_lead`, `ver_contacto`.

Registrarlo para todas las sesiones de Claude Code (reemplazar la ruta por la de esta carpeta):

```bash
claude mcp add --scope user kommo -- /ruta/a/kommo/.venv/bin/python /ruta/a/kommo/kommo_mcp_server.py
```

El servidor lee el `.env` que está junto a `kommo_mcp_server.py`, así que no hace falta pasar el token con `-e`.
Las fechas se interpretan en horario de Argentina (se puede cambiar con la variable `KOMMO_TZ`).
