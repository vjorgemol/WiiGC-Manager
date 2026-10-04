"""
Diálogo «Acerca de». Los datos (versión, licencia, enlaces, novedades) se leen
del metainfo.xml del paquete Flatpak, para no mantenerlos en dos sitios.
"""
import xml.etree.ElementTree as ET
from pathlib import Path

import gi
gi.require_version('Gtk', '4.0')
gi.require_version('Adw', '1')
from gi.repository import Gtk, Adw

APP_ID = 'org.wiigcmanager.Gtk'

_HERE = Path(__file__).resolve().parent
# Instalada como Flatpak (/app/share/…), o ejecutada desde el código fuente
_METAINFO_PATHS = [
    _HERE.parent.parent / 'metainfo' / f'{APP_ID}.metainfo.xml',
    _HERE.parent / 'flatpak' / f'{APP_ID}.metainfo.xml',
    _HERE.parent / 'WiiGC-Manager' / 'flatpak' / f'{APP_ID}.metainfo.xml',
]


def _metainfo():
    for path in _METAINFO_PATHS:
        try:
            return ET.parse(path).getroot()
        except (OSError, ET.ParseError):
            pass
    return ET.Element('component')


def version():
    """Versión de la app: la del lanzamiento más reciente del metainfo ('' si no se encuentra)."""
    release = _metainfo().find('releases/release')
    return release.get('version', '') if release is not None else ''


def present(parent):
    info = _metainfo()
    urls = {u.get('type'): (u.text or '').strip() for u in info.findall('url')}
    dialog = Adw.AboutDialog(
        application_name=info.findtext('name') or 'WiiGC Manager',
        application_icon=APP_ID,
        version=version() or 'desconocida',
        comments=info.findtext('summary') or '',
        developer_name=info.findtext('developer/name') or '',
        website=urls.get('homepage', ''),
        issue_url=urls.get('bugtracker', ''))
    if info.findtext('project_license') == 'MIT':
        dialog.set_license_type(Gtk.License.MIT_X11)
    release = info.find('releases/release')
    notes = release.find('description') if release is not None else None
    if notes is not None:
        # Novedades de la última versión (el diálogo admite <p>, <ul>, <ol> y <li>)
        dialog.set_release_notes(''.join(ET.tostring(child, encoding='unicode').strip() for child in notes))
        dialog.set_release_notes_version(release.get('version', ''))
    dialog.present(parent)
