#!/usr/bin/env python3
"""Exporta a CSV los leads de la etapa "Negociación" creados en los últimos N días.

Solo lectura: únicamente hace pedidos GET a la API v4 de Kommo.

Credenciales (desde .env): KOMMO_SUBDOMAIN y KOMMO_TOKEN (también se aceptan en minúsculas).
El token nunca se imprime ni se escribe en archivos/logs.

Uso:
    python export_leads_negociacion.py [--stage "Negociación"] [--days 30]
                                       [--pipeline-id ID] [--output leads_negociacion.csv]
"""
import argparse
import csv
import os
import sys
import time
import unicodedata
from datetime import datetime, timedelta, timezone

import requests
from dotenv import dotenv_values, load_dotenv

PAGE_LIMIT = 250          # máximo permitido por la API v4 para "limit"
MAX_RPS = 7               # límite de Kommo: 7 pedidos por segundo por cuenta
MIN_INTERVAL = 1.0 / (MAX_RPS - 2)  # margen de seguridad: ~5 req/s
MAX_RETRIES = 5
CONTACT_BATCH = 50        # IDs de contacto por pedido (filter[id][])

CSV_COLUMNS = [
    "id_lead", "nombre_lead", "precio", "responsable", "fecha_creacion",
    "nombre_contacto", "telefono", "email",
]


def load_credentials():
    load_dotenv()
    values = {k.upper(): v for k, v in dotenv_values(".env").items() if v}
    subdomain = os.getenv("KOMMO_SUBDOMAIN") or values.get("KOMMO_SUBDOMAIN")
    token = os.getenv("KOMMO_TOKEN") or values.get("KOMMO_TOKEN")
    if not subdomain or not token:
        sys.exit("Faltan KOMMO_SUBDOMAIN y/o KOMMO_TOKEN en .env")
    # Acepta tanto "miempresa" como "miempresa.kommo.com" o una URL completa.
    subdomain = subdomain.strip().removeprefix("https://").removeprefix("http://")
    subdomain = subdomain.split(".kommo.com")[0].strip("/")
    return subdomain, token.strip()


class KommoClient:
    """Cliente mínimo de solo lectura con rate limiting y reintentos."""

    def __init__(self, subdomain, token):
        self.base_url = f"https://{subdomain}.kommo.com/api/v4"
        self.session = requests.Session()
        self.session.headers.update({
            "Authorization": f"Bearer {token}",
            "Accept": "application/json",
        })
        self._last_call = 0.0
        self.calls = 0

    def _throttle(self):
        wait = MIN_INTERVAL - (time.monotonic() - self._last_call)
        if wait > 0:
            time.sleep(wait)
        self._last_call = time.monotonic()

    def get(self, path_or_url, params=None):
        """GET con reintentos. Devuelve el JSON, o None si la API responde 204 (sin datos)."""
        url = path_or_url if path_or_url.startswith("http") else f"{self.base_url}/{path_or_url.lstrip('/')}"
        for attempt in range(1, MAX_RETRIES + 1):
            self._throttle()
            self.calls += 1
            try:
                resp = self.session.get(url, params=params, timeout=30)
            except requests.RequestException as exc:
                if attempt == MAX_RETRIES:
                    raise
                wait = 2 ** attempt
                print(f"  Error de red ({type(exc).__name__}); reintento en {wait}s...", file=sys.stderr)
                time.sleep(wait)
                continue

            if resp.status_code == 204:
                return None
            if resp.status_code == 429 or resp.status_code >= 500:
                if attempt == MAX_RETRIES:
                    break
                retry_after = resp.headers.get("Retry-After")
                wait = int(retry_after) if retry_after and retry_after.isdigit() else 2 ** attempt
                print(f"  HTTP {resp.status_code}; reintento {attempt}/{MAX_RETRIES - 1} en {wait}s...",
                      file=sys.stderr)
                time.sleep(wait)
                continue
            if resp.ok:
                return resp.json()
            break

        # Error definitivo: mostrar la respuesta de la API (nunca el token ni los headers del pedido).
        raise RuntimeError(f"GET {resp.request.path_url} -> HTTP {resp.status_code}: {resp.text[:1000]}")

    def get_all(self, path, embedded_key, params=None):
        """Recorre todas las páginas siguiendo _links.next y devuelve la lista completa."""
        params = dict(params or {})
        params.setdefault("limit", PAGE_LIMIT)
        params.setdefault("page", 1)
        items = []
        data = self.get(path, params)
        while data:
            items.extend(data.get("_embedded", {}).get(embedded_key, []))
            next_url = data.get("_links", {}).get("next", {}).get("href")
            if not next_url:
                break
            # _links.next ya incluye todos los parámetros (filtros, page, limit).
            data = self.get(next_url)
        return items


def normalize(text):
    text = unicodedata.normalize("NFKD", text or "")
    return "".join(c for c in text if not unicodedata.combining(c)).strip().casefold()


def find_stage(client, stage_name, pipeline_id=None):
    pipelines = client.get_all("leads/pipelines", "pipelines")
    target = normalize(stage_name)

    def collect(predicate):
        return [
            {
                "pipeline_id": pipeline["id"],
                "pipeline_name": pipeline.get("name"),
                "status_id": status["id"],
                "status_name": status.get("name"),
            }
            for pipeline in pipelines
            for status in pipeline.get("_embedded", {}).get("statuses", [])
            if predicate(normalize(status.get("name")))
        ]

    # Primero coincidencia exacta (sin mayúsculas/acentos); si no hay, la etapa que la contenga
    # (p. ej. "Negociación" -> "EN NEGOCIACION").
    matches = collect(lambda name: name == target)
    if not matches:
        matches = collect(lambda name: target in name)
        if matches:
            print(f"No hay una etapa llamada exactamente '{stage_name}'; se usan las que la contienen.")

    print(f"Pipelines encontrados: {len(pipelines)}")
    if not matches:
        print(f"No se encontró ninguna etapa llamada '{stage_name}'. Etapas disponibles:")
        for pipeline in pipelines:
            names = [s.get("name") for s in pipeline.get("_embedded", {}).get("statuses", [])]
            print(f"  - {pipeline.get('name')} (id {pipeline['id']}): {', '.join(names)}")
        sys.exit(1)

    if pipeline_id is not None:
        chosen = [m for m in matches if m["pipeline_id"] == pipeline_id]
        if not chosen:
            sys.exit(f"El pipeline {pipeline_id} no tiene una etapa '{stage_name}'.")
        return chosen[0]

    if len(matches) > 1:
        print(f"La etapa '{stage_name}' existe en {len(matches)} pipelines:")
        for m in matches:
            print(f"  - pipeline '{m['pipeline_name']}' (pipeline_id {m['pipeline_id']}), "
                  f"etapa '{m['status_name']}' (status_id {m['status_id']})")
        print("Volvé a correr el script con --pipeline-id <ID> para elegir uno.")
        sys.exit(2)

    return matches[0]


def fetch_leads(client, stage, days):
    now = datetime.now(timezone.utc)
    since = now - timedelta(days=days)
    params = {
        "with": "contacts",
        "filter[statuses][0][pipeline_id]": stage["pipeline_id"],
        "filter[statuses][0][status_id]": stage["status_id"],
        "filter[created_at][from]": int(since.timestamp()),
        "filter[created_at][to]": int(now.timestamp()),
    }
    leads = client.get_all("leads", "leads", params)
    # Verificación defensiva del lado del cliente por si algún filtro se ignora.
    since_ts = int(since.timestamp())
    return [
        lead for lead in leads
        if lead.get("status_id") == stage["status_id"]
        and lead.get("pipeline_id") == stage["pipeline_id"]
        and (lead.get("created_at") or 0) >= since_ts
    ]


def fetch_contacts(client, contact_ids):
    """Trae contactos en lotes usando filter[id][] (una llamada cada CONTACT_BATCH IDs)."""
    contacts = {}
    ids = sorted(contact_ids)
    for i in range(0, len(ids), CONTACT_BATCH):
        chunk = ids[i:i + CONTACT_BATCH]
        params = [("filter[id][]", cid) for cid in chunk] + [("limit", PAGE_LIMIT)]
        data = client.get("contacts", params)
        for contact in (data or {}).get("_embedded", {}).get("contacts", []):
            contacts[contact["id"]] = contact
    return contacts


def fetch_users(client):
    try:
        users = client.get_all("users", "users")
    except RuntimeError as exc:
        print(f"  Aviso: no se pudieron leer los usuarios ({exc}); se usará el ID del responsable.",
              file=sys.stderr)
        return {}
    return {u["id"]: u.get("name") for u in users}


def field_values(entity, field_code):
    values = []
    for field in entity.get("custom_fields_values") or []:
        if field.get("field_code") == field_code:
            values.extend(str(v.get("value")) for v in field.get("values") or [] if v.get("value"))
    return " | ".join(values)


def main_contact_id(lead):
    contacts = lead.get("_embedded", {}).get("contacts", []) or []
    for c in contacts:
        if c.get("is_main"):
            return c["id"]
    return contacts[0]["id"] if contacts else None


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--stage", default="Negociación", help="Nombre de la etapa (default: Negociación)")
    parser.add_argument("--days", type=int, default=30, help="Antigüedad máxima en días (default: 30)")
    parser.add_argument("--pipeline-id", type=int, help="Pipeline a usar si la etapa existe en varios")
    parser.add_argument("--output", default="leads_negociacion.csv", help="Archivo CSV de salida")
    args = parser.parse_args()

    client = KommoClient(*load_credentials())

    stage = find_stage(client, args.stage, args.pipeline_id)
    print(f"Etapa: '{stage['status_name']}' (status_id {stage['status_id']}) "
          f"en pipeline '{stage['pipeline_name']}' (pipeline_id {stage['pipeline_id']})")

    leads = fetch_leads(client, stage, args.days)
    print(f"Leads en la etapa creados en los últimos {args.days} días: {len(leads)}")

    contact_ids = {cid for cid in (main_contact_id(l) for l in leads) if cid}
    contacts = fetch_contacts(client, contact_ids) if contact_ids else {}
    users = fetch_users(client) if leads else {}

    with open(args.output, "w", newline="", encoding="utf-8-sig") as f:
        writer = csv.DictWriter(f, fieldnames=CSV_COLUMNS)
        writer.writeheader()
        for lead in sorted(leads, key=lambda l: l.get("created_at") or 0, reverse=True):
            contact = contacts.get(main_contact_id(lead), {})
            responsible = lead.get("responsible_user_id")
            writer.writerow({
                "id_lead": lead["id"],
                "nombre_lead": lead.get("name", ""),
                "precio": lead.get("price", ""),
                "responsable": users.get(responsible, responsible or ""),
                "fecha_creacion": datetime.fromtimestamp(lead["created_at"], timezone.utc)
                                  .strftime("%Y-%m-%d %H:%M:%S UTC") if lead.get("created_at") else "",
                "nombre_contacto": contact.get("name", ""),
                "telefono": field_values(contact, "PHONE"),
                "email": field_values(contact, "EMAIL"),
            })

    print(f"CSV guardado en {args.output} ({len(leads)} filas). Pedidos a la API: {client.calls}")


if __name__ == "__main__":
    main()
