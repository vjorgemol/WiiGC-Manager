"""
Migración de los juegos de una unidad a otra (p. ej. a un disco de más
capacidad). Origen y destino pueden ser una unidad montada (FAT32/NTFS, con
los juegos en /wbfs/ y /games/) o una partición WBFS.

Los juegos de Wii se copian con wit/wwt, que leen y escriben ambos tipos de
unidad; los de GameCube son archivos sueltos y se copian tal cual.
"""
import os
import re
import shlex
import shutil
import signal
import subprocess
from pathlib import Path

from backend import core

CHUNK = 16 * 1024 * 1024


class Cancelled(Exception):
    """El usuario ha cancelado la migración."""


def volumes(devices):
    """
    Unidades entre las que se puede migrar, a partir de core.api_devices():
    [{ path, name }]. Una unidad montada se usa por su punto de montaje; una
    partición WBFS no se monta y se usa por su dispositivo.
    """
    found = []
    for dev in devices:
        if dev.get('is_system'):
            continue
        for vol in dev.get('partitions') or [dev]:
            path = vol.get('mountpoint') or (vol.get('path') if vol.get('fstype') == 'wbfs' else '')
            if not path or vol.get('is_system'):
                continue
            label = vol.get('label') or dev.get('model') or vol.get('name') or ''
            details = ' · '.join(p for p in (label, vol.get('size_formatted'), vol.get('fstype')) if p)
            found.append({'path': path, 'name': f'{details} — {path}'})
    return found


def _tools():
    return core.which('wit') or 'wit', core.which('wwt') or 'wwt'


def _wii_games(wit, part):
    """Juegos de Wii de una unidad. Lanza OSError si no se puede leer."""
    if core.is_mounted_dir(part):
        folder = core.wbfs_folder(part)
        return core.wit_list_images(wit, folder)[0] if folder.is_dir() else []
    games, _cmd, _stdout, stderr, rc = core.wit_list_images(wit, part)
    if rc != 0 and not games:
        reason = stderr.strip().splitlines()
        raise OSError(f'No se puede leer {part}' + (f': {reason[-1].strip()}' if reason else ''))
    # En una partición WBFS el juego se elige con <partición>/<ID6>
    return [{**g, 'path': f"{part}/{g['id']}"} for g in games]


def _free_bytes(wwt, part):
    """Espacio libre de la unidad, o None si no se puede saber."""
    if core.is_mounted_dir(part):
        try:
            return shutil.disk_usage(part).free
        except OSError:
            return None
    # Tabla de 'wwt SPACE' (en MiB):  size  used  used%  free  discs  file
    stdout = core.run(f'{wwt} SPACE -p {shlex.quote(part)}')[0]
    m = re.search(r'^\s*\d+\s+\d+\s+\d+%\s+(\d+)\s', stdout, re.MULTILINE)
    return int(m.group(1)) * 1024**2 if m else None


def _gc_target(game, games_dir):
    """Carpeta del destino para un juego de GameCube: la que ya tenga (del otro disco) o una con el mismo nombre."""
    target = games_dir / Path(game['folder']).name
    if not target.exists() and games_dir.is_dir():
        existing = [d for d in games_dir.iterdir() if d.is_dir() and f"[{game['id']}]" in d.name]
        if existing:
            target = existing[0]
    return target


def plan(src, dst, only=None):
    """
    Compara las dos unidades y decide qué hay que copiar (bloqueante).
    Devuelve:
        { games:       juegos por copiar [{ platform, id, title, bytes, … }],
          present:     juegos que ya están en el destino (se omiten),
          unsupported: juegos de GameCube que el destino no admite (partición WBFS),
          total_bytes: lo que ocupan los juegos por copiar,
          free_bytes:  espacio libre del destino (None si no se puede saber) }
    only limita la comparación a unos juegos del origen: un conjunto de
    ('wii', ID) y ('gc', carpeta). Sin él, entran todos.
    Lanza OSError si alguna de las unidades no se puede leer.
    """
    wit, wwt = _tools()
    for part in (src, dst):
        if not os.path.exists(part):
            raise OSError(f'No existe {part}')
    if os.path.realpath(src) == os.path.realpath(dst):
        raise OSError('El origen y el destino son la misma unidad')

    games, present, unsupported = [], [], []

    dst_ids = {g['id'] for g in _wii_games(wit, dst)}
    for g in _wii_games(wit, src):
        if only is not None and ('wii', g['id']) not in only:
            continue
        game = {'platform': 'wii', 'id': g['id'], 'title': g['title'],
                'bytes': g['size_bytes'], 'source': g['path']}
        (present if g['id'] in dst_ids else games).append(game)

    if core.is_mounted_dir(src):
        dst_games_dir = core.find_games_dir(dst) if core.is_mounted_dir(dst) else None
        for g in core.api_gc_list({'path': src}).get('games', []):
            if only is not None and ('gc', g['folder']) not in only:
                continue
            game = {'platform': 'gc', 'id': g['id'], 'title': g['title'], 'folder': g['folder']}
            if not dst_games_dir:
                unsupported.append(game)
                continue
            target = _gc_target(game, dst_games_dir)
            # Solo los archivos que falten en el destino (o que estén a medias)
            files = [(str(f), str(target / f.name), f.stat().st_size)
                     for f in sorted(Path(g['folder']).iterdir()) if f.is_file()]
            files = [(s, d, size) for s, d, size in files
                     if not os.path.isfile(d) or os.path.getsize(d) != size]
            game.update(target=str(target), files=files, bytes=sum(size for _s, _d, size in files))
            (games if files else present).append(game)

    return {'games': games, 'present': present, 'unsupported': unsupported,
            'total_bytes': sum(g['bytes'] for g in games), 'free_bytes': _free_bytes(wwt, dst)}


_running = None  # comando de copia en curso (subprocess.Popen)


def kill_running():
    """Mata ya el comando de copia en curso: al cerrar la app no debe seguir copiando por su cuenta."""
    proc = _running
    if proc and proc.poll() is None:
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except OSError:
            pass


def _run(cmd, cancel):
    """Como core.run(), pero interrumpible: si se activa cancel, mata el comando y lanza Cancelled."""
    global _running
    proc = _running = subprocess.Popen(
        ['flatpak-spawn', '--host', '--watch-bus', 'sh', '-c', cmd] if core.IN_FLATPAK else cmd,
        shell=not core.IN_FLATPAK, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        text=True, encoding='utf-8', errors='replace',
        start_new_session=True)  # grupo propio, para matar también a los hijos del shell
    while True:
        try:
            stdout, stderr = proc.communicate(timeout=0.5)
            return stdout, stderr, proc.returncode
        except subprocess.TimeoutExpired:
            if not cancel.is_set():
                continue
        for sig in (signal.SIGTERM, signal.SIGKILL):
            try:
                os.killpg(proc.pid, sig)
                proc.communicate(timeout=5)
                break
            except (OSError, subprocess.TimeoutExpired):
                pass
        raise Cancelled()


def _copy_wii(game, dst, cancel):
    wit, wwt = _tools()
    src = shlex.quote(game['source'])
    if core.is_mounted_dir(dst):
        # Mismo esquema que "Añadir juego": /wbfs/Título [ID6]/ID6.wbfs, en trozos de <4 GB
        title = re.sub(r'[<>:"/\\|?*\x00-\x1f]', '', game['title']).strip(' .') or game['id']
        game_dir = core.wbfs_folder(dst) / f"{title} [{game['id']}]"
        # Si la carpeta existe es sin imagen válida dentro (o el juego constaría
        # como presente): restos de una copia interrumpida
        shutil.rmtree(game_dir, ignore_errors=True)
        cmd = f"{wit} COPY {src} --wbfs --split --DEST {shlex.quote(str(game_dir / (game['id'] + '.wbfs')))}"
        undo = lambda: shutil.rmtree(game_dir, ignore_errors=True)
    else:
        cmd = f'{wwt} ADD -p {shlex.quote(dst)} {src}'
        undo = lambda: core.run(f"{wwt} REMOVE -p {shlex.quote(dst)} {game['id']}")
    # No dejar una copia a medias que luego aparecería como juego roto
    try:
        _stdout, stderr, rc = _run(cmd, cancel)
    except Cancelled:
        undo()
        raise
    if rc != 0:
        undo()
        reason = stderr.strip().splitlines()
        raise OSError(reason[-1].strip() if reason else f'{cmd.split()[0]} devolvió código {rc}')


def _copy_gc(game, cancel):
    target = Path(game['target'])
    created = not target.exists()
    target.mkdir(parents=True, exist_ok=True)
    dest = None
    try:
        for src, dest, _size in game['files']:
            with open(src, 'rb') as fsrc, open(dest, 'wb') as fdst:
                while chunk := fsrc.read(CHUNK):
                    if cancel.is_set():
                        raise Cancelled()
                    fdst.write(chunk)
    except BaseException:
        # Fuera el archivo a medias (y la carpeta, si no existía antes)
        if dest:
            Path(dest).unlink(missing_ok=True)
        if created:
            shutil.rmtree(target, ignore_errors=True)
        raise


def migrate(src, dst, cancel, on_game=None, only=None):
    """
    Copia al destino los juegos del origen que le falten, o solo los de only
    (ver plan()), (bloqueante: llamar vía run_async). Secuencial a propósito: una sola escritura a la vez sobre
    la unidad. cancel es un threading.Event: al activarlo se interrumpe la
    copia en curso (sin dejarla a medias) y no se empiezan más.
    on_game(n, total, game) se llama, desde este hilo, al empezar cada juego.

    Devuelve (copiados, [(game, error)], cancelado, plan). Al volver, lo
    copiado está de verdad escrito en el disco. Lanza OSError si no caben.
    """
    todo = plan(src, dst, only)
    games = todo['games']
    if todo['free_bytes'] is not None and todo['total_bytes'] > todo['free_bytes']:
        raise OSError(f"No caben en el destino: hacen falta {todo['total_bytes'] / 1024**3:.2f} GB "
                      f"y hay {todo['free_bytes'] / 1024**3:.2f} GB libres")
    copied, failed = [], []
    # Una sola medición para toda la migración: la barra marca el avance global
    with core.track_write(dst, todo['total_bytes']):
        for n, game in enumerate(games):
            if cancel.is_set():
                break
            if on_game:
                on_game(n, len(games), game)
            try:
                if game['platform'] == 'gc':
                    _copy_gc(game, cancel)
                else:
                    _copy_wii(game, dst, cancel)
                copied.append(game)
            except Cancelled:
                break
            except OSError as e:
                failed.append((game, str(e)))
        if not os.path.isdir(dst):
            # track_write solo vacía la caché de las unidades montadas
            core.run(f'sync {shlex.quote(dst)}', timeout=3600)
    return copied, failed, cancel.is_set(), todo
