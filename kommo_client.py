"""Cliente de solo lectura para la API v4 de Kommo.

Compartido por export_leads_negociacion.py y kommo_mcp_server.py.
Solo hace pedidos GET. El token nunca se imprime ni se incluye en errores.
"""
import os
import sys
import time
import unicodedata
from pathlib import Path

import requests
from dotenv import dotenv_values

PAGE_LIMIT = 250          # máximo permitido por la API v4 para "limit"
MAX_RPS = 7               # límite de Kommo: 7 pedidos por segundo por cuenta
MIN_INTERVAL = 1.0 / (MAX_RPS - 2)  # margen de seguridad: ~5 req/s
MAX_RETRIES = 5


def load_credentials(env_file=None):
    """Lee KOMMO_SUBDOMAIN y KOMMO_TOKEN (también en minúsculas).

    Orden: variables de entorno, luego el .env indicado en KOMMO_ENV_FILE,
    el .env (o .env.txt) junto a este archivo y, por último, el .env del directorio actual.
    """
    here = Path(__file__).resolve().parent
    # ".env.txt" cubre el caso de Windows, donde el Bloc de notas agrega ".txt" al guardar.
    candidates = [env_file, os.getenv("KOMMO_ENV_FILE"), here / ".env", here / ".env.txt", Path.cwd() / ".env"]
    values = {}
    for path in candidates:
        if path and Path(path).is_file():
            values = {k.upper(): v for k, v in dotenv_values(path).items() if v}
            break
    subdomain = os.getenv("KOMMO_SUBDOMAIN") or values.get("KOMMO_SUBDOMAIN")
    token = os.getenv("KOMMO_TOKEN") or values.get("KOMMO_TOKEN")
    if not subdomain or not token:
        raise RuntimeError("Faltan KOMMO_SUBDOMAIN y/o KOMMO_TOKEN en .env")
    # Acepta tanto "miempresa" como "miempresa.kommo.com" o una URL completa.
    subdomain = subdomain.strip().removeprefix("https://").removeprefix("http://")
    subdomain = subdomain.split(".kommo.com")[0].strip("/")
    return subdomain, token.strip()


def normalize(text):
    """Minúsculas y sin acentos, para comparar nombres."""
    text = unicodedata.normalize("NFKD", str(text or ""))
    return "".join(c for c in text if not unicodedata.combining(c)).strip().casefold()


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
                    raise RuntimeError(f"Error de red al consultar Kommo: {type(exc).__name__}") from None
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

    def iter_pages(self, path, embedded_key, params=None, max_items=None):
        """Recorre las páginas siguiendo _links.next y va devolviendo cada elemento."""
        if isinstance(params, list):  # lista de tuplas, para parámetros repetidos como filter[id][]
            params = list(params)
            if not any(key == "limit" for key, _ in params):
                params.append(("limit", PAGE_LIMIT))
        else:
            params = {"limit": PAGE_LIMIT, "page": 1, **(params or {})}
        count = 0
        data = self.get(path, params)
        while data:
            for item in data.get("_embedded", {}).get(embedded_key, []):
                yield item
                count += 1
                if max_items is not None and count >= max_items:
                    return
            next_url = data.get("_links", {}).get("next", {}).get("href")
            if not next_url:
                return
            # _links.next ya incluye todos los parámetros (filtros, page, limit).
            data = self.get(next_url)

    def get_all(self, path, embedded_key, params=None, max_items=None):
        """Devuelve la lista completa de todas las páginas."""
        return list(self.iter_pages(path, embedded_key, params, max_items))
