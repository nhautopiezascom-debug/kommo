"""Agrega el servidor MCP de Kommo a la configuración de la app Claude de escritorio.

Lo ejecuta instalar_windows.bat. Hace una copia de seguridad del archivo de configuración
antes de modificarlo y conserva cualquier otro servidor que ya esté configurado.
"""
import json
import os
import shutil
import sys
from datetime import datetime
from pathlib import Path

HERE = Path(__file__).resolve().parent


def python_path():
    for candidate in (HERE / ".venv" / "Scripts" / "python.exe", HERE / ".venv" / "bin" / "python"):
        if candidate.is_file():
            return candidate
    sys.exit("No encontré el entorno .venv. Ejecutá primero instalar_windows.bat.")


def config_dirs():
    """Carpetas de configuración de Claude: instalación normal y versión de Microsoft Store."""
    dirs = []
    appdata = os.getenv("APPDATA")
    if appdata:
        dirs.append(Path(appdata) / "Claude")
    local = os.getenv("LOCALAPPDATA")
    if local:
        dirs.extend(sorted((Path(local) / "Packages").glob("Claude_*/LocalCache/Roaming/Claude")))
    if sys.platform == "darwin":
        dirs.append(Path.home() / "Library" / "Application Support" / "Claude")
    existing = [d for d in dirs if d.is_dir()]
    return existing or dirs[:1]


def main():
    if not any((HERE / name).is_file() for name in (".env", ".env.txt")):
        print("AVISO: falta el archivo .env con KOMMO_SUBDOMAIN y KOMMO_TOKEN en", HERE)
    entry = {"command": str(python_path()), "args": [str(HERE / "kommo_mcp_server.py")]}
    targets = config_dirs()
    if not targets:
        sys.exit("No encontré la carpeta de configuración de Claude. ¿Está instalada la app Claude de escritorio?")
    for folder in targets:
        folder.mkdir(parents=True, exist_ok=True)
        config_file = folder / "claude_desktop_config.json"
        config = {}
        if config_file.is_file() and config_file.read_text(encoding="utf-8").strip():
            try:
                config = json.loads(config_file.read_text(encoding="utf-8"))
            except json.JSONDecodeError:
                sys.exit(f"El archivo {config_file} tiene un formato inválido; no lo modifiqué.")
            backup = config_file.with_name(f"claude_desktop_config.backup-{datetime.now():%Y%m%d-%H%M%S}.json")
            shutil.copy2(config_file, backup)
            print("Copia de seguridad:", backup)
        config.setdefault("mcpServers", {})["kommo"] = entry
        config_file.write_text(json.dumps(config, indent=2, ensure_ascii=False), encoding="utf-8")
        print("Configuración actualizada:", config_file)


if __name__ == "__main__":
    main()
