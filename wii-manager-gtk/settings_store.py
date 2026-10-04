"""
Persistencia de Ajustes. Equivalente a loadSettings()/saveSettings() del
frontend web, que usaban localStorage; aquí usamos un JSON en
~/.config/wii-manager-gtk/settings.json (no hay navegador ni localStorage).
"""
import json
from pathlib import Path

_CONFIG_PATH = Path.home() / '.config' / 'wii-manager-gtk' / 'settings.json'

DEFAULTS = {
    'wit_path': '',
    'wwt_path': '',
    'wfs_path': '',            # unidad que aparece en el panel lateral al arrancar
    'cover_region': 'ES',      # región y tipo de carátula que se piden primero a GameTDB
    'cover_type': 'cover3D',
    'show_terminal': False,    # panel inferior con la salida de los comandos
    'banner_sound': True,      # hacer sonar el banner del juego de Wii seleccionado
    'gc_sound_path': '',       # archivo de audio que suena al seleccionar un juego de GameCube
}


def load():
    """Ajustes guardados sobre los valores por defecto; las claves desconocidas del archivo se ignoran."""
    try:
        data = json.loads(_CONFIG_PATH.read_text())
    except Exception:
        data = {}
    merged = dict(DEFAULTS)
    merged.update({k: v for k, v in data.items() if k in DEFAULTS})
    return merged


def save(settings):
    """Escribe los ajustes, creando la carpeta de configuración si no existe."""
    _CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    _CONFIG_PATH.write_text(json.dumps(settings, indent=2))
