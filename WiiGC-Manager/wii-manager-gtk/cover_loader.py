"""
Descarga de carátulas desde GameTDB con reintento automático por región y
tipo. Equivalente a tryLoadCover()/tryLoadGCCover() del frontend web, pero
bloqueante (se ejecuta en un hilo vía backend.run_async, no en el navegador).

Las carátulas se guardan en ~/.cache/wii-manager-gtk/covers/ para no volver
a pedirlas a GameTDB: una por juego y por preferencia (tipo/región) elegida
en Ajustes.
"""
import time
import urllib.error
import urllib.request
from pathlib import Path

_CACHE_DIR = Path.home() / '.cache' / 'wii-manager-gtk' / 'covers'
# "GameTDB no tiene carátula" también se recuerda (archivo vacío), pero solo
# una semana, por si la añaden más adelante.
_MISSING_MAX_AGE = 7 * 24 * 3600

COVER_REGIONS = ['ES', 'EN', 'FR', 'DE', 'IT', 'PT', 'AU', 'US', 'JA', 'KO']
COVER_TYPES = ['cover3D', 'cover', 'coverfull', 'disc']


def fetch_cover(game_id, is_gc, pref_region, pref_type, timeout=5, fallback_types=True):
    """
    Prueba combinaciones tipo×región (empezando por las preferidas) y,
    para juegos de GameCube, también las bases /gcn/ y /wii/ de GameTDB.
    Devuelve los bytes de la primera imagen encontrada, o None si GameTDB
    no tiene ninguna. Si no se encontró imagen y algún intento falló por
    algo distinto de un 404 (sin red, timeout…), lanza ese error: no es lo
    mismo "no existe carátula" que "no se pudo comprobar".
    Con fallback_types=False solo se prueba pref_type (en todas las regiones).
    """
    if not game_id:
        return None

    cache_file = _CACHE_DIR / pref_type / pref_region / f'{game_id}.png'
    try:
        data = cache_file.read_bytes()
        if data or time.time() - cache_file.stat().st_mtime < _MISSING_MAX_AGE:
            return data or None
    except OSError:
        pass

    regions = [pref_region] + [r for r in COVER_REGIONS if r != pref_region]
    types = [pref_type] + [t for t in COVER_TYPES if t != pref_type and fallback_types]
    bases = ['gcn', 'wii'] if is_gc else ['wii']
    network_error = None

    for type_ in types:
        for region in regions:
            for base in bases:
                url = f'https://art.gametdb.com/{base}/{type_}/{region}/{game_id}.png'
                try:
                    with urllib.request.urlopen(url, timeout=timeout) as resp:
                        data = resp.read()
                    _write_cache(cache_file, data)
                    return data
                except urllib.error.HTTPError as e:
                    if e.code != 404:
                        network_error = e
                except Exception as e:
                    network_error = e
    if network_error:
        raise network_error
    _write_cache(cache_file, b'')
    return None


def _write_cache(cache_file, data):
    try:
        cache_file.parent.mkdir(parents=True, exist_ok=True)
        cache_file.write_bytes(data)
    except OSError:
        pass  # sin caché se sigue funcionando, solo que descargando cada vez
