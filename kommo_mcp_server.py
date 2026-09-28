#!/usr/bin/env python3
"""Servidor MCP local (stdio) de solo lectura para consultar Kommo CRM.

Herramientas: listar_pipelines, buscar_leads, contar_leads, ver_lead, ver_contacto.
Credenciales desde el .env junto a este archivo (KOMMO_SUBDOMAIN y KOMMO_TOKEN).
Solo hace pedidos GET; el token nunca aparece en las respuestas.

Uso directo (para probar): .venv/bin/python kommo_mcp_server.py
"""
import os
import re
import time
from datetime import date, datetime, time as dtime, timedelta, timezone
from typing import Optional
from zoneinfo import ZoneInfo

from mcp.server.fastmcp import FastMCP

from kommo_client import KommoClient, load_credentials, normalize

TZ = ZoneInfo(os.getenv("KOMMO_TZ", "America/Argentina/Buenos_Aires"))
CACHE_TTL = 600           # segundos que se guardan pipelines y usuarios en memoria
MAX_RESULTS = 500         # tope de leads que devuelve buscar_leads
MAX_COUNT = 50_000        # tope de leads que recorre contar_leads
MAX_NOTES = 30            # notas más recientes que devuelve ver_lead
BATCH = 50                # IDs por pedido cuando se piden varios elementos juntos

mcp = FastMCP(
    "kommo",
    instructions=(
        "Consultas de solo lectura al CRM Kommo de la cuenta (leads, contactos, pipelines y etapas). "
        "Las fechas se interpretan en horario de Argentina. Los montos están en la moneda de la cuenta. "
        "Para preguntas de 'cuántos' o totales usá contar_leads; para listar usá buscar_leads; "
        "para el detalle de un lead o contacto usá ver_lead o ver_contacto."
    ),
)

_client = None
_cache = {}


def client():
    global _client
    if _client is None:
        _client = KommoClient(*load_credentials())
    return _client


def cached(key, loader):
    hit = _cache.get(key)
    if hit and time.monotonic() - hit[0] < CACHE_TTL:
        return hit[1]
    value = loader()
    _cache[key] = (time.monotonic(), value)
    return value


def pipelines():
    return cached("pipelines", lambda: client().get_all("leads/pipelines", "pipelines"))


def users():
    return cached("users", lambda: {u["id"]: u for u in client().get_all("users", "users")})


def user_name(user_id):
    user = users().get(user_id)
    return user.get("name") if user else (str(user_id) if user_id else None)


def status_names():
    return {(p["id"], s["id"]): (p.get("name"), s.get("name"))
            for p in pipelines() for s in p.get("_embedded", {}).get("statuses", [])}


def fmt_ts(ts):
    return datetime.fromtimestamp(ts, TZ).strftime("%Y-%m-%d %H:%M") if ts else None


def parse_day(value, end=False):
    try:
        day = date.fromisoformat(value.strip())
    except (ValueError, AttributeError):
        raise ValueError(f"Fecha inválida '{value}'. Usá el formato AAAA-MM-DD, por ejemplo 2026-09-01.")
    moment = datetime.combine(day, dtime.max if end else dtime.min, TZ)
    return int(moment.timestamp())


def match_by_name(items, query, name_key="name"):
    """Coincidencia exacta (sin mayúsculas/acentos) y, si no hay, parcial."""
    target = normalize(query)
    exact = [i for i in items if normalize(i.get(name_key)) == target]
    return exact or [i for i in items if target in normalize(i.get(name_key))]


def resolve_pipelines(pipeline):
    if pipeline is None:
        return pipelines()
    text = str(pipeline).strip()
    found = [p for p in pipelines() if str(p["id"]) == text] or match_by_name(pipelines(), text)
    if not found:
        raise ValueError(f"No existe el pipeline '{pipeline}'. Disponibles: "
                         + ", ".join(f"{p['name']} (id {p['id']})" for p in pipelines()))
    return found


def resolve_statuses(etapa, pipeline):
    """Devuelve [(pipeline_id, status_id)] para el filtro, o None si no hay que filtrar por etapa."""
    selected = resolve_pipelines(pipeline)
    if etapa is None:
        if pipeline is None:
            return None
        return [(p["id"], s["id"]) for p in selected for s in p.get("_embedded", {}).get("statuses", [])]
    text = str(etapa).strip()
    statuses = [dict(s, pipeline_id=p["id"]) for p in selected for s in p.get("_embedded", {}).get("statuses", [])]
    found = [s for s in statuses if str(s["id"]) == text] or match_by_name(statuses, text)
    if not found:
        raise ValueError(f"No existe la etapa '{etapa}'. Usá listar_pipelines para ver las etapas disponibles.")
    return [(s["pipeline_id"], s["id"]) for s in found]


def resolve_users(responsable):
    if responsable is None:
        return None
    text = str(responsable).strip()
    all_users = list(users().values())
    found = [u for u in all_users if str(u["id"]) == text] or match_by_name(all_users, text)
    if not found:
        raise ValueError(f"No existe el usuario '{responsable}'. Usuarios: "
                         + ", ".join(sorted({u['name'] for u in all_users})))
    return [u["id"] for u in found]


def lead_params(etapa, pipeline, creado_desde, creado_hasta, responsable, texto):
    params = {}
    applied = {}
    statuses = resolve_statuses(etapa, pipeline)
    if statuses:
        names = status_names()
        for i, (pid, sid) in enumerate(statuses):
            params[f"filter[statuses][{i}][pipeline_id]"] = pid
            params[f"filter[statuses][{i}][status_id]"] = sid
        applied["etapas"] = [f"{names[(pid, sid)][0]} / {names[(pid, sid)][1]}" for pid, sid in statuses]
    if creado_desde:
        params["filter[created_at][from]"] = parse_day(creado_desde)
        applied["creado_desde"] = creado_desde
    if creado_hasta:
        params["filter[created_at][to]"] = parse_day(creado_hasta, end=True)
        applied["creado_hasta"] = creado_hasta
    user_ids = resolve_users(responsable)
    if user_ids:
        for i, uid in enumerate(user_ids):
            params[f"filter[responsible_user_id][{i}]"] = uid
        applied["responsable"] = [f"{user_name(uid)} (id {uid})" for uid in user_ids]
    if texto:
        params["query"] = texto
        applied["texto"] = texto
    return params, applied


def lead_summary(lead):
    pipeline_name, status_name = status_names().get((lead.get("pipeline_id"), lead.get("status_id")), (None, None))
    contacts = lead.get("_embedded", {}).get("contacts") or []
    main = next((c["id"] for c in contacts if c.get("is_main")), contacts[0]["id"] if contacts else None)
    return {
        "id": lead["id"],
        "nombre": lead.get("name"),
        "precio": lead.get("price"),
        "pipeline": pipeline_name,
        "etapa": status_name,
        "responsable": user_name(lead.get("responsible_user_id")),
        "creado": fmt_ts(lead.get("created_at")),
        "actualizado": fmt_ts(lead.get("updated_at")),
        "cerrado": fmt_ts(lead.get("closed_at")),
        "contacto_principal_id": main,
        "etiquetas": [t.get("name") for t in lead.get("_embedded", {}).get("tags") or []],
    }


def custom_fields(entity):
    result = {}
    for field in entity.get("custom_fields_values") or []:
        values = [v.get("value") for v in field.get("values") or [] if v.get("value") not in (None, "")]
        if values:
            result[field.get("field_name") or field.get("field_code")] = values[0] if len(values) == 1 else values
    return result


def field_values(entity, code):
    return [str(v.get("value")) for f in entity.get("custom_fields_values") or []
            if f.get("field_code") == code for v in f.get("values") or [] if v.get("value")]


def contact_summary(contact):
    return {
        "id": contact["id"],
        "nombre": contact.get("name"),
        "telefonos": field_values(contact, "PHONE"),
        "emails": field_values(contact, "EMAIL"),
        "responsable": user_name(contact.get("responsible_user_id")),
        "creado": fmt_ts(contact.get("created_at")),
    }


def fetch_by_ids(path, key, ids, params=None):
    items = []
    ids = list(ids)
    for i in range(0, len(ids), BATCH):
        chunk = [("filter[id][]", x) for x in ids[i:i + BATCH]] + list((params or {}).items())
        items.extend(client().get_all(path, key, chunk))
    return items


@mcp.tool()
def listar_pipelines() -> dict:
    """Lista los pipelines (embudos de venta) de Kommo con sus etapas e IDs.

    Usala para saber qué etapas existen, cómo se llaman exactamente o qué ID tienen,
    antes de filtrar leads por etapa o pipeline.
    """
    return {"pipelines": [
        {
            "id": p["id"],
            "nombre": p.get("name"),
            "principal": p.get("is_main"),
            "etapas": [{"id": s["id"], "nombre": s.get("name"), "orden": s.get("sort")}
                       for s in p.get("_embedded", {}).get("statuses", [])],
        }
        for p in pipelines()
    ]}


@mcp.tool()
def buscar_leads(
    etapa: Optional[str] = None,
    pipeline: Optional[str] = None,
    creado_desde: Optional[str] = None,
    creado_hasta: Optional[str] = None,
    responsable: Optional[str] = None,
    texto: Optional[str] = None,
    limite: int = 50,
) -> dict:
    """Busca leads (negocios) en Kommo y devuelve una lista resumida, del más nuevo al más viejo.

    Usala para listar o ver ejemplos de leads que cumplen condiciones. Para saber CUÁNTOS
    hay o sumar montos, usá contar_leads (esta herramienta corta en `limite`).
    Todos los filtros son opcionales y se combinan:
    - etapa: nombre (p. ej. "EN NEGOCIACION", "Leads perdidos"; no distingue mayúsculas ni
      acentos y acepta parte del nombre) o ID de la etapa.
    - pipeline: nombre o ID del pipeline (p. ej. "Ventas").
    - creado_desde / creado_hasta: fechas AAAA-MM-DD, inclusivas, en horario de Argentina.
    - responsable: nombre o ID del usuario responsable.
    - texto: búsqueda libre (nombre del lead, datos del contacto, teléfono, etc.).
    - limite: máximo de leads a devolver (1 a 500, por defecto 50).
    Para ver el detalle completo de un lead usá ver_lead con su id.
    """
    limite = max(1, min(int(limite), MAX_RESULTS))
    params, applied = lead_params(etapa, pipeline, creado_desde, creado_hasta, responsable, texto)
    params["with"] = "contacts"
    params["order[created_at]"] = "desc"
    leads = client().get_all("leads", "leads", params, max_items=limite + 1)
    return {
        "filtros_aplicados": applied,
        "cantidad_devuelta": min(len(leads), limite),
        "hay_mas_resultados": len(leads) > limite,
        "leads": [lead_summary(l) for l in leads[:limite]],
    }


@mcp.tool()
def contar_leads(
    etapa: Optional[str] = None,
    pipeline: Optional[str] = None,
    creado_desde: Optional[str] = None,
    creado_hasta: Optional[str] = None,
    responsable: Optional[str] = None,
    texto: Optional[str] = None,
) -> dict:
    """Cuenta los leads que cumplen los filtros y suma sus montos, con desglose por etapa y responsable.

    Usala para preguntas de cantidad o totales ("¿cuántos leads perdimos en septiembre?",
    "¿cuánto suma lo que tiene Daniela en negociación?"). Acepta los mismos filtros que
    buscar_leads: etapa, pipeline, creado_desde, creado_hasta (AAAA-MM-DD, horario de
    Argentina), responsable y texto. Recorre todas las páginas, así que con muchos leads
    puede tardar algunos segundos.
    """
    params, applied = lead_params(etapa, pipeline, creado_desde, creado_hasta, responsable, texto)
    names = status_names()
    total, amount = 0, 0
    by_status, by_user = {}, {}
    for lead in client().iter_pages("leads", "leads", params, max_items=MAX_COUNT):
        total += 1
        price = lead.get("price") or 0
        amount += price
        pipeline_name, status_name = names.get((lead.get("pipeline_id"), lead.get("status_id")), ("?", "?"))
        for bucket, key in ((by_status, f"{pipeline_name} / {status_name}"),
                            (by_user, user_name(lead.get("responsible_user_id")) or "Sin responsable")):
            entry = bucket.setdefault(key, {"cantidad": 0, "monto": 0})
            entry["cantidad"] += 1
            entry["monto"] += price
    order = lambda d: dict(sorted(d.items(), key=lambda kv: -kv[1]["cantidad"]))
    return {
        "filtros_aplicados": applied,
        "cantidad": total,
        "monto_total": amount,
        "resultado_truncado": total >= MAX_COUNT,
        "por_etapa": order(by_status),
        "por_responsable": order(by_user),
    }


@mcp.tool()
def ver_lead(lead_id: int) -> dict:
    """Muestra el detalle completo de un lead por su ID: datos, etapa, responsable, campos
    personalizados (p. ej. "Autopartes"), etiquetas, motivo de pérdida, contactos asociados
    con teléfonos y emails, y sus notas más recientes (hasta 30).

    Usala cuando ya tenés el ID de un lead (por ejemplo, de buscar_leads) y necesitás todo su detalle.
    """
    lead = client().get(f"leads/{int(lead_id)}", {"with": "contacts,loss_reason,catalog_elements"})
    if not lead:
        raise ValueError(f"No existe el lead {lead_id}.")
    embedded = lead.get("_embedded", {})
    contact_refs = embedded.get("contacts") or []
    contacts = {c["id"]: c for c in fetch_by_ids("contacts", "contacts", [c["id"] for c in contact_refs])}
    notes = []
    try:
        raw_notes = client().get_all(f"leads/{int(lead_id)}/notes", "notes", {"order[id]": "desc"},
                                     max_items=MAX_NOTES)
    except RuntimeError:
        raw_notes = client().get_all(f"leads/{int(lead_id)}/notes", "notes")[-MAX_NOTES:][::-1]
    for note in raw_notes:
        params = note.get("params") or {}
        notes.append({
            "id": note["id"],
            "tipo": note.get("note_type"),
            "fecha": fmt_ts(note.get("created_at")),
            "autor": user_name(note.get("created_by")),
            "texto": params.get("text") or params.get("service"),
            "detalle": {k: v for k, v in params.items() if k not in ("text",)} or None,
        })
    summary = lead_summary(lead)
    summary.update({
        "campos": custom_fields(lead),
        "motivo_perdida": [r.get("name") for r in embedded.get("loss_reason") or []] or None,
        "productos_catalogo": embedded.get("catalog_elements") or None,
        "empresas_ids": [c["id"] for c in embedded.get("companies") or []] or None,
        "contactos": [
            dict(contact_summary(contacts[c["id"]]), principal=bool(c.get("is_main")),
                 campos=custom_fields(contacts[c["id"]]))
            for c in contact_refs if c["id"] in contacts
        ],
        "notas": notes,
    })
    return summary


@mcp.tool()
def ver_contacto(contacto_id: Optional[int] = None, busqueda: Optional[str] = None, limite: int = 10) -> dict:
    """Muestra un contacto de Kommo por ID, o busca contactos por nombre, teléfono o email.

    - Con contacto_id: devuelve el detalle completo (teléfonos, emails, campos, responsable)
      y la lista de sus leads con etapa y monto.
    - Con busqueda: devuelve hasta `limite` contactos que coinciden (por ejemplo un nombre,
      "Mariano" o un teléfono "2926450242"). Después usá contacto_id para ver el detalle.
    """
    if contacto_id is None and not busqueda:
        raise ValueError("Indicá contacto_id o busqueda.")
    if contacto_id is None:
        limite = max(1, min(int(limite), 50))
        found = client().get_all("contacts", "contacts", {"query": busqueda.strip()}, max_items=limite + 1)
        return {
            "busqueda": busqueda,
            "cantidad_devuelta": min(len(found), limite),
            "hay_mas_resultados": len(found) > limite,
            "contactos": [contact_summary(c) for c in found[:limite]],
        }
    contact = client().get(f"contacts/{int(contacto_id)}", {"with": "leads"})
    if not contact:
        raise ValueError(f"No existe el contacto {contacto_id}.")
    lead_ids = [l["id"] for l in contact.get("_embedded", {}).get("leads") or []]
    leads = fetch_by_ids("leads", "leads", lead_ids)
    detail = contact_summary(contact)
    detail.update({
        "campos": custom_fields(contact),
        "etiquetas": [t.get("name") for t in contact.get("_embedded", {}).get("tags") or []],
        "leads": [
            {k: lead_summary(l)[k] for k in ("id", "nombre", "precio", "pipeline", "etapa", "responsable", "creado")}
            for l in sorted(leads, key=lambda l: -(l.get("created_at") or 0))
        ],
    })
    return detail


if __name__ == "__main__":
    mcp.run()
