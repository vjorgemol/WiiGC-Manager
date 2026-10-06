"""
Catálogo de homebrew de la Open Shop Channel (https://oscwii.org/library) y
su instalación en la unidad. La web no tiene nada que raspar: pinta la lista
con la misma API pública que se usa aquí (/api/v4/contents).

Cada app se publica como un .zip con la estructura de la raíz de la unidad
(apps/<slug>/boot.dol, meta.xml, icon.png y, a veces, carpetas de datos
fuera de apps/): instalarla es descomprimirlo allí.

El catálogo y los iconos se guardan en ~/.cache/wii-manager-gtk/oscwii/.

load(), fetch_icon(), scan() e install() son bloqueantes: llamar vía
backend.run_async.
"""
import hashlib
import io
import json
import re
import shlex
import shutil
import time
import urllib.request
import zipfile
from pathlib import Path

from backend import core

_API_URL = 'https://hbb1.oscwii.org/api/v4/contents'
# Sin User-Agent propio, el servidor rechaza el de urllib
_HEADERS = {'User-Agent': 'WiiGC-Manager/1.0'}
_CACHE_DIR = Path.home() / '.cache' / 'wii-manager-gtk' / 'oscwii'
_CATALOG_PATH = _CACHE_DIR / 'catalog.json'
_MAX_AGE = 6 * 3600  # el catálogo recibe apps y versiones nuevas a menudo

# Categorías por las que filtra la web: (valor del campo category, texto)
CATEGORIES = [
    ('utilities', 'Utilidades'),
    ('emulators', 'Emuladores'),
    ('games', 'Juegos'),
    ('media', 'Multimedia'),
    ('demos', 'Demos'),
]

PERIPHERALS = {
    'wii_remote': 'Wiimote',
    'nunchuk': 'Nunchuk',
    'classic_controller': 'Mando clásico',
    'gamecube_controller': 'Mando de GameCube',
    'wii_zapper': 'Wii Zapper',
    'usb_keyboard': 'Teclado USB',
    'sdhc': 'Tarjeta SDHC',
}

PLATFORMS = {'wii': 'Wii', 'vwii': 'Wii U (vWii)', 'wii_mini': 'Wii Mini'}


class _Cancelled(Exception):
    """Se ha cancelado la instalación en mitad de una descarga."""


def load(force=False, timeout=30):
    """
    Lista de apps del catálogo (los dict de la API), ordenada por nombre. Sale
    de la caché si es reciente; con force=True se descarga siempre.
    """
    cached = _read_cache()
    if cached and not force and time.time() - _CATALOG_PATH.stat().st_mtime < _MAX_AGE:
        return cached
    try:
        req = urllib.request.Request(_API_URL, headers=_HEADERS)
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read()
        apps = _sorted(json.loads(raw))
    except Exception:
        # Sin red: mejor un catálogo antiguo que nada
        if cached:
            return cached
        raise
    try:
        _CACHE_DIR.mkdir(parents=True, exist_ok=True)
        _CATALOG_PATH.write_bytes(raw)
    except OSError:
        pass  # sin caché se sigue funcionando, solo que descargando cada vez
    return apps


def _read_cache():
    """El catálogo en disco, o None si no existe o está dañado."""
    try:
        return _sorted(json.loads(_CATALOG_PATH.read_text())) or None
    except Exception:
        return None


def _sorted(apps):
    """Las apps con los campos imprescindibles, por nombre."""
    usable = [a for a in apps if a.get('slug') and a.get('assets', {}).get('archive', {}).get('url')]
    return sorted(usable, key=lambda a: (a.get('name') or a['slug']).lower())


def fetch_icon(app, timeout=10):
    """Bytes del icono de la app (PNG de 128 × 48), de la caché o descargado; None si no tiene."""
    url = app.get('assets', {}).get('icon', {}).get('url')
    if not url:
        return None
    cache_file = _CACHE_DIR / 'icons' / f"{app['slug']}.png"
    try:
        return cache_file.read_bytes()
    except OSError:
        pass
    req = urllib.request.Request(url, headers=_HEADERS)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        data = resp.read()
    try:
        cache_file.parent.mkdir(parents=True, exist_ok=True)
        cache_file.write_bytes(data)
    except OSError:
        pass
    return data


def scan(base_path, apps):
    """
    Qué apps del catálogo están ya en la unidad y cuánto espacio le queda:
    ({slug: versión instalada}, bytes libres). La versión es la del meta.xml
    de la app ('' si no se puede leer), que es la misma que publica el catálogo.
    """
    apps_dir = Path(base_path) / 'apps'
    installed = {}
    for app in apps:
        app_dir = apps_dir / app['slug']
        if not any((app_dir / binary).is_file() for binary in ('boot.dol', 'boot.elf')):
            continue
        version = ''
        try:
            # Con expresión regular: muchos meta.xml no son XML bien formado
            meta = (app_dir / 'meta.xml').read_text(encoding='utf-8', errors='replace')
            match = re.search(r'<version>(.*?)</version>', meta, re.DOTALL)
            version = match.group(1).strip() if match else ''
        except OSError:
            pass
        installed[app['slug']] = version
    return installed, shutil.disk_usage(base_path).free


def install(apps, base_path, cancel):
    """
    Descarga las apps una a una y las descomprime en la raíz de la unidad.
    cancel es un threading.Event: al activarlo se descarta la descarga en
    curso y no se empiezan más. El avance se publica con core.set_step().

    Devuelve (instaladas, [(app, error)], cancelada).
    """
    base = Path(base_path).resolve()
    total = sum(a['assets']['archive'].get('size') or 0 for a in apps) or 1
    received = 0
    done, failed = [], []
    try:
        for n, app in enumerate(apps):
            if cancel.is_set():
                break
            text = f"Descargando {app.get('name', app['slug'])} ({n + 1} de {len(apps)})"
            before = received

            def on_bytes(count):
                nonlocal received
                received += count
                core.set_step(min(99, int(received * 100 / total)), text)

            on_bytes(0)
            try:
                _install_one(app, base, cancel, on_bytes)
                done.append(app)
            except _Cancelled:
                break
            except Exception as e:
                failed.append((app, str(e) or type(e).__name__))
            # Si la descarga falló a medias, la barra cuenta la app como vista
            received = before + (app['assets']['archive'].get('size') or 0)
        if done:
            core.set_step(99, 'Guardando los cambios en la unidad…')
            core.run(f'sync -f {shlex.quote(str(base))}', timeout=600)
    finally:
        core.set_step(None)
    return done, failed, cancel.is_set()


def _install_one(app, base, cancel, on_bytes):
    """Descarga el .zip de la app, lo comprueba y lo descomprime en base. Lanza una excepción si algo falla."""
    archive_info = app['assets']['archive']
    if shutil.disk_usage(base).free < (app.get('uncompressed_size') or 0):
        raise OSError('no queda espacio suficiente en la unidad')

    archive = _download_archive(app, archive_info['url'], cancel, on_bytes)
    if archive is None and archive_info.get('hash'):
        # La CDN puede seguir sirviendo el paquete de una versión anterior:
        # con otra URL se le obliga a pedir el actual al servidor
        archive = _download_archive(app, f"{archive_info['url']}?h={archive_info['hash']}", cancel, lambda _n: None)
    if archive is None:
        raise ValueError('la descarga no coincide con el catálogo: recarga el catálogo y vuelve a intentarlo')

    for member in archive.namelist():
        # No permitir que una ruta del zip escriba fuera de la unidad
        if not (base / member).resolve().is_relative_to(base):
            raise ValueError(f'ruta no válida en el paquete: {member}')
    archive.extractall(base)


def _download_archive(app, url, cancel, on_bytes):
    """
    Descarga el .zip de la app y devuelve el ZipFile, o None si no es el
    paquete que anuncia el catálogo.

    La suma MD5 del .zip no basta para saberlo: el servidor reempaqueta las
    apps cada pocas horas (mismo contenido, fechas nuevas y por tanto otra
    suma) y su CDN sigue sirviendo un rato el .zip anterior. Si la suma no
    coincide, el paquete se da por bueno cuando está íntegro y su ejecutable
    (boot.dol o boot.elf) es el del catálogo.
    """
    req = urllib.request.Request(url, headers=_HEADERS)
    buffer = io.BytesIO()
    with urllib.request.urlopen(req, timeout=60) as resp:
        while True:
            if cancel.is_set():
                raise _Cancelled()
            chunk = resp.read(256 * 1024)
            if not chunk:
                break
            buffer.write(chunk)
            on_bytes(len(chunk))

    try:
        archive = zipfile.ZipFile(buffer)
    except zipfile.BadZipFile:
        return None
    expected = (app['assets']['archive'].get('hash') or '').lower()
    if not expected or hashlib.md5(buffer.getvalue()).hexdigest() == expected:
        return archive
    binary = app['assets'].get('binary') or {}
    name = f"apps/{app['slug']}/{(binary.get('url') or '').rsplit('/', 1)[-1]}"
    try:
        same_binary = hashlib.md5(archive.read(name)).hexdigest() == (binary.get('hash') or '').lower()
        return archive if same_binary and archive.testzip() is None else None
    except (KeyError, zipfile.BadZipFile):
        return None
