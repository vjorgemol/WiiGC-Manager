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
    """Raíz del metainfo.xml (el primero que exista); un <component> vacío si no hay ninguno."""
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
    """Muestra el diálogo «Acerca de» sobre la ventana parent."""
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
    # Novedades (el diálogo admite <p>, <ul>, <ol> y <li>): las de la última
    # versión y, debajo, las de las anteriores encabezadas por su número
    notes = []
    releases = info.findall('releases/release')
    for release in releases:
        description = release.find('description')
        if description is None:
            continue
        if release is not releases[0]:
            notes.append(f"<p>Versión {release.get('version', '')}</p>")
        notes.extend(ET.tostring(child, encoding='unicode').strip() for child in description)
    if notes:
        dialog.set_release_notes(''.join(notes))
        dialog.set_release_notes_version(releases[0].get('version', ''))
    dialog.present(parent)
