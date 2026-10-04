"""
Metadatos de juegos (compañía y fecha de lanzamiento) desde la base de datos
de GameTDB (wiitdb.zip, incluye Wii, WiiWare y GameCube). wwt/wit no dan esos
datos, así que se descarga el XML una vez, se reduce a lo que usa la tabla de
Videoteca y se guarda en ~/.cache/wii-manager-gtk/gametdb.json.

load() es bloqueante (descarga + parseo): llamar vía backend.run_async.
"""
import io
import json
import re
import time
import urllib.request
import zipfile
import xml.etree.ElementTree as ET
from pathlib import Path

_URL = 'https://www.gametdb.com/wiitdb.zip?LANG=ES&FALLBACK=TRUE&GAMECUBE=TRUE&WIIWARE=TRUE'
_CACHE_PATH = Path.home() / '.cache' / 'wii-manager-gtk' / 'gametdb.json'
_MAX_AGE = 30 * 24 * 3600  # la base de datos cambia poco: refrescar una vez al mes

_CACHE_VERSION = 2          # 2: añade las sumas SHA1 de los volcados (roms)

# En memoria: { companies: { código: nombre }, games: { ID: (compañía, fecha ISO, volcados) } }
_db = {'companies': {}, 'games': {}}


def load(timeout=30):
    """Carga la base de datos (de caché si es reciente, si no la descarga)."""
    global _db
    cached = _read_cache()
    if cached and time.time() - _CACHE_PATH.stat().st_mtime < _MAX_AGE:
        _db = cached
        return True
    try:
        _db = _download(timeout)
    except Exception:
        # Sin red: mejor una caché antigua que nada
        if cached:
            _db = cached
            return True
        raise
    _CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
    _CACHE_PATH.write_text(json.dumps(_db))
    return True


def lookup(game_id):
    """Devuelve (compañía, fecha ISO 'AAAA-MM-DD') — cadenas vacías si no se conoce."""
    game_id = (game_id or '').upper()
    publisher, date = _db['games'].get(game_id, ('', '', []))[:2]
    if not publisher and len(game_id) >= 6:
        # Los dos últimos caracteres del ID son el código de compañía
        publisher = _db['companies'].get(game_id[4:6], '')
    return publisher, date


def roms(game_id):
    """Volcados conocidos del juego: lista de (versión, tamaño en bytes, sha1)."""
    return [tuple(r) for r in _db['games'].get((game_id or '').upper(), ('', '', []))[2]]


def disc_count(game_id):
    """Número de discos del juego según GameTDB (1 si no consta que sea multidisco)."""
    numbers = [int(m.group(1)) for version, _size, _sha1 in roms(game_id)
               if (m := re.search(r'dis[ck]\s*(\d+)', version, re.IGNORECASE))]
    return max(numbers, default=1)


def format_date(iso):
    """'2008-04-11' → '11/04/2008'; fechas parciales → '04/2008' o '2008'."""
    if not iso:
        return ''
    year, month, day = iso.split('-')
    if month == '00':
        return year
    if day == '00':
        return f'{month}/{year}'
    return f'{day}/{month}/{year}'


def _read_cache():
    """La caché en disco, o None si no existe, está dañada o es de otra versión del formato."""
    try:
        data = json.loads(_CACHE_PATH.read_text())
        return data if data.get('games') and data.get('version') == _CACHE_VERSION else None
    except Exception:
        return None


def _download(timeout):
    """Descarga wiitdb.zip y reduce su XML a las compañías y, por juego, compañía, fecha y volcados conocidos."""
    with urllib.request.urlopen(_URL, timeout=timeout) as resp:
        archive = zipfile.ZipFile(io.BytesIO(resp.read()))
    companies, games = {}, {}
    with archive.open(archive.namelist()[0]) as xml:
        for _event, elem in ET.iterparse(xml):
            if elem.tag == 'company':
                companies[elem.get('code', '')] = elem.get('name', '')
            elif elem.tag == 'game':
                game_id = (elem.findtext('id') or '').strip().upper()
                if game_id:
                    known = [(r.get('version', ''), int(r.get('size') or 0), r.get('sha1', '').lower())
                             for r in elem.findall('rom') if r.get('sha1')]
                    games[game_id] = ((elem.findtext('publisher') or '').strip(), _iso_date(elem.find('date')), known)
                elem.clear()  # el XML es grande: se suelta cada nodo ya leído
    return {'version': _CACHE_VERSION, 'companies': companies, 'games': games}


def _iso_date(elem):
    """<date year=… month=… day=…> → 'AAAA-MM-DD'; el mes o el día desconocidos quedan a 00 (ver format_date)."""
    if elem is None or not (elem.get('year') or '').isdigit():
        return ''
    month, day = elem.get('month') or '', elem.get('day') or ''
    return f"{int(elem.get('year')):04d}-{int(month) if month.isdigit() else 0:02d}-{int(day) if day.isdigit() else 0:02d}"
