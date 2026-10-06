"""
Homebrew instalado en la unidad: las carpetas de apps/ que el Homebrew
Channel muestra como aplicaciones (las que tienen boot.dol o boot.elf), vengan
de la Open Shop Channel, de «Instalar o actualizar cargadores» o de una copia
a mano. Los datos de cada una salen de su meta.xml.

list_apps() y remove_apps() son bloqueantes: llamar vía backend.run_async.
"""
import html
import os
import re
import shlex
import shutil
from pathlib import Path

from backend import core

_BINARIES = ('boot.dol', 'boot.elf')
# Campos del meta.xml → clave del dict de la app
_META_FIELDS = {'name': 'name', 'coder': 'author', 'version': 'version', 'release_date': 'date',
                'short_description': 'short', 'long_description': 'long'}


def list_apps(base_path):
    """
    Apps instaladas en la unidad y espacio que le queda: ([app], bytes libres).
    Cada app es un dict { folder, path, name, author, version, date, short,
    long, size (bytes), icon (ruta de su icon.png, o '') }.
    """
    apps_dir = Path(base_path) / 'apps'
    apps = []
    try:
        folders = sorted((d for d in apps_dir.iterdir() if d.is_dir()), key=lambda d: d.name.lower())
    except OSError:
        folders = []  # la unidad no tiene carpeta apps/
    for folder in folders:
        if not any((folder / binary).is_file() for binary in _BINARIES):
            continue
        app = {'folder': folder.name, 'path': str(folder), 'size': _folder_size(folder),
               'icon': str(folder / 'icon.png') if (folder / 'icon.png').is_file() else ''}
        app.update(_read_meta(folder / 'meta.xml'))
        app['name'] = app['name'] or folder.name
        apps.append(app)
    return apps, shutil.disk_usage(base_path).free


def _read_meta(meta_path):
    """Los campos de _META_FIELDS del meta.xml ('' los que falten o si no se puede leer)."""
    try:
        text = meta_path.read_text(encoding='utf-8', errors='replace')
    except OSError:
        text = ''
    meta = {}
    for tag, key in _META_FIELDS.items():
        # Con expresión regular: muchos meta.xml no son XML bien formado
        match = re.search(rf'<{tag}>(.*?)</{tag}>', text, re.DOTALL)
        meta[key] = html.unescape(match.group(1)).strip() if match else ''
    return meta


def _folder_size(folder):
    """Bytes que ocupan los archivos de la carpeta, subcarpetas incluidas."""
    total = 0
    for root, _dirs, files in os.walk(folder):
        for name in files:
            try:
                total += os.path.getsize(os.path.join(root, name))
            except OSError:
                pass
    return total


def format_date(date):
    """Fecha del meta.xml ('20131123' o '20131123000000') → '23/11/2013'; con otro formato, tal cual."""
    if re.fullmatch(r'\d{8}(\d{6})?', date):
        return f'{date[6:8]}/{date[4:6]}/{date[:4]}'
    return date


def remove_apps(base_path, apps):
    """
    Borra de la unidad la carpeta apps/<carpeta> de cada app. Las carpetas de
    datos que la app tenga fuera de apps/ (partidas, ROM, ajustes) no se tocan.
    Devuelve [(app, error o None)].
    """
    apps_dir = (Path(base_path) / 'apps').resolve()
    results = []
    try:
        for n, app in enumerate(apps):
            core.set_step(int(n * 100 / len(apps)), f"Eliminando {app['name']} ({n + 1} de {len(apps)})")
            folder = (apps_dir / app['folder']).resolve()
            error = None
            if folder.parent != apps_dir:
                error = 'la carpeta no está dentro de apps/'  # nunca borrar fuera de apps/
            else:
                try:
                    shutil.rmtree(folder)
                except OSError as e:
                    error = str(e)
            results.append((app, error))
        core.set_step(99, 'Guardando los cambios en la unidad…')
        core.run(f'sync -f {shlex.quote(str(base_path))}', timeout=600)
    finally:
        core.set_step(None)
    return results
