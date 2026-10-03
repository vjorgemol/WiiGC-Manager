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
    'wfs_path': '',
    'cover_region': 'ES',
    'cover_type': 'cover3D',
    'show_terminal': False,
}


def load():
    try:
        data = json.loads(_CONFIG_PATH.read_text())
    except Exception:
        data = {}
    merged = dict(DEFAULTS)
    merged.update({k: v for k, v in data.items() if k in DEFAULTS})
    return merged


def save(settings):
    _CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    _CONFIG_PATH.write_text(json.dumps(settings, indent=2))
