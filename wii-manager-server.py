#!/usr/bin/env python3
"""
wii-manager-server.py
=====================
Servidor HTTP local que actúa como backend de la GUI WiiFlow Manager.

Expone una API REST minimalista en http://localhost:8765 que traduce las
peticiones del frontend en llamadas reales a los ejecutables `wit` y `wwt`
de Wiimm (https://wit.wiimm.de/).

Uso:
    python3 wii-manager-server.py

El archivo wii-manager.html debe estar en el mismo directorio; el servidor
lo sirve directamente en la ruta raíz (/).

Rutas de la API:
    GET  /api/status            Detecta rutas de wit y wwt en el sistema
    GET  /api/list?part=...     Lista juegos y espacio en la partición WBFS
    POST /api/add               Añade un ISO a la partición WBFS
    POST /api/remove            Elimina un juego por su Game ID
    POST /api/extract           Extrae un juego a fichero ISO
    POST /api/verify            Verifica la integridad de juegos o archivos
    POST /api/run               Ejecuta un comando wwt/wit arbitrario
    GET  /api/progress          Avance de la operación larga en curso
    GET  /api/browse            Explora carpetas del equipo (selector de archivos)
    GET  /api/devices           Discos y particiones, y cuáles se pueden formatear
    POST /api/format            Formatea una unidad como FAT32 o WBFS
    POST /api/nintendont/install  Instala Nintendont en una unidad
    GET  /api/loaders/status    Cargadores instalados y si tienen versión nueva
    POST /api/loaders/install   Instala o actualiza los cargadores
    GET  /api/gamecube/inspect  Lee la cabecera de una imagen de GameCube
    GET  /api/gamecube/list     Lista los juegos de GameCube de una unidad
    POST /api/gamecube/add      Copia un juego de GameCube a la unidad
    POST /api/gamecube/remove   Elimina un juego de GameCube

    La tabla completa está en ROUTES. La app GTK4 (wii-manager-gtk/) no usa
    el servidor HTTP: importa este fichero y llama a las funciones api_xxx.

Seguridad:
    El servidor escucha solo en 127.0.0.1 (localhost), no es accesible
    desde la red. El endpoint /api/run rechaza cualquier comando que no
    empiece por 'wwt', 'wit' o 'wdf'.

Licencia: GNU GPL v3
"""

import sys
import tempfile
import threading
import time
sys.stdout.reconfigure(line_buffering=True)
sys.stderr.reconfigure(line_buffering=True)

import http.server
import io
import json
import os
import urllib.request
import re
import shlex
import shutil
import stat
import struct
import subprocess
import urllib.parse
import zipfile
from contextlib import contextmanager
from pathlib import Path

# Puerto en el que escucha el servidor
PORT = 8765

# Ruta al HTML del frontend (mismo directorio que este script)
HTML_FILE = Path(__file__).parent / 'wii-manager.html'


# ══════════════════════════════════════════════════════════════
#  UTILIDADES GENERALES
# ══════════════════════════════════════════════════════════════

# Dentro de un Flatpak las herramientas (wit, wwt, lsblk, mkfs, pkexec…) no
# están en el sandbox: los comandos se ejecutan en el sistema anfitrión con
# flatpak-spawn (requiere el permiso --talk-name=org.freedesktop.Flatpak).
IN_FLATPAK = os.path.exists('/.flatpak-info')


def which(cmd):
    """
    Devuelve la ruta absoluta de un ejecutable buscándolo en el PATH,
    o None si no existe. Equivale a `which <cmd>` en bash.
    """
    if IN_FLATPAK:
        return run(f'command -v {shlex.quote(cmd)}')[0].strip() or None
    return shutil.which(cmd)


def run(cmd, timeout=120, read_total=None):
    """
    Ejecuta un comando de shell y captura su salida.

    Parámetros:
        cmd     -- comando completo como string (se ejecuta con shell=True)
        timeout -- segundos máximos de espera (default: 120)
        read_total -- si se indica, bytes que el comando va a leer en total
                      (p. ej. el tamaño de la imagen a verificar): permite a
                      api_progress() calcular el porcentaje de avance.

    Devuelve:
        (stdout, stderr, returncode)
        En caso de timeout o excepción devuelve returncode=1 y el error
        en stderr para que el frontend pueda mostrarlo en el terminal.
    """
    global _read_progress
    try:
        proc = subprocess.Popen(
            ['flatpak-spawn', '--host', '--watch-bus', 'sh', '-c', cmd] if IN_FLATPAK else cmd,
            shell=not IN_FLATPAK,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding='utf-8',
            errors='replace'    # caracteres inválidos → replacement char
        )
        # En Flatpak el proceso real corre en el anfitrión: no se puede medir lo que lee
        if read_total and not IN_FLATPAK:
            _read_progress = {'pid': proc.pid, 'total': read_total}
        try:
            stdout, stderr = proc.communicate(timeout=timeout)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.communicate()
            return '', f'Timeout: el comando tardó más de {timeout}s', 1
        return stdout, stderr, proc.returncode
    except Exception as e:
        return '', str(e), 1
    finally:
        if read_total:
            _read_progress = None


# Comando en curso del que se conoce cuánto va a leer: {'pid', 'total'}
_read_progress = None


def _bytes_read(pid):
    """Bytes leídos hasta ahora por un proceso y sus hijos directos (/proc/PID/io)."""
    total = 0
    try:
        children = Path(f'/proc/{pid}/task/{pid}/children').read_text().split()
    except OSError:
        children = []
    for p in [pid, *children]:
        try:
            total += int(Path(f'/proc/{p}/io').read_text().split()[1])
        except (OSError, ValueError, IndexError):
            pass
    return total


def part_flag(part):
    """
    Construye el flag de partición para los comandos wwt.

    Si se ha configurado una ruta (ej. '/dev/sdb1' o '/ruta/disco.wbfs')
    devuelve '-p "<ruta>"'. Si la ruta está vacía, devuelve '--auto' para
    que wwt detecte automáticamente la partición WBFS montada.

    Ejemplos:
        part_flag('/dev/sdb1')  →  '-p "/dev/sdb1"'
        part_flag('')           →  '--auto'
        part_flag(None)         →  '--auto'
    """
    if part and part.strip():
        return f'-p "{part.strip()}"'
    return '--auto'


# ══════════════════════════════════════════════════════════════
#  PARSERS DE SALIDA DE wwt
# ══════════════════════════════════════════════════════════════

def parse_wwt_list(output):
    """
    Parsea la salida de texto de 'wwt LIST --long' y devuelve una lista
    de diccionarios con la información de cada juego.

    Formato esperado (--long):
        1  RMCP01  4.37 GiB  Mario Kart Wii
        2  SOUE01  7.92 GiB  Super Smash Bros. Brawl

    Si el formato largo falla (algunos builds de wwt omiten el tamaño),
    se intenta un formato simplificado:
        RMCP01  Mario Kart Wii

    Cada juego se devuelve como:
        {
            'id':     'RMCP01',       # Game ID de 4-6 caracteres
            'title':  'Mario Kart Wii',
            'size':   4.37,           # Tamaño en GiB (0 si no disponible)
            'fmt':    'wbfs',         # Siempre 'wbfs' (formato de partición)
            'region': 'PAL',          # Derivado del 4º carácter del ID
            'year':   0,              # wwt no devuelve el año
        }
    """
    games = []

    # Regex para formato largo: índice, ID, tamaño+unidad, título
    pattern = re.compile(
        r'^\s*\d+\s+'               # número de índice (1, 2, 3…)
        r'([A-Z0-9]{4,6})\s+'       # Game ID (ej. RMCP01)
        r'(?:[^\s]+\s+)?'           # ruta opcional (algunos builds la incluyen)
        r'([\d.]+)\s*(GiB|MiB|GB|MB)\s+'  # tamaño y unidad
        r'(.+?)\s*$',               # título hasta fin de línea
        re.MULTILINE
    )
    for m in pattern.finditer(output):
        game_id  = m.group(1).strip()
        size_raw = float(m.group(2))
        unit     = m.group(3).upper()
        title    = m.group(4).strip()

        # Ignorar líneas de cabecera o comentarios
        if not title or title.startswith('#'):
            continue

        # Normalizar a GiB si viene en MiB o MB
        if 'MIB' in unit or unit == 'MB':
            size_raw /= 1024

        games.append({
            'id':     game_id,
            'title':  title,
            'size':   round(size_raw, 2),
            'fmt':    'wbfs',
            'region': region_from_id(game_id),
            'year':   0,
            'platform': 'wii',
        })

    # Fallback: formato simplificado sin tamaño (wwt sin --long)
    if not games:
        for line in output.splitlines():
            line = line.strip()
            # Saltar líneas vacías, comentarios y separadores
            if not line or line.startswith('#') or line.startswith('='):
                continue
            m2 = re.match(r'^([A-Z0-9]{4,6})\s+(.+)$', line)
            if m2:
                games.append({
                    'id':     m2.group(1),
                    'title':  m2.group(2).strip(),
                    'size':   0,
                    'fmt':    'wbfs',
                    'region': region_from_id(m2.group(1)),
                    'year':   0,
                    'platform': 'wii',
                })

    return games


def region_from_id(game_id):
    """
    Deriva la región de un juego Wii a partir del 4º carácter de su Game ID.

    El sistema de IDs de Nintendo codifica la región en la posición 3 (0-based):
        RMCP01  →  'P'  →  PAL
        RMCE01  →  'E'  →  NTSC-U (USA)
        RMCJ01  →  'J'  →  NTSC-J (Japón)

    Devuelve 'UNK' si el carácter no está en la tabla conocida,
    o '?' si el ID es demasiado corto.
    """
    if len(game_id) < 4:
        return '?'
    return {
        'P': 'PAL',     # Europa
        'E': 'NTSC-U',  # USA
        'J': 'NTSC-J',  # Japón
        'K': 'NTSC-K',  # Corea
        'W': 'NTSC-T',  # Taiwan
        'D': 'PAL-DE',  # Alemania
        'F': 'PAL-FR',  # Francia
        'S': 'PAL-ES',  # España
        'I': 'PAL-IT',  # Italia
    }.get(game_id[3], 'UNK')


def to_gib(value, unit):
    """
    Convierte un valor de tamaño a GiB independientemente de la unidad
    de origen.

    Parámetros:
        value -- string o número con el valor numérico
        unit  -- string con la unidad (GiB, MiB, GB, MB, TiB, TB, KiB, KB)

    Devuelve:
        float en GiB
    """
    v    = float(value)
    unit = unit.strip().upper()
    if unit in ('MIB', 'MB'):  return v / 1024
    if unit in ('KIB', 'KB'):  return v / (1024 * 1024)
    if unit in ('TIB', 'TB'):  return v * 1024
    return v  # GiB o GB, ya en la unidad correcta


def parse_wwt_space(output):
    """
    Parsea la salida de 'wwt SPACE' y extrae el espacio usado, libre y total
    de la partición WBFS.

    wwt puede producir varios formatos según la versión:

        Formato clave = valor:
            total = 120.00 GiB
            used  =  45.32 GiB
            free  =  74.68 GiB

        Formato valor + etiqueta inline (en la misma línea):
            45.32 GiB used   74.68 GiB free   120.00 GiB total

    El parser intenta ambos formatos en cada línea y acepta cualquier
    combinación de unidades (GiB, MiB, TiB, GB, MB, TB, KiB, KB).

    Si alguno de los tres valores no aparece en la salida, lo deriva
    matemáticamente de los otros dos.

    Devuelve:
        (used_gib, free_gib, total_gib)  — floats redondeados a 2 decimales
    """
    total = used = free = 0.0

    for line in output.splitlines():
        line = line.strip()
        if not line or line.startswith('#'):
            continue

        # ── Formato "clave = valor unidad" ──────────────────────
        m = re.search(
            r'\btotal\b\s*[=:]\s*([\d.]+)\s*(GiB|MiB|GB|MB|TiB|TB|KiB|KB)',
            line, re.I)
        if m: total = to_gib(m.group(1), m.group(2))

        m = re.search(
            r'\bused\b\s*[=:]\s*([\d.]+)\s*(GiB|MiB|GB|MB|TiB|TB|KiB|KB)',
            line, re.I)
        if m: used = to_gib(m.group(1), m.group(2))

        m = re.search(
            r'\bfree\b\s*[=:]\s*([\d.]+)\s*(GiB|MiB|GB|MB|TiB|TB|KiB|KB)',
            line, re.I)
        if m: free = to_gib(m.group(1), m.group(2))

        # ── Formato "valor unidad etiqueta" ─────────────────────
        for val, unit, label in re.findall(
                r'([\d.]+)\s*(GiB|MiB|GB|MB|TiB|TB|KiB|KB)\s+(used|free|total)',
                line, re.I):
            g   = to_gib(val, unit)
            lbl = label.lower()
            if   lbl == 'used':  used  = g
            elif lbl == 'free':  free  = g
            elif lbl == 'total': total = g

    # Derivar el valor que falte si solo hay dos
    if total and not free and used:   free  = total - used
    if total and not used and free:   used  = total - free
    if used  and free  and not total: total = used  + free

    return round(used, 2), round(free, 2), round(total, 2)


def parse_wwt_sections(output):
    """
    Parsea la salida de 'wwt LIST --sections' (bloques [wbfs-N] y [disc-N] con
    líneas clave=valor), que no cambia de formato entre versiones de wwt como
    sí lo hacen las tablas de 'wwt LIST --long' y 'wwt SPACE'.

    Devuelve (juegos, (used_gib, free_gib, total_gib)); el espacio es None si
    la salida no trae el bloque [wbfs-N].
    """
    games, space = [], None
    for kind, block in re.findall(r'^\[(wbfs|disc)-\d+\]\s*$(.*?)(?=^\[|\Z)', output, re.MULTILINE | re.DOTALL):
        info = dict(line.split('=', 1) for line in block.splitlines() if '=' in line)
        try:
            if kind == 'wbfs':
                space = tuple(round(int(info[k]) / 1024, 2) for k in ('used_mib', 'free_mib', 'total_mib'))
                continue
            size = int(info.get('size') or 0)
        except (KeyError, ValueError):
            if kind == 'wbfs':
                continue
            size = 0
        game_id = info.get('id', '').strip()
        if not game_id:
            continue
        games.append({
            'id':     game_id,
            'title':  (info.get('title') or info.get('name') or game_id).strip(),
            'size':   round(size / 1024**3, 2),
            'size_bytes': size,
            'fmt':    'wbfs',
            'region': region_from_id(game_id),
            'year':   0,
            'platform': 'wii',
        })
    return games, space


# ══════════════════════════════════════════════════════════════
#  HANDLERS DE LA API REST
# ══════════════════════════════════════════════════════════════
#  JUEGOS WII EN UNIDADES MONTADAS (FAT32/NTFS, carpeta /wbfs/)
# ══════════════════════════════════════════════════════════════
#
# wwt solo trabaja con particiones/archivos WBFS. En una unidad FAT32
# montada, los cargadores (WiiFlow, USB Loader GX) esperan los juegos Wii
# como archivos en /wbfs/Título [ID6]/ID6.wbfs (partidos en trozos de <4 GB),
# y eso se gestiona con wit.

# ── Progreso de las copias largas (añadir juegos) ──────────────
#
# El avance se mide con el contador de sectores escritos del dispositivo
# destino (/sys/dev/block/MAJ:MIN/stat), no con el tamaño del archivo: el
# archivo "crece" enseguida en la caché de memoria mientras el USB sigue
# escribiendo durante minutos, y la barra marcaría 90 % nada más empezar.

_write_progress = None   # {'stat_file', 'start', 'total'} de la copia en curso


def _sectors_written(stat_file):
    """Sectores escritos en el dispositivo desde el arranque (7.º campo de su archivo stat), o None si no se puede leer."""
    try:
        return int(Path(stat_file).read_text().split()[6])
    except (OSError, ValueError, IndexError):
        return None


@contextmanager
def track_write(dest_path, total_bytes):
    """
    Envuelve una copia hacia dest_path (carpeta o dispositivo de bloques) de
    total_bytes para que api_progress() pueda informar del avance. Al salir,
    vacía la caché de escritura de la unidad: cuando la operación termina,
    los datos están de verdad en el disco y se puede desconectar.
    """
    global _write_progress
    stat_file = None
    try:
        st = os.stat(dest_path)
        dev = st.st_rdev if stat.S_ISBLK(st.st_mode) else st.st_dev
        candidate = f'/sys/dev/block/{os.major(dev)}:{os.minor(dev)}/stat'
        if _sectors_written(candidate) is not None:
            stat_file = candidate
    except OSError:
        pass
    _write_progress = {'stat_file': stat_file, 'total': total_bytes,
                       'start': _sectors_written(stat_file) if stat_file else None}
    try:
        yield
        if os.path.isdir(dest_path):
            run(f'sync -f {shlex.quote(str(dest_path))}', timeout=3600)
    finally:
        _write_progress = None


_step_progress = None    # (porcentaje, texto) de una operación por pasos (formatear)


def set_step(percent, text=''):
    """Marca el paso actual de una operación por pasos; set_step(None) al terminar."""
    global _step_progress
    _step_progress = None if percent is None else (percent, text)


def api_progress(params):
    """
    GET /api/progress

    Avance de la operación larga en curso (añadir juego, formatear).
    Respuesta JSON:
        { "active": bool,
          "percent": 0-100 o null si no se puede medir,
          "step": "texto del paso actual" (solo operaciones por pasos) }
    """
    step = _step_progress
    if step:
        return {'active': True, 'percent': step[0], 'step': step[1]}
    reading = _read_progress
    if reading:
        percent = int(_bytes_read(reading['pid']) * 100 / reading['total'])
        return {'active': True, 'percent': max(0, min(99, percent)), 'step': ''}
    progress = _write_progress
    if not progress:
        return {'active': False, 'percent': None, 'step': ''}
    now = _sectors_written(progress['stat_file']) if progress['stat_file'] else None
    if now is None or not progress['total']:
        return {'active': True, 'percent': None, 'step': ''}
    written = (now - progress['start']) * 512
    # 99 % como tope: el 100 % solo se da cuando la operación ha terminado
    return {'active': True, 'percent': max(0, min(99, int(written * 100 / progress['total']))), 'step': ''}


# Carpetas de juego que se están copiando ahora mismo (api_add en curso),
# para rechazar un segundo "añadir" del mismo juego mientras dura la copia.
_adds_in_progress = set()
_adds_lock = threading.Lock()


def is_mounted_dir(part):
    """True si la ruta es un directorio (unidad montada) y no una partición WBFS."""
    return bool(part) and os.path.isdir(part)


def wbfs_folder(part):
    """Carpeta /wbfs/ de una unidad montada (la propia ruta si ya apunta a ella)."""
    p = Path(part)
    return p if p.name.lower() == 'wbfs' else p / 'wbfs'


def wit_list_images(wit, path):
    """
    Lista las imágenes Wii de un archivo o directorio (recursivo) con
    'wit LIST --sections'. Devuelve (juegos, cmd, stdout, stderr, rc); cada
    juego lleva además 'path' con la ruta del archivo de imagen.
    """
    cmd = f'{wit} LIST --sections -r {shlex.quote(str(path))}'
    stdout, stderr, rc = run(cmd)
    games = []
    for block in re.split(r'^\[disc-\d+\]\s*$', stdout, flags=re.MULTILINE)[1:]:
        info = dict(line.split('=', 1) for line in block.splitlines() if '=' in line)
        game_id = info.get('id', '').strip()
        if not game_id or 'WII' not in info.get('disctype', 'Wii').upper():
            continue
        try:
            size = int(info.get('size') or 0) / 1024**3
        except ValueError:
            size = 0
        games.append({
            'id':     game_id,
            'title':  (info.get('title') or info.get('name') or game_id).strip(),
            'size':   round(size, 2),
            'size_bytes': int(size * 1024**3),
            'fmt':    info.get('filetype', 'WBFS').split('/')[0].lower(),
            'region': region_from_id(game_id),
            'year':   0,
            'platform': 'wii',
            'path':   info.get('filename', '').strip(),
        })
    return games, cmd, stdout, stderr, rc


# ══════════════════════════════════════════════════════════════
#  IMÁGENES DE DOLPHIN (RVZ / GCZ)
# ══════════════════════════════════════════════════════════════
# wit/wwt y los cargadores de la consola no leen los formatos comprimidos de
# Dolphin: se convierten antes a ISO con dolphin-tool (el de sistema o el del
# Flatpak de Dolphin).

DOLPHIN_SUFFIXES = {'.rvz', '.gcz'}
DOLPHIN_FLATPAK = 'org.DolphinEmu.dolphin-emu'
DOLPHIN_TMP_DIR = Path.home() / '.cache' / 'wii-manager' / 'tmp'
DOLPHIN_MISSING = ('Para usar imágenes RVZ/GCZ hace falta Dolphin (dolphin-tool): '
                   'instálalo desde Software o con «flatpak install org.DolphinEmu.dolphin-emu».')


def is_dolphin_image(path):
    """True si la extensión es la de un formato comprimido de Dolphin (RVZ/GCZ)."""
    return Path(str(path)).suffix.lower() in DOLPHIN_SUFFIXES


def dolphin_tool(read_dir=None, write_dir=None):
    """
    Prefijo del comando dolphin-tool, o None si Dolphin no está instalado. El
    Flatpak va aislado: hay que darle acceso a las carpetas que va a leer y escribir.
    """
    if which('dolphin-tool'):
        return 'dolphin-tool'
    if which('flatpak') and run(f'flatpak info {DOLPHIN_FLATPAK}')[2] == 0:
        access = ''.join(f' --filesystem={shlex.quote(str(d) + mode)}'
                         for d, mode in ((read_dir, ':ro'), (write_dir, '')) if d)
        return f'flatpak run --command=dolphin-tool{access} {DOLPHIN_FLATPAK}'
    return None


def inspect_dolphin_image(file_path):
    """
    Metadatos de una imagen RVZ/GCZ leídos con 'dolphin-tool header' (sin convertirla).
    Devuelve { valid, id, title, disc (None: no se sabe hasta convertir), region, size_gb, platform }.
    """
    p = Path(file_path)
    tool = dolphin_tool(read_dir=p.parent)
    if not tool:
        return {'valid': False, 'error': DOLPHIN_MISSING}
    stdout, stderr, rc = run(f'{tool} header -j -i {shlex.quote(str(p))} < /dev/null')
    try:
        info = json.loads(stdout)
    except ValueError:
        info = {}
    game_id = (info.get('game_id') or '').strip().upper()
    if rc != 0 or len(game_id) < 4:
        return {'valid': False, 'error': f'No se reconoce como imagen de Dolphin válida: {p.name}'}
    return {
        'valid': True,
        'id': game_id,
        'title': (info.get('internal_name') or p.stem).strip(),
        'disc': None,
        'region': region_from_id(game_id),
        'size_gb': None,
        # Solo los discos de Wii llevan Title ID
        'platform': 'wii' if info.get('title_id') else 'gc',
    }


@contextmanager
def dolphin_to_iso(file_path):
    """
    Convierte una imagen RVZ/GCZ a un ISO temporal, que se borra al salir.
    Entrega (ruta_del_iso, None) o (None, mensaje_de_error).
    """
    p = Path(file_path)
    DOLPHIN_TMP_DIR.mkdir(parents=True, exist_ok=True)
    tool = dolphin_tool(read_dir=p.parent, write_dir=DOLPHIN_TMP_DIR)
    if not tool:
        yield None, DOLPHIN_MISSING
        return
    tmp_dir = Path(tempfile.mkdtemp(dir=DOLPHIN_TMP_DIR))
    try:
        iso = tmp_dir / f'{p.stem}.iso'
        _, stderr, rc = run(f'{tool} convert -f iso -i {shlex.quote(str(p))} -o {shlex.quote(str(iso))} < /dev/null',
                            timeout=3600)
        if rc != 0 or not iso.is_file():
            reason = stderr.strip().splitlines()
            yield None, f'No se pudo convertir {p.name} a ISO' + (f': {reason[-1]}' if reason else '')
        else:
            yield str(iso), None
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)


def find_wii_image(wit, part, game_id):
    """Ruta del archivo de imagen de un juego (por ID) en /wbfs/ de una unidad montada, o None."""
    games = wit_list_images(wit, wbfs_folder(part))[0]
    return next((g['path'] for g in games if g['id'] == game_id.upper() and g['path']), None)


# ══════════════════════════════════════════════════════════════

def api_status(params):
    """
    GET /api/status

    Detecta si wit y wwt están instalados en el sistema usando `which`.

    Respuesta JSON:
        {
            "wwt":     "/usr/bin/wwt",  # ruta o "" si no se encuentra
            "wit":     "/usr/bin/wit",
            "wwt_ok":  true,
            "wit_ok":  true
        }
    """
    wwt = which('wwt') or ''
    wit = which('wit')  or ''
    return {
        'wwt':    wwt,
        'wit':    wit,
        'wwt_ok': bool(wwt),
        'wit_ok': bool(wit),
    }


def api_list(params):
    """
    GET /api/list?part=<ruta>&wwt=<ruta_wwt>

    Lista todos los juegos de la partición WBFS y el espacio en disco.
    Ejecuta 'wwt LIST --long' (con fallback sin --long) y 'wwt SPACE'.

    Parámetros GET:
        part  -- ruta de la partición (ej. '/dev/sdb1'). Vacío = --auto
        wwt   -- ruta al ejecutable wwt (opcional, por defecto detectado)

    Respuesta JSON:
        {
            "games":     [ { id, title, size, fmt, region, year }, ... ],
            "used_gb":   45.32,
            "free_gb":   74.68,
            "total_gb":  120.0,
            "stdout":    "...",   # salida raw de wwt LIST (para el terminal)
            "stderr":    "...",
            "rc":        0,       # código de retorno de wwt
            "cmd":       "wwt LIST -p /dev/sdb1 --long",
            "space_raw": "..."    # salida raw de wwt SPACE (para depuración)
        }
    """
    part = params.get('part', '').strip()
    wwt  = params.get('wwt',  which('wwt') or 'wwt')
    wit  = params.get('wit',  which('wit') or 'wit')
    flag = part_flag(part)
    wbfs_space = None

    if is_mounted_dir(part):
        # Unidad montada: los juegos Wii son archivos en /wbfs/
        folder = wbfs_folder(part)
        if folder.is_dir():
            games, cmd, stdout, stderr, rc = wit_list_images(wit, folder)
        else:
            games, cmd, stdout, stderr, rc = [], '', '', '', 0
    else:
        # Intentar primero con --long para obtener tamaños
        cmd = f'{wwt} LIST {flag} --long'
        stdout, stderr, rc = run(cmd)

        # Si falla (código de error), reintentar sin --long
        if rc != 0:
            cmd = f'{wwt} LIST {flag}'
            stdout, stderr, rc = run(cmd)

        games = parse_wwt_list(stdout)

        # La tabla de --long cambia entre versiones de wwt: si responde, usar
        # la salida por secciones, que además trae el espacio de la partición
        if rc == 0:
            sec_games, wbfs_space = parse_wwt_sections(run(f'{wwt} LIST {flag} --sections')[0])
            if len(sec_games) >= len(games):
                games = sec_games

    # Si se proporcionó una ruta o partición, comprobar si hay juegos de GameCube
    gc_games = []
    if part:
        try:
            gc_res = api_gc_list({'path': part})
            gc_games = gc_res.get('games', [])
            games.extend(gc_games)
        except Exception:
            pass

    # Obtener espacio en disco de la partición
    cmd_sp = f'{wwt} SPACE {flag} --no-colors'
    sp_out, sp_err, _ = run(cmd_sp)
    used, free, total = wbfs_space or parse_wwt_space(sp_out)

    # wwt SPACE solo entiende particiones/archivos WBFS. Si la ruta es una
    # unidad montada (FAT32/NTFS con carpetas wbfs/ y games/), wwt falla y
    # devolvería 0: preguntar entonces al sistema de archivos.
    if not total and part and os.path.isdir(part):
        try:
            usage = shutil.disk_usage(part)
            used, free, total = (round(v / 1024**3, 2) for v in (usage.used, usage.free, usage.total))
        except OSError:
            pass

    return {
        'games':     games,
        'gc_games':  gc_games,
        'used_gb':   used,
        'free_gb':   free,
        'total_gb':  total,
        'stdout':    stdout,
        'stderr':    stderr,
        'rc':        rc,
        'cmd':       cmd,
        'space_raw': sp_out.strip() or sp_err.strip(),
    }


def api_add(params):
    """
    POST /api/add

    Añade un archivo ISO/WBFS a la partición WBFS usando 'wwt ADD'.
    Este comando puede tardar varios minutos para ISOs grandes;
    el timeout está fijado en 600 segundos (10 minutos).

    Si 'part' es una unidad montada (FAT32/NTFS), el juego se copia con
    'wit COPY' a /wbfs/Título [ID6]/ID6.wbfs, partido en trozos de <4 GB.

    Las imágenes de Dolphin (RVZ/GCZ) se convierten antes a un ISO temporal.

    Cuerpo JSON:
        {
            "part": "/dev/sdb1",        # partición destino (vacío = --auto)
            "src":  "/ruta/juego.iso",  # archivo origen (obligatorio)
            "opts": "",                 # opciones adicionales para wwt
            "wwt":  "/usr/bin/wwt"      # ruta al ejecutable (opcional)
        }

    Respuesta JSON:
        { "cmd": "...", "stdout": "...", "stderr": "...", "rc": 0 }
    """
    src = params.get('src', '')
    if is_dolphin_image(src) and os.path.isfile(src):
        with dolphin_to_iso(src) as (iso, error):
            if error:
                return {'error': error}
            return _api_add({**params, 'src': iso})
    return _api_add(params)


def _api_add(params):
    """Implementación de api_add para una imagen que wit/wwt saben leer (las RVZ/GCZ llegan ya convertidas a ISO)."""
    part = params.get('part', '').strip()
    src  = params.get('src',  '')
    wwt  = params.get('wwt',  which('wwt') or 'wwt')
    opts = params.get('opts', '')

    if not src:
        return {'error': 'Falta el parámetro src'}

    if is_mounted_dir(part):
        wit = params.get('wit', which('wit') or 'wit')
        found, cmd, stdout, stderr, rc = wit_list_images(wit, src)
        if not found:
            return {'cmd': cmd, 'stdout': stdout, 'rc': rc or 1,
                    'stderr': stderr or f'No es una imagen de Wii válida: {src}'}
        game = found[0]
        if find_wii_image(wit, part, game['id']):
            return {'cmd': cmd, 'stdout': '', 'rc': 1,
                    'stderr': f"El juego {game['title']} [{game['id']}] ya está en la unidad"}
        # Título apto para nombre de carpeta en FAT32
        title = re.sub(r'[<>:"/\\|?*\x00-\x1f]', '', game['title']).strip(' .') or game['id']
        game_dir = wbfs_folder(part) / f"{title} [{game['id']}]"
        dest = game_dir / f"{game['id']}.wbfs"
        key = str(game_dir)
        with _adds_lock:
            if key in _adds_in_progress:
                return {'cmd': cmd, 'stdout': '', 'rc': 1,
                        'stderr': f"El juego {game['title']} [{game['id']}] ya se está copiando a la unidad; "
                                  'espera a que termine'}
            _adds_in_progress.add(key)
        try:
            # La carpeta existe pero sin imagen válida dentro (o find_wii_image
            # la habría encontrado). Si algo se ha escrito hace un momento, es
            # otra copia en marcha (otro proceso): no tocarla. Si no, son restos
            # de una copia interrumpida: empezar de cero.
            if game_dir.is_dir():
                newest = max((f.stat().st_mtime for f in game_dir.iterdir()), default=0)
                if time.time() - newest < 120:
                    return {'cmd': cmd, 'stdout': '', 'rc': 1,
                            'stderr': f"Parece que el juego {game['title']} [{game['id']}] ya se está copiando "
                                      'a la unidad; espera a que termine'}
                shutil.rmtree(game_dir, ignore_errors=True)
            cmd = f'{wit} COPY {shlex.quote(src)} --wbfs --split --DEST {shlex.quote(str(dest))}'
            with track_write(part, game['size_bytes']):
                stdout, stderr, rc = run(cmd, timeout=3600)
            if rc != 0:
                # No dejar una copia a medias que luego aparecería como juego roto
                shutil.rmtree(game_dir, ignore_errors=True)
        finally:
            with _adds_lock:
                _adds_in_progress.discard(key)
        return {'cmd': cmd, 'stdout': stdout, 'stderr': stderr, 'rc': rc}

    flag = part_flag(part)
    cmd  = f'{wwt} ADD {flag} {opts} "{src}"'
    try:
        src_size = os.path.getsize(src)
    except OSError:
        src_size = 0
    if part and os.path.exists(part):
        with track_write(part, src_size):
            stdout, stderr, rc = run(cmd, timeout=3600)
    else:
        stdout, stderr, rc = run(cmd, timeout=3600)
    return {'cmd': cmd, 'stdout': stdout, 'stderr': stderr, 'rc': rc}


def api_remove(params):
    """
    POST /api/remove

    Elimina un juego de la partición WBFS por su Game ID usando 'wwt REMOVE'.

    Cuerpo JSON:
        {
            "part": "/dev/sdb1",  # partición (vacío = --auto)
            "id":   "RMCP01",     # Game ID del juego a eliminar (obligatorio)
            "wwt":  "/usr/bin/wwt"
        }

    Respuesta JSON:
        { "cmd": "...", "stdout": "...", "stderr": "...", "rc": 0 }
    """
    part    = params.get('part', '').strip()
    game_id = params.get('id',   '')
    wwt     = params.get('wwt',  which('wwt') or 'wwt')

    if not game_id:
        return {'error': 'Falta el parámetro id'}

    if is_mounted_dir(part):
        # Unidad montada: borrar los archivos del juego en /wbfs/
        wit = params.get('wit', which('wit') or 'wit')
        image = find_wii_image(wit, part, game_id)
        if not image:
            return {'cmd': '', 'stdout': '', 'rc': 1,
                    'stderr': f'No se encontró el juego {game_id} en {wbfs_folder(part)}'}
        image = Path(image)
        try:
            if image.parent.resolve() != wbfs_folder(part).resolve() and f'[{game_id.upper()}]' in image.parent.name.upper():
                # Carpeta propia del juego (Título [ID6]/): se borra entera
                shutil.rmtree(image.parent)
                removed = str(image.parent)
            else:
                # Archivo suelto: la imagen y sus trozos (.wbf1, .wbf2…)
                for f in image.parent.glob(image.stem + '.wb[f0-9]*'):
                    f.unlink()
                removed = str(image)
        except OSError as e:
            return {'cmd': '', 'stdout': '', 'stderr': f'Error al eliminar {image}: {e}', 'rc': 1}
        return {'cmd': f'rm {removed}', 'stdout': f'Eliminado: {removed}', 'stderr': '', 'rc': 0}

    flag = part_flag(part)
    cmd  = f'{wwt} REMOVE {flag} {game_id}'
    stdout, stderr, rc = run(cmd)
    return {'cmd': cmd, 'stdout': stdout, 'stderr': stderr, 'rc': rc}


def api_extract(params):
    """
    POST /api/extract

    Extrae un juego de la partición WBFS a un archivo ISO usando 'wwt EXTRACT'.
    El timeout es de 600 segundos por el tamaño de los ISOs de Wii.

    Si 'id' es la ruta de un archivo de imagen, o 'part' es una unidad montada
    (FAT32/NTFS, con los juegos en /wbfs/), se convierte con 'wit COPY'.

    Cuerpo JSON:
        {
            "part": "/dev/sdb1",    # partición origen (vacío = --auto)
            "id":   "RMCP01",       # Game ID a extraer (obligatorio)
            "dest": "/tmp/",        # directorio destino (default: /tmp/)
            "opts": "",             # opciones adicionales (ej. "--wbfs")
            "wwt":  "/usr/bin/wwt"
        }

    Respuesta JSON:
        { "cmd": "...", "stdout": "...", "stderr": "...", "rc": 0 }
    """
    part    = params.get('part', '').strip()
    game_id = params.get('id',   '')
    dest    = params.get('dest', '/tmp/')
    wwt     = params.get('wwt',  which('wwt') or 'wwt')
    opts    = params.get('opts', '')

    if not game_id:
        return {'error': 'Falta el parámetro id'}

    # Origen en un archivo de imagen (ruta directa, o juego de una unidad
    # montada FAT32/NTFS, donde no hay partición WBFS que wwt pueda leer):
    # se convierte con 'wit COPY'.
    game_id = game_id.strip()
    src = game_id if os.path.isfile(game_id) else None
    if not src and is_mounted_dir(part):
        wit = params.get('wit', which('wit') or 'wit')
        src = find_wii_image(wit, part, game_id)
        if not src:
            return {'error': f'No se encuentra el juego {game_id} en {wbfs_folder(part)}'}
    if src:
        wit = params.get('wit', which('wit') or 'wit')
        images = wit_list_images(wit, src)[0]
        if not images:
            return {'error': f'No es una imagen de Wii válida: {src}'}
        if os.path.exists(dest) and os.path.samefile(src, dest):
            return {'error': 'El destino es la propia imagen de origen'}
        # opts: '' → ISO, '--wbfs' → WBFS, '--split' → partida en trozos de <4 GB
        fmt = '--wbfs' if '--wbfs' in opts else '--iso'
        split = ' --split' if '--split' in opts else ''
        cmd = f'{wit} COPY {shlex.quote(src)} {fmt}{split} --overwrite --dest {shlex.quote(dest)}'
        with track_write(os.path.dirname(os.path.abspath(dest)), images[0]['size_bytes']):
            stdout, stderr, rc = run(cmd, timeout=3600)
        return {'cmd': cmd, 'stdout': stdout, 'stderr': stderr, 'rc': rc}

    flag = part_flag(part)
    cmd  = f'{wwt} EXTRACT {flag} {opts} {game_id} --dest "{dest}"'
    stdout, stderr, rc = run(cmd, timeout=600)
    return {'cmd': cmd, 'stdout': stdout, 'stderr': stderr, 'rc': rc}


def api_verify(params):
    """
    POST /api/verify

    Verifica la integridad de juegos. Tiene dos modos:

    - Si se proporciona 'src' (ruta a un archivo): usa 'wit VERIFY <src>'
      para verificar un ISO o WBFS individual.
    - Si no hay 'src' pero sí 'part': usa 'wwt VERIFY <flag>' para
      verificar todos los juegos de la partición WBFS, o solo el juego
      indicado en 'id' si se proporciona.

    Cuerpo JSON:
        {
            "src":  "/ruta/juego.iso",  # archivo a verificar (opcional)
            "part": "/dev/sdb1",        # partición (usado si no hay src)
            "id":   "RMCP01",           # limitar a un juego de la partición (opcional)
            "wit":  "/usr/bin/wit",
            "wwt":  "/usr/bin/wwt"
        }

    Respuesta JSON:
        { "cmd": "...", "stdout": "...", "stderr": "...", "rc": 0 }
    """
    part = params.get('part', '').strip()
    src  = params.get('src',  '')
    game_id = params.get('id', '').strip()
    wwt  = params.get('wwt',  which('wwt') or 'wwt')
    wit  = params.get('wit',  which('wit')  or 'wit')

    if game_id and not re.fullmatch(r'[A-Za-z0-9]{4,6}', game_id):
        return {'error': f'ID de juego no válido: {game_id}'}
    read_total = None   # bytes a leer, si se conocen (para la barra de progreso)

    if not src and is_mounted_dir(part):
        # Unidad montada: los juegos son archivos en /wbfs/, se verifican con wit
        images = wit_list_images(wit, wbfs_folder(part))[0]
        if game_id:
            images = [g for g in images if g['id'] == game_id.upper() and g['path']]
            if not images:
                return {'error': f'No se encontró el juego {game_id} en {wbfs_folder(part)}'}
            target = images[0]['path']
        else:
            target = str(wbfs_folder(part))
        read_total = sum(g['size_bytes'] for g in images)
        cmd = f'{wit} VERIFY -r {shlex.quote(target)}'
    elif src:
        # Verificar un archivo concreto con wit
        cmd = f'{wit} VERIFY "{src}"'
        try:
            read_total = os.path.getsize(src)
        except OSError:
            pass
    else:
        # Verificar toda la partición con wwt
        flag = part_flag(part)
        cmd  = f'{wwt} VERIFY {flag}' + (f' {game_id}' if game_id else '')

    stdout, stderr, rc = run(cmd, timeout=1800, read_total=read_total)
    return {'cmd': cmd, 'stdout': stdout, 'stderr': stderr, 'rc': rc}


def api_run(params):
    """
    POST /api/run

    Ejecuta un comando wwt/wit arbitrario construido por el frontend
    (usado principalmente para conversiones en lote).

    Por seguridad, solo se permiten comandos cuyo primer token termine
    en 'wwt', 'wit' o 'wdf'. Cualquier otro ejecutable es rechazado
    con un error sin ejecutar nada.

    Cuerpo JSON:
        { "cmd": "wwt ADD -p /dev/sdb1 /ruta/*.iso" }

    Respuesta JSON:
        { "cmd": "...", "stdout": "...", "stderr": "...", "rc": 0 }
        o
        { "error": "Solo se permiten comandos wwt/wit" }
    """
    cmd = params.get('cmd', '').strip()
    if not cmd:
        return {'error': 'Sin comando'}

    # Validación de seguridad: solo wwt, wit o wdf
    first = cmd.split()[0].lower()
    if not any(first.endswith(t) for t in ('wwt', 'wit', 'wdf')):
        return {'error': f'Solo se permiten comandos wwt/wit, recibido: {first}'}

    stdout, stderr, rc = run(cmd, timeout=600)
    return {'cmd': cmd, 'stdout': stdout, 'stderr': stderr, 'rc': rc}


def api_browse(params):
    """
    GET /api/browse?type=file|dir&filter=iso|gc|all
    Abre un diálogo nativo de selección de archivo o directorio usando zenity.
    """
    kind = params.get('type', 'file')
    filt = params.get('filter', '')

    if not which('zenity'):
        return {'error': 'zenity no está instalado. Instálalo con: sudo apt install zenity'}

    if kind == 'dir':
        cmd = 'zenity --file-selection --directory --title="Seleccionar directorio o unidad"'
    else:
        if filt == 'iso':
            cmd = (
                'zenity --file-selection '
                '--title="Seleccionar imagen Wii" '
                '--file-filter="Imágenes Wii (iso, wbfs, wdf)|*.iso *.ISO *.wbfs *.WBFS *.wdf *.WDF" '
                '--file-filter="Todos los archivos|*"'
            )
        elif filt in ('gc', 'gamecube'):
            cmd = (
                'zenity --file-selection '
                '--title="Seleccionar imagen GameCube" '
                '--file-filter="Imágenes GameCube (iso, gcm, ciso)|*.iso *.ISO *.gcm *.GCM *.ciso *.CISO" '
                '--file-filter="Todos los archivos|*"'
            )
        elif filt in ('all', 'any'):
            cmd = (
                'zenity --file-selection '
                '--title="Seleccionar imagen Wii o GameCube" '
                '--file-filter="Imágenes Wii y GameCube|*.iso *.ISO *.wbfs *.WBFS *.wdf *.WDF *.gcm *.GCM *.ciso *.CISO" '
                '--file-filter="Todos los archivos|*"'
            )
        else:
            cmd = 'zenity --file-selection --title="Seleccionar archivo"'

    stdout, stderr, rc = run(cmd, timeout=120)
    path = stdout.strip()
    return {'path': path if rc == 0 else ''}


# ══════════════════════════════════════════════════════════════
#  DISPOSITIVOS DE ALMACENAMIENTO Y FORMATEO
# ══════════════════════════════════════════════════════════════

def format_bytes(size):
    """Formatea bytes en formato legible (GiB, MiB, etc.)."""
    if size is None:
        return '—'
    try:
        s = float(size)
    except (ValueError, TypeError):
        return str(size)
    if s >= 1024**4:
        return f'{s / (1024**4):.2f} TiB'
    if s >= 1024**3:
        return f'{s / (1024**3):.2f} GiB'
    if s >= 1024**2:
        return f'{s / (1024**2):.1f} MiB'
    return f'{s / 1024:.0f} KiB'


def get_mount_map():
    """Devuelve un diccionario { /dev/...: /punto/de/montaje } leyendo /proc/mounts."""
    mounts = {}
    try:
        if IN_FLATPAK:
            lines = run('cat /proc/mounts')[0].splitlines()  # los montajes del anfitrión, no los del sandbox
        else:
            with open('/proc/mounts', 'r') as f:
                lines = f.readlines()
        for line in lines:
            parts = line.split()
            if len(parts) >= 2 and parts[0].startswith('/dev/'):
                mounts[parts[0]] = parts[1]
    except Exception:
        pass
    return mounts


def probe_wbfs(paths):
    """
    lsblk (libblkid) no reconoce WBFS: una partición WBFS sale sin sistema de
    archivos. Se identifica por su firma, los 4 primeros bytes ('WBFS').

    Devuelve (wbfs, denied): las rutas que son WBFS y las que no se han podido
    leer por falta de permisos (hace falta pertenecer al grupo 'disk').
    """
    if not paths:
        return set(), set()
    devs = ' '.join(shlex.quote(p) for p in paths)
    stdout, _, _ = run(
        f'for d in {devs}; do '
        'if [ ! -r "$d" ]; then echo "denied $d"; '
        'elif [ "$(head -c 4 "$d" 2>/dev/null | tr -d \'\\0\')" = WBFS ]; then echo "wbfs $d"; fi; '
        'done', timeout=20)
    found = {'wbfs': set(), 'denied': set()}
    for line in stdout.splitlines():
        kind, _, path = line.partition(' ')
        if kind in found:
            found[kind].add(path)
    return found['wbfs'], found['denied']


def get_block_devices():
    """
    Obtiene la lista de dispositivos de bloques del sistema usando lsblk en formato JSON.
    Detecta de forma segura los discos y particiones del sistema operativo para protegerlos.
    """
    cmd = 'lsblk -J -b -o NAME,PATH,SIZE,TYPE,FSTYPE,LABEL,MOUNTPOINT,RM,RO,MODEL,TRAN'
    stdout, stderr, rc = run(cmd)
    if rc != 0:
        return []

    try:
        data = json.loads(stdout)
    except Exception:
        return []

    system_mounts = {'/', '/boot', '/boot/efi', '/home', '[SWAP]', '/root', '/var', '/usr', '/etc'}

    def is_node_system(node):
        """True si el nodo de lsblk, o alguna de sus particiones, está montado en una ruta del sistema."""
        mp = node.get('mountpoint')
        if mp and (mp in system_mounts or any(mp.startswith(p) for p in ('/boot', '/home', '/var', '/usr'))):
            return True
        for ch in node.get('children', []):
            if is_node_system(ch):
                return True
        return False

    # Volúmenes sin sistema de archivos reconocido ni montaje: pueden ser WBFS
    candidates = []
    for dev in data.get('blockdevices', []):
        if is_node_system(dev):
            continue
        for node in dev.get('children') or [dev]:
            if (node.get('type') in ('disk', 'part') and not node.get('fstype') and not node.get('mountpoint')
                    and not node.get('children') and node.get('size') and node.get('path')):
                candidates.append(node['path'])
    wbfs_paths, denied_paths = probe_wbfs(candidates)

    def fstype_of(node):
        return node.get('fstype') or ('wbfs' if node.get('path') in wbfs_paths else '')

    devices = []
    for dev in data.get('blockdevices', []):
        is_sys = is_node_system(dev)
        raw_size = dev.get('size')

        parts = []
        for ch in dev.get('children', []):
            ch_is_sys = is_node_system(ch) or is_sys
            ch_size = ch.get('size')
            parts.append({
                'name': ch.get('name'),
                'path': ch.get('path'),
                'size_bytes': ch_size,
                'size_formatted': format_bytes(ch_size),
                'type': ch.get('type'),
                'fstype': fstype_of(ch),
                'unreadable': ch.get('path') in denied_paths,
                'label': ch.get('label') or '',
                'mountpoint': ch.get('mountpoint') or '',
                'removable': bool(ch.get('rm', False) or dev.get('rm', False)),
                'is_system': ch_is_sys,
                'safe_to_format': not ch_is_sys
            })

        devices.append({
            'name': dev.get('name'),
            'path': dev.get('path'),
            'size_bytes': raw_size,
            'size_formatted': format_bytes(raw_size),
            'type': dev.get('type'),
            'fstype': fstype_of(dev),
            'unreadable': dev.get('path') in denied_paths,
            'label': dev.get('label') or '',
            'mountpoint': dev.get('mountpoint') or '',
            'model': (dev.get('model') or '').strip(),
            'removable': bool(dev.get('rm', False)),
            'transport': dev.get('tran') or '',
            'is_system': is_sys,
            'safe_to_format': not is_sys,
            'partitions': parts
        })

    return devices


def api_devices(params):
    """
    GET /api/devices
    Devuelve todos los discos y particiones detectadas, identificando cuáles
    son seguras de formatear y cuáles pertenecen al sistema operativo.
    """
    devs = get_block_devices()
    return {'devices': devs}


# ══════════════════════════════════════════════════════════════
#  NINTENDONT AUTO-INSTALLER
# ══════════════════════════════════════════════════════════════

NINTENDONT_REPO   = 'FIX94/Nintendont'
NINTENDONT_BRANCH = 'master'
# FIX94/Nintendont no publica GitHub Releases: el propio README indica
# descargar estos archivos directamente de la rama principal del repo.
NINTENDONT_RAW_FILES = {
    'boot.dol': 'loader/loader.dol',
    'meta.xml': 'nintendont/meta.xml',
    'icon.png': 'nintendont/icon.png',
}


def fetch_nintendont_latest():
    """
    Construye las URLs de descarga directa (raw.githubusercontent.com) de los
    archivos de Nintendont en la rama principal del repositorio.

    Como "versión" se usa la huella (ETag) del boot.dol publicado: cambia
    solo cuando cambia el binario. No se consulta la API de GitHub, que sin
    autenticar se agota a las 60 consultas por hora (error 403).

    Devuelve:
        dict { 'boot.dol': <url>, 'meta.xml': <url>, 'icon.png': <url>, 'version': <huella corta> }
        o {'error': <mensaje>} si no se puede contactar con GitHub.
    """
    base = f'https://raw.githubusercontent.com/{NINTENDONT_REPO}/{NINTENDONT_BRANCH}'
    urls = {local_name: f'{base}/{repo_path}' for local_name, repo_path in NINTENDONT_RAW_FILES.items()}
    try:
        req = urllib.request.Request(urls['boot.dol'], method='HEAD', headers={'User-Agent': 'WiiFlowManager/1.0'})
        with urllib.request.urlopen(req, timeout=15) as resp:
            etag = (resp.headers.get('ETag') or '').strip('W/"')
    except Exception as e:
        return {'error': f'No se pudo consultar GitHub: {e}'}
    urls['version'] = etag[:7] or NINTENDONT_BRANCH
    return urls


def install_nintendont(base_path):
    """
    Descarga Nintendont desde el último release de GitHub y lo instala en:
        <base_path>/apps/Nintendont/boot.dol
        <base_path>/apps/Nintendont/meta.xml

    Parámetros:
        base_path -- Path al punto de montaje de la unidad (ej. /media/usb)

    Devuelve:
        dict con claves:
            success  -- bool
            version  -- versión descargada (ej. 'v1.0.2024-10-01')
            files    -- lista de archivos instalados
            error    -- mensaje de error (solo si success=False)
    """
    urls = fetch_nintendont_latest()
    if 'error' in urls:
        return {'success': False, 'error': urls['error']}

    nintendont_dir = Path(base_path) / 'apps' / 'Nintendont'
    nintendont_dir.mkdir(parents=True, exist_ok=True)

    installed = []
    for local_name in ('boot.dol', 'meta.xml', 'icon.png'):
        url = urls.get(local_name)
        if not url:
            continue  # meta.xml / icon.png son opcionales
        dest = nintendont_dir / local_name
        try:
            req = urllib.request.Request(
                url,
                headers={'User-Agent': 'WiiFlowManager/1.0'}
            )
            with urllib.request.urlopen(req, timeout=60) as resp, open(dest, 'wb') as f:
                while True:
                    chunk = resp.read(1024 * 256)  # 256 KB chunks
                    if not chunk:
                        break
                    f.write(chunk)
            installed.append(f'apps/Nintendont/{local_name}')
        except Exception as e:
            if local_name == 'boot.dol':
                # boot.dol es obligatorio
                return {'success': False, 'error': f'Error descargando {local_name}: {e}'}
            # meta.xml es opcional; seguir sin él

    # Crear meta.xml mínimo si no se descargó
    meta_path = nintendont_dir / 'meta.xml'
    if not meta_path.exists():
        meta_content = (
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'
            '<app version="1">\n'
            '  <name>Nintendont</name>\n'
            '  <coder>FIX94</coder>\n'
            '  <version>' + urls.get('version', '?') + '</version>\n'
            '  <release_date>2024</release_date>\n'
            '  <short_description>GameCube Loader para Wii</short_description>\n'
            '  <long_description>\n'
            '    Nintendont permite lanzar juegos de GameCube desde USB/SD en la Wii.\n'
            '    Los juegos deben estar en /games/[Titulo]/game.iso\n'
            '  </long_description>\n'
            '</app>\n'
        )
        try:
            meta_path.write_text(meta_content, encoding='utf-8')
            installed.append('apps/Nintendont/meta.xml')
        except Exception:
            pass

    _write_version_marker(nintendont_dir, urls.get('version', ''))
    return {
        'success': True,
        'name':    'Nintendont',
        'version': urls.get('version', '?'),
        'files':   installed,
        'path':    str(nintendont_dir)
    }


# Cargadores que se publican como un .zip con la estructura de la raíz de
# la unidad (apps/<cargador>/boot.dol, etc.): basta con descomprimirlo allí.
LOADERS = {
    'wiiflow': {
        'name':  'WiiFlow Lite',
        'repo':  'Fledge68/WiiFlow_Lite',
        'asset': lambda tag: f'wiiflow_{tag}.zip',                       # v5.6.2 → wiiflow_v5.6.2.zip
        'path':  'apps/wiiflow',
    },
    'usbloadergx': {
        'name':  'USB Loader GX',
        'repo':  'wiidev/usbloadergx',
        'asset': lambda tag: f"usbloadergx_{tag.split('-')[-1]}.zip",    # v4.0-r1283 → usbloadergx_r1283.zip
        'path':  'apps/usbloader_gx',
    },
}


# Aplicaciones gestionadas por "instalar / actualizar cargadores": Nintendont
# (archivos sueltos del repositorio) más los cargadores de LOADERS.
MANAGED_APPS = {
    'nintendont': {'name': 'Nintendont', 'path': 'apps/Nintendont'},
    **{key: {'name': loader['name'], 'path': loader['path']} for key, loader in LOADERS.items()},
}

# Archivo que se deja junto al boot.dol con la versión instalada (el tag del
# release o el commit), para saber después si hay una más nueva. El meta.xml
# de cada aplicación no sirve: su formato de versión no coincide con el de GitHub.
VERSION_MARKER = '.version_instalada'


def _write_version_marker(app_dir, version):
    """
    Anota la versión instalada junto al cargador (ver VERSION_MARKER). Si no se
    puede escribir, solo se pierde la detección de actualizaciones.
    """
    try:
        (Path(app_dir) / VERSION_MARKER).write_text(version, encoding='utf-8')
    except OSError:
        pass


def _latest_tag(key):
    """
    Tag del último release de un cargador de LOADERS.

    No se usa la API de GitHub (api.github.com): sin autenticar solo admite
    60 consultas por hora y, agotadas, responde 403. La página
    /releases/latest redirige a /releases/tag/<tag>, y eso no tiene ese límite.
    """
    req = urllib.request.Request(
        f"https://github.com/{LOADERS[key]['repo']}/releases/latest",
        headers={'User-Agent': 'WiiFlowManager/1.0'})
    with urllib.request.urlopen(req, timeout=15) as resp:
        final_url = resp.geturl()
    if '/releases/tag/' not in final_url:
        raise ValueError('el repositorio no tiene releases publicados')
    return urllib.parse.unquote(final_url.rsplit('/releases/tag/', 1)[1])


def loaders_status(base_path):
    """
    Estado de cada aplicación de MANAGED_APPS en la unidad:
        { clave: { name, installed, installed_version, latest_version, state, error } }
    state: 'missing' (no está), 'current' (al día), 'outdated' (hay una más
    nueva), 'unknown' (está, pero no se sabe qué versión es o no se pudo
    consultar la última).
    """
    status = {}
    for key, app in MANAGED_APPS.items():
        app_dir = Path(base_path) / app['path']
        installed = (app_dir / 'boot.dol').is_file()
        installed_version = ''
        if installed:
            try:
                installed_version = (app_dir / VERSION_MARKER).read_text(encoding='utf-8').strip()
            except OSError:
                pass
        latest, error = '', ''
        try:
            if key == 'nintendont':
                info = fetch_nintendont_latest()
                latest, error = info.get('version', ''), info.get('error', '')
                if error:
                    latest = ''
            else:
                latest = _latest_tag(key)
        except Exception as e:
            error = f'No se pudo consultar GitHub: {e}'
        if not installed:
            state = 'missing'
        elif installed_version and latest:
            state = 'current' if installed_version == latest else 'outdated'
        else:
            state = 'unknown'
        status[key] = {'name': app['name'], 'installed': installed, 'installed_version': installed_version,
                       'latest_version': latest, 'state': state, 'error': error}
    return status


def api_loaders_status(params):
    """
    POST /api/loaders/status
    Cuerpo JSON: { "path": "/media/usb" }  →  { "loaders": { ...loaders_status... } }
    """
    path = params.get('path', '').strip()
    if not path or not Path(path).is_dir():
        return {'error': f'La ruta no es una unidad montada: {path or "(vacía)"}'}
    return {'loaders': loaders_status(path)}


def api_loaders_install(params):
    """
    POST /api/loaders/install

    Instala o actualiza Nintendont, WiiFlow Lite y USB Loader GX en una unidad
    ya montada, sin formatearla. Los que ya están en su última versión se
    dejan como están.

    Cuerpo JSON: { "path": "/media/usb", "loaders": ["nintendont", "wiiflow", "usbloadergx"] }
    Respuesta JSON: { "results": { clave: { name, action, version, error } } }
        action: 'installed' | 'updated' | 'current' | 'error'
    """
    path = params.get('path', '').strip()
    if not path or not Path(path).is_dir():
        return {'error': f'La ruta no es una unidad montada: {path or "(vacía)"}'}
    keys = [k for k in (params.get('loaders') or list(MANAGED_APPS)) if k in MANAGED_APPS]
    results = {}
    try:
        set_step(2, 'Comprobando versiones instaladas…')
        status = loaders_status(path)
        for n, key in enumerate(keys):
            info = status[key]
            name = info['name']
            if info['state'] == 'current':
                results[key] = {'name': name, 'action': 'current', 'version': info['installed_version']}
                continue
            set_step(10 + int(n * 80 / len(keys)), f'Descargando e instalando {name}…')
            result = install_nintendont(path) if key == 'nintendont' else install_loader(key, path)
            if result.get('success'):
                results[key] = {'name': name, 'action': 'updated' if info['installed'] else 'installed',
                                'version': result.get('version', '')}
            else:
                results[key] = {'name': name, 'action': 'error', 'error': result.get('error', 'Error desconocido')}
        set_step(92, 'Guardando los cambios en la unidad…')
        run(f'sync -f {shlex.quote(path)}', timeout=600)
    finally:
        set_step(None)
    return {'results': results}


def install_loader(key, base_path):
    """
    Descarga el último release de un cargador de LOADERS desde GitHub y lo
    descomprime en la raíz de la unidad.

    Devuelve dict { success, name, version, path, files } o
    { success: False, name, error }.
    """
    loader = LOADERS[key]
    name = loader['name']
    try:
        tag = _latest_tag(key)
        url = f"https://github.com/{loader['repo']}/releases/download/{tag}/{loader['asset'](tag)}"
        req = urllib.request.Request(url, headers={'User-Agent': 'WiiFlowManager/1.0'})
        with urllib.request.urlopen(req, timeout=120) as resp:
            archive = zipfile.ZipFile(io.BytesIO(resp.read()))

        base = Path(base_path).resolve()
        for member in archive.namelist():
            # No permitir que una ruta del zip escriba fuera de la unidad
            if not (base / member).resolve().is_relative_to(base):
                return {'success': False, 'name': name, 'error': f'Ruta no válida en el paquete: {member}'}
        archive.extractall(base)
    except Exception as e:
        return {'success': False, 'name': name, 'error': f'Error descargando {name}: {e}'}

    if not (base / loader['path'] / 'boot.dol').is_file():
        return {'success': False, 'name': name, 'error': f"El paquete no contiene {loader['path']}/boot.dol"}
    _write_version_marker(base / loader['path'], tag)
    top_level = sorted({m.split('/')[0] for m in archive.namelist() if m.split('/')[0]})
    return {
        'success': True,
        'name':    name,
        'version': tag,
        'path':    str(base / loader['path']),
        'files':   [f'{d}/' for d in top_level],
    }


def api_nintendont_install(params):
    """
    POST /api/nintendont/install
    Instala Nintendont en una unidad ya montada.
    Cuerpo JSON: { "mount_path": "/media/usb" }
    """
    mount_path = params.get('mount_path', '').strip()
    if not mount_path:
        return {'error': 'Falta mount_path'}
    if not Path(mount_path).is_dir():
        return {'error': f'La ruta no existe o no está montada: {mount_path}'}
    return install_nintendont(mount_path)


def api_format(params):
    """POST /api/format — ver _api_format. Envoltorio que limpia el indicador de progreso al acabar."""
    try:
        return _api_format(params)
    finally:
        set_step(None)


def _api_format(params):
    """
    POST /api/format
    Formatea un dispositivo o partición para usarlo con Wii y GameCube.
    Opciones:
      - device: '/dev/sdX1' (o '/dev/sdX')
      - fs_type: 'fat32' (Recomendado para Wii + GameCube / WiiFlow / Nintendont)
                 'wbfs'  (Partición clásica WBFS con wwt)
      - label: etiqueta del volumen (ej. 'WII')
      - create_folders: bool (crea /wbfs, /games, /apps, /wiiflow en FAT32)
      - confirm: bool (obligatorio true)
    """
    device = params.get('device', '').strip()
    fs_type = params.get('fs_type', 'fat32').lower()
    label = params.get('label', 'WII').strip() or 'WII'
    create_folders = bool(params.get('create_folders', True))
    install_nint = bool(params.get('install_nintendont', False))
    # Cargadores extra a instalar (claves de LOADERS): install_wiiflow, install_usbloadergx
    install_loaders = [key for key in LOADERS if params.get(f'install_{key}')]
    confirm = bool(params.get('confirm', False))

    if not confirm:
        return {'error': 'Se requiere confirmación explícita para formatear.'}

    if not device or not device.startswith('/dev/'):
        return {'error': 'Dispositivo inválido. Debe comenzar por /dev/'}

    # Comprobación de seguridad estricta: NO permitir dispositivos del sistema
    all_devs = get_block_devices()
    target_node = None
    for d in all_devs:
        if d['path'] == device:
            target_node = d
            break
        for p in d.get('partitions', []):
            if p['path'] == device:
                target_node = p
                break

    if target_node and target_node.get('is_system'):
        return {
            'error': f'SEGURIDAD: El dispositivo {device} pertenece al sistema operativo y está bloqueado para prevenir pérdida de datos.'
        }

    # Desmontar si está montado
    set_step(5, 'Desmontando la unidad…')
    run(f'udisksctl unmount -b "{device}" 2>/dev/null || umount "{device}" 2>/dev/null')

    output_log = []
    created_dirs = []
    nintendont_result = None
    loader_results = {}

    if fs_type == 'fat32':
        mkfs_bin = which('mkfs.vfat') or which('mkfs.fat') or 'mkfs.vfat'
        cmd = f'{mkfs_bin} -F 32 -s 64 -n "{label}" "{device}"'
        set_step(15, 'Formateando como FAT32…')
        stdout, stderr, rc = run(cmd)
        output_log.append(f'$ {cmd}\n{stdout}\n{stderr}')

        if rc != 0 and ('permission' in (stderr + stdout).lower() or 'denied' in (stderr + stdout).lower()):
            if which('pkexec'):
                cmd_elevated = f'pkexec {cmd}'
                stdout, stderr, rc = run(cmd_elevated, timeout=120)
                output_log.append(f'$ {cmd_elevated}\n{stdout}\n{stderr}')

        if rc != 0:
            return {
                'success': False,
                'cmd': cmd,
                'stdout': stdout,
                'stderr': stderr,
                'rc': rc,
                'error': f'Error al formatear {device}: {stderr or stdout}'
            }

        # Crear estructura de carpetas si se solicitó
        if create_folders:
            set_step(50, 'Montando la unidad…')
            m_out, m_err, _ = run(f'udisksctl mount -b "{device}"')
            output_log.append(f'$ udisksctl mount -b {device}\n{m_out}\n{m_err}')

            mount_path = None
            m = re.search(r'at\s+(/\S+)', m_out)
            if m:
                mount_path = m.group(1).rstrip('.')
            else:
                mount_path = get_mount_map().get(device)

            if mount_path and Path(mount_path).exists():
                base = Path(mount_path)
                set_step(60, 'Creando carpetas…')
                folders = [
                    'wbfs',
                    'games',
                    'apps',
                    'wiiflow',
                    'wiiflow/boxcovers',
                    'wiiflow/covers',
                    'wiiflow/fanart',
                    'wiiflow/plugins',
                    'wiiflow/trailers'
                ]
                for fld in folders:
                    target_dir = base / fld
                    target_dir.mkdir(parents=True, exist_ok=True)
                    created_dirs.append(fld)

                readme_path = base / 'LEEME_WIIFLOW.txt'
                readme_content = (
                    "════════════════════════════════════════════════════════════════\n"
                    " WiiFlow & Nintendont — Estructura de almacenamiento preparada\n"
                    "════════════════════════════════════════════════════════════════\n\n"
                    "1. JUEGOS DE WII:\n"
                    "   - Colócalos en la carpeta /wbfs/\n"
                    "   - Formato: /wbfs/Nombre del Juego [GAMEID]/GAMEID.wbfs\n\n"
                    "2. JUEGOS DE GAMECUBE (Nintendont / WiiFlow):\n"
                    "   - Colócalos en la carpeta /games/\n"
                    "   - Formato: /games/Nombre del Juego [GAMEID]/game.iso\n"
                    "   - Disco 2 (si aplica): /games/Nombre del Juego [GAMEID]/disc2.iso\n\n"
                    "3. APLICACIONES HOMEBREW:\n"
                    "   - Coloca loaders (WiiFlow, USB Loader GX, Nintendont) en /apps/\n\n"
                    "Preparado con WiiFlow Manager.\n"
                )
                try:
                    readme_path.write_text(readme_content, encoding='utf-8')
                    created_dirs.append('LEEME_WIIFLOW.txt')
                except Exception:
                    pass

                # ── Instalar Nintendont si se solicitó ──────────────────
                if install_nint:
                    set_step(70, 'Descargando e instalando Nintendont…')
                    nintendont_result = install_nintendont(mount_path)
                    if nintendont_result.get('success'):
                        installed_files = nintendont_result.get('files', [])
                        created_dirs.extend(installed_files)
                        output_log.append(
                            f'✓ Nintendont {nintendont_result.get("version","")} instalado en {nintendont_result.get("path","")}\n'
                            + '  ' + ', '.join(installed_files)
                        )
                    else:
                        output_log.append(
                            f'⚠ Nintendont no se pudo instalar: {nintendont_result.get("error","error desconocido")}'
                        )

                for n, key in enumerate(install_loaders):
                    set_step(76 + n * 7, f"Descargando e instalando {LOADERS[key]['name']}…")
                    result = install_loader(key, mount_path)
                    loader_results[key] = result
                    if result.get('success'):
                        output_log.append(f"✓ {result['name']} {result['version']} instalado en {result['path']}")
                    else:
                        output_log.append(f"⚠ {result['name']} no se pudo instalar: {result.get('error', 'error desconocido')}")

                set_step(90, 'Guardando los cambios en la unidad…')
                run(f'sync -f {shlex.quote(mount_path)}', timeout=600)

        # Dejar la unidad montada y lista para usar (la interfaz la recarga)
        mount_path = get_mount_map().get(device)
        if not mount_path:
            run(f'udisksctl mount -b "{device}"')
            mount_path = get_mount_map().get(device, '')

        return {
            'success': True,
            'cmd': cmd,
            'fs_type': 'FAT32',
            'device': device,
            'mount_path': mount_path,
            'log': '\n'.join(output_log),
            'folders_created': created_dirs,
            'nintendont': nintendont_result,
            'loaders': loader_results,
        }

    elif fs_type == 'wbfs':
        wwt = which('wwt') or 'wwt'
        cmd = f'{wwt} FORMAT "{device}" --force'
        set_step(30, 'Formateando como WBFS…')
        stdout, stderr, rc = run(cmd)
        if rc != 0 and ('permission' in (stderr + stdout).lower() or 'denied' in (stderr + stdout).lower()):
            if which('pkexec'):
                cmd = f'pkexec {cmd}'
                stdout, stderr, rc = run(cmd)

        if rc != 0:
            return {
                'success': False,
                'cmd': cmd,
                'stdout': stdout,
                'stderr': stderr,
                'rc': rc,
                'error': f'Error al formatear partición WBFS {device}: {stderr or stdout}'
            }

        return {
            'success': True,
            'cmd': cmd,
            'fs_type': 'WBFS',
            'device': device,
            'stdout': stdout
        }

    else:
        return {'error': f'Tipo de sistema de archivos desconocido: {fs_type}'}


# ══════════════════════════════════════════════════════════════
#  UTILIDADES PARA GAMECUBE (Nintendont & WiiFlow)
# ══════════════════════════════════════════════════════════════

def read_disc_header(file_path):
    """
    Lee la cabecera del disco (los primeros 0x60 bytes del disco original)
    de una imagen Wii o GameCube, mirando dentro del contenedor si hace falta:
      - ISO / GCM: está al principio del archivo.
      - CISO: detrás de la cabecera del contenedor (0x8000 bytes).
      - WBFS (archivo): copia de la cabecera en el segundo sector del archivo.

    Devuelve { 'id', 'disc' (1, 2…), 'platform' ('gc' | 'wii'), 'title' } o
    None si no se reconoce (formatos comprimidos como WDF/WIA/GCZ, u otro tipo de archivo).
    """
    try:
        with open(file_path, 'rb') as f:
            start = f.read(0x10)
            if start[:4] == b'CISO':
                offset = 0x8000
            elif start[:4] == b'WBFS':
                offset = 1 << start[8]      # tamaño de sector del contenedor (normalmente 512)
            else:
                offset = 0
            f.seek(offset)
            head = f.read(0x60)
    except (OSError, IndexError):
        return None
    if len(head) < 0x60:
        return None
    wii_magic, gc_magic = struct.unpack('>II', head[0x18:0x20])
    if wii_magic == 0x5d1c9ea3:
        platform = 'wii'
    elif gc_magic == 0xc2339f3d:
        platform = 'gc'
    else:
        return None
    return {
        'id':       head[0:6].decode('latin1', errors='replace').strip().upper(),
        'disc':     head[6] + 1,
        'platform': platform,
        'title':    head[0x20:0x60].split(b'\x00')[0].decode('latin1', errors='replace').strip(),
    }


def inspect_wii_file(file_path):
    """
    Inspecciona una imagen de Wii (ISO, WBFS, WDF…) antes de añadirla.
    Devuelve { valid, id, title, disc (o None si no se puede saber), region, size_gb, platform }.
    """
    if is_dolphin_image(file_path) and Path(file_path).is_file():
        meta = inspect_dolphin_image(file_path)
        if meta.get('valid') and meta['platform'] != 'wii':
            return {'valid': False, 'error': 'Es una imagen de GameCube: añádela con «Añadir GameCube».'}
        return meta
    wit = which('wit') or 'wit'
    found = wit_list_images(wit, file_path)[0] if Path(file_path).is_file() else []
    if not found:
        header = read_disc_header(file_path)
        if header and header['platform'] == 'gc':
            return {'valid': False, 'error': 'Es una imagen de GameCube: añádela con «Añadir GameCube».'}
        return {'valid': False, 'error': 'No es una imagen de Wii válida.'}
    game = found[0]
    header = read_disc_header(file_path)
    return {
        'valid': True,
        'id': game['id'],
        'title': game['title'],
        'disc': header['disc'] if header else None,
        'region': game['region'],
        'size_gb': game['size'],
        'platform': 'wii',
    }


def inspect_gamecube_file(file_path):
    """
    Inspecciona un archivo de imagen de GameCube (.iso, .gcm, .ciso).
    Lee la cabecera estándar de Nintendo GameCube (primeros 0x60 bytes).
    Devuelve los metadatos completos del juego.
    """
    p = Path(file_path)
    if not p.is_file():
        return {'valid': False, 'error': f'No existe el archivo {file_path}'}
    if is_dolphin_image(p):
        meta = inspect_dolphin_image(file_path)
        if meta.get('valid') and meta['platform'] != 'gc':
            return {'valid': False, 'error': 'Es una imagen de Wii: añádela con «Añadir Wii».'}
        return meta
    disc_header = read_disc_header(file_path)
    if disc_header and disc_header['platform'] == 'wii':
        return {'valid': False, 'error': 'Es una imagen de Wii: añádela con «Añadir Wii».'}

    file_size_gb = round(p.stat().st_size / (1024**3), 2)
    head = b''
    try:
        with open(file_path, 'rb') as f:
            head = f.read(0x60)
    except Exception as e:
        return {'valid': False, 'error': f'Error de lectura: {e}'}

    if len(head) < 0x60:
        return {'valid': False, 'error': 'El archivo es demasiado pequeño para ser una imagen GameCube válida.'}

    # Descodificar cabecera GameCube:
    # 0x00-0x04: 4 letras código del juego (ej. GALE)
    # 0x04-0x06: 2 letras código de fabricante (ej. 01 = Nintendo)
    # 0x06: disco (0 = Disco 1, 1 = Disco 2)
    # 0x1C-0x20: magic 0xc2339f3d
    # 0x20-0x60: título del disco
    id4 = head[0:4].decode('latin1', errors='replace')
    maker = head[4:6].decode('latin1', errors='replace')
    game_id = (id4 + maker).strip().upper()
    disc_num = head[6] + 1

    magic = 0
    try:
        magic = struct.unpack('>I', head[0x1C:0x20])[0]
    except Exception:
        pass

    raw_title = head[0x20:0x60].split(b'\x00')[0]
    title = raw_title.decode('latin1', errors='replace').strip()

    is_gc_magic = (magic == 0xc2339f3d)

    # Si el magic word no coincide, la cabecera no es la de un disco GC real:
    # son los primeros bytes de un contenedor (.ciso, .gcz…). En un CISO la
    # cabecera real del disco se puede leer igualmente, detrás de la del contenedor.
    if not is_gc_magic:
        inner = read_disc_header(file_path)
        if inner and inner['platform'] == 'gc':
            game_id, title, disc_num, is_gc_magic = inner['id'], inner['title'], inner['disc'], True
        else:
            # El id/título leídos son basura binaria, no texto, y el nº de disco tampoco es fiable
            game_id = ''
            title = ''
            disc_num = 1

    # Si el magic o título están vacíos (algunos CISOs o dumps modificados), consultar wit
    if (not is_gc_magic or not title or not game_id) and which('wit'):
        out, _, rc = run(f'wit ID6 "{file_path}"')
        if rc == 0 and out.strip():
            game_id = out.strip().splitlines()[0].strip()
        out_dump, _, _ = run(f'wit DUMP "{file_path}"')
        m = re.search(r'Disc title:\s*(.+)', out_dump)
        if m:
            title = m.group(1).strip()

    # Si aún no tenemos título legible, usar el nombre del archivo sin extensión
    if not title:
        title = p.stem.replace('_', ' ')

    region = region_from_id(game_id)

    return {
        'valid': bool(game_id and len(game_id) >= 4),
        'id': game_id,
        'title': title,
        'disc': disc_num,
        'region': region,
        'size_gb': file_size_gb,
        'magic_ok': is_gc_magic,
        'platform': 'gc',
        'file_path': str(p.resolve())
    }


def find_games_dir(root_path):
    """
    Encuentra el directorio 'games' a partir de una ruta dada
    (que puede ser el USB montado, o la propia carpeta games).
    """
    if not root_path or not root_path.strip():
        return None
    p = Path(root_path.strip())
    if p.name.lower() == 'games' and p.is_dir():
        return p
    if (p / 'games').is_dir():
        return p / 'games'
    if (p / 'GAMES').is_dir():
        return p / 'GAMES'
    if p.is_dir():
        return p / 'games'
    mp = get_mount_map().get(str(p))
    if mp:
        mp_path = Path(mp)
        if (mp_path / 'games').is_dir():
            return mp_path / 'games'
        return mp_path / 'games'
    return None


def api_gc_inspect(params):
    """
    GET /api/gamecube/inspect?path=/ruta/al/juego.iso
    """
    path = params.get('path', '').strip()
    if not path:
        return {'error': 'Falta el parámetro path'}
    return inspect_gamecube_file(path)


def api_gc_list(params):
    """
    GET /api/gamecube/list?path=<ruta_usb_o_games>
    Lista los juegos de GameCube organizados en la carpeta /games/.
    """
    path = params.get('path', '').strip()
    games_dir = find_games_dir(path)

    if not games_dir or not games_dir.is_dir():
        return {
            'games': [],
            'games_dir': str(games_dir) if games_dir else '',
            'message': 'No se encontró la carpeta /games/ en la ruta indicada.'
        }

    games = []
    for item in sorted(games_dir.iterdir()):
        if not item.is_dir():
            continue

        d1 = None
        d2 = None
        for cand in item.iterdir():
            if not cand.is_file():
                continue
            name_lower = cand.name.lower()
            if name_lower in ('game.iso', 'game.gcm', 'game.ciso'):
                d1 = cand
            elif name_lower in ('disc2.iso', 'disc2.gcm', 'disc2.ciso'):
                d2 = cand
            elif name_lower.endswith(('.iso', '.gcm', '.ciso')) and not d1:
                d1 = cand

        if not d1 and not d2:
            continue

        primary = d1 or d2
        info = inspect_gamecube_file(str(primary))

        m_id = re.search(r'\[([A-Z0-9]{4,6})\]', item.name)
        if m_id and (not info.get('id') or info.get('id') == '?'):
            info['id'] = m_id.group(1)
            info['region'] = region_from_id(info['id'])

        clean_title = info.get('title')
        if not clean_title or clean_title == primary.stem:
            clean_title = re.sub(r'\[[A-Z0-9]{4,6}\]', '', item.name).strip()

        total_size = 0.0
        if d1: total_size += d1.stat().st_size / (1024**3)
        if d2: total_size += d2.stat().st_size / (1024**3)

        games.append({
            'id': info.get('id', 'GC0001'),
            'title': clean_title or item.name,
            'size': round(total_size, 2),
            'fmt': (d1 or d2).suffix.lstrip('.').lower(),
            'region': info.get('region', 'UNK'),
            'year': 0,
            'platform': 'gc',
            'folder': str(item.resolve()),
            'disc1_present': bool(d1),
            'disc2_present': bool(d2),
            'disc_count': (1 if d1 else 0) + (1 if d2 else 0),
            'path': str(primary.resolve())
        })

    return {
        'games': games,
        'games_dir': str(games_dir.resolve()),
        'total': len(games)
    }


def api_gc_add(params):
    """
    POST /api/gamecube/add
    Copia e instala una imagen de GameCube en la estructura de WiiFlow/Nintendont:
      <destino>/games/<Título> [<ID>]/game.iso (o disc2.iso)
    """
    src = params.get('src', '').strip()
    if is_dolphin_image(src) and os.path.isfile(src):
        # Nintendont no lee RVZ/GCZ: se copia convertida a ISO. El número de
        # disco solo se puede leer de la imagen ya convertida.
        with dolphin_to_iso(src) as (iso, error):
            if error:
                return {'error': error}
            meta = inspect_gamecube_file(iso)
            return _api_gc_add({**params, 'src': iso, 'disc': meta.get('disc') or 1})
    return _api_gc_add(params)


def _api_gc_add(params):
    """Implementación de api_gc_add para una imagen ISO/GCM/CISO (las RVZ/GCZ llegan ya convertidas a ISO)."""
    src = params.get('src', '').strip()
    dest = params.get('dest', '').strip()
    disc = int(params.get('disc', 1))
    custom_title = params.get('title', '').strip()

    if not src:
        return {'error': 'Falta el archivo de origen (src).'}
    if not dest:
        return {'error': 'Falta el directorio de destino (dest).'}

    src_path = Path(src)
    if not src_path.is_file():
        return {'error': f'El archivo de origen no existe: {src}'}

    meta = inspect_gamecube_file(src)
    game_id = params.get('id', '').strip() or meta.get('id') or 'GC0001'
    title = custom_title or meta.get('title') or src_path.stem

    safe_title = re.sub(r'[\\/*?:"<>|]', '', title).strip()
    folder_name = f'{safe_title} [{game_id}]'

    games_dir = find_games_dir(dest)
    if not games_dir:
        games_dir = Path(dest) / 'games'

    target_folder = games_dir / folder_name
    # Los dos discos de un juego multidisco deben ir en la misma carpeta: si
    # ya hay una de este juego (del otro disco), se usa esa aunque el título difiera.
    if not target_folder.exists() and games_dir.is_dir():
        existing = [d for d in games_dir.iterdir() if d.is_dir() and f'[{game_id}]' in d.name]
        if existing:
            target_folder = existing[0]

    filename = 'game.iso' if disc == 1 else 'disc2.iso'
    ext = src_path.suffix.lower()
    if ext in ('.gcm', '.ciso'):
        filename = f'game{ext}' if disc == 1 else f'disc2{ext}'

    target_file = target_folder / filename

    try:
        target_folder.mkdir(parents=True, exist_ok=True)
        with track_write(target_file.parent, src_path.stat().st_size):
            with open(src_path, 'rb') as fsrc, open(target_file, 'wb') as fdst:
                shutil.copyfileobj(fsrc, fdst, length=16 * 1024 * 1024)
    except Exception as e:
        return {'error': f'Error al copiar el archivo: {e}'}

    return {
        'success': True,
        'title': title,
        'id': game_id,
        'disc': disc,
        'dest_file': str(target_file.resolve()),
        'folder': str(target_folder.resolve())
    }


def api_gc_remove(params):
    """
    POST /api/gamecube/remove
    Elimina la carpeta de un juego de GameCube de /games/.
    """
    folder = params.get('folder', '').strip()
    if not folder:
        return {'error': 'Falta el parámetro folder'}

    p = Path(folder)
    if not p.is_dir():
        return {'error': f'El directorio no existe: {folder}'}

    parent_name = p.parent.name.lower()
    if parent_name != 'games' and 'games' not in [part.lower() for part in p.parts]:
        return {'error': f'Seguridad: La carpeta {folder} no parece ser un juego dentro de /games/'}

    try:
        shutil.rmtree(p)
        return {'success': True, 'removed': folder}
    except Exception as e:
        return {'error': f'Error al eliminar {folder}: {e}'}


# Tabla de enrutamiento: ruta URL → función handler
ROUTES = {
    '/api/status':              api_status,
    '/api/list':                api_list,
    '/api/add':                 api_add,
    '/api/remove':              api_remove,
    '/api/extract':             api_extract,
    '/api/verify':              api_verify,
    '/api/progress':            api_progress,
    '/api/run':                 api_run,
    '/api/browse':              api_browse,
    '/api/devices':             api_devices,
    '/api/format':              api_format,
    '/api/nintendont/install':  api_nintendont_install,
    '/api/loaders/status':      api_loaders_status,
    '/api/loaders/install':     api_loaders_install,
    '/api/gamecube/inspect':    api_gc_inspect,
    '/api/gamecube/list':       api_gc_list,
    '/api/gamecube/add':        api_gc_add,
    '/api/gamecube/remove':     api_gc_remove,
}


# ══════════════════════════════════════════════════════════════
#  SERVIDOR HTTP
# ══════════════════════════════════════════════════════════════

class Handler(http.server.BaseHTTPRequestHandler):
    """
    Manejador HTTP para el servidor local de WiiFlow Manager.

    Gestiona tres tipos de peticiones:
        - GET  /api/*  → llamada a la API (parámetros en query string)
        - POST /api/*  → llamada a la API (parámetros en body JSON)
        - GET  /       → sirve el archivo wii-manager.html
        - OPTIONS *    → responde headers CORS para peticiones preflight
    """

    def log_message(self, fmt, *args):
        """Sobreescribe el log por defecto para formato más limpio."""
        print(f'  {self.address_string()} {fmt % args}')

    def send_json(self, data, status=200):
        """
        Serializa 'data' a JSON y lo envía como respuesta HTTP.
        Incluye headers CORS para permitir peticiones desde el frontend.
        """
        body = json.dumps(data, ensure_ascii=False).encode()
        self.send_response(status)
        self.send_header('Content-Type', 'application/json; charset=utf-8')
        self.send_header('Content-Length', str(len(body)))
        self.send_header('Access-Control-Allow-Origin', '*')
        self.end_headers()
        self.wfile.write(body)

    def do_OPTIONS(self):
        """
        Responde a peticiones OPTIONS (preflight CORS).
        Necesario para que el navegador permita las llamadas fetch() al API.
        """
        self.send_response(204)
        self.send_header('Access-Control-Allow-Origin', '*')
        self.send_header('Access-Control-Allow-Methods', 'GET, POST, OPTIONS')
        self.send_header('Access-Control-Allow-Headers', 'Content-Type')
        self.end_headers()

    def call_route(self, path, params):
        """
        Ejecuta el handler de ROUTES[path] protegido ante excepciones:
        si el handler falla, devuelve un JSON de error en vez de dejar
        que la excepción cierre la conexión (lo que el navegador ve
        como "Error de conexión" sin ningún detalle del fallo real).
        """
        try:
            result = ROUTES[path](params)
        except Exception as e:
            self.send_json({'error': f'Error interno: {e}'}, 500)
            return
        self.send_json(result)

    def do_GET(self):
        """
        Maneja peticiones GET.
        - Rutas /api/* → ejecuta el handler correspondiente de ROUTES.
        - Ruta raíz /  → sirve wii-manager.html.
        - Resto        → 404.
        """
        parsed = urllib.parse.urlparse(self.path)
        path   = parsed.path
        params = dict(urllib.parse.parse_qsl(parsed.query))

        if path in ROUTES:
            self.call_route(path, params)
            return

        # Servir el frontend HTML
        if path in ('/', '/index.html', '/wii-manager.html'):
            if HTML_FILE.exists():
                body = HTML_FILE.read_bytes()
                self.send_response(200)
                self.send_header('Content-Type', 'text/html; charset=utf-8')
                self.send_header('Content-Length', str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            else:
                self.send_json(
                    {'error': f'No se encuentra {HTML_FILE.name}. '
                              f'Asegúrate de que está en el mismo directorio.'},
                    404
                )
            return

        self.send_json({'error': 'Not found'}, 404)

    def do_POST(self):
        """
        Maneja peticiones POST.
        Lee el body como JSON (o como form-urlencoded como fallback)
        y llama al handler correspondiente de ROUTES.
        """
        parsed = urllib.parse.urlparse(self.path)
        path   = parsed.path
        length = int(self.headers.get('Content-Length', 0))
        body   = self.rfile.read(length) if length else b'{}'

        # Intentar parsear como JSON, fallback a form-urlencoded
        try:
            params = json.loads(body)
        except Exception:
            params = dict(urllib.parse.parse_qsl(body.decode()))

        if path in ROUTES:
            self.call_route(path, params)
            return

        self.send_json({'error': 'Not found'}, 404)


# ══════════════════════════════════════════════════════════════
#  PUNTO DE ENTRADA
# ══════════════════════════════════════════════════════════════

def main():
    """
    Arranca el servidor HTTP y muestra información de diagnóstico:
    - URL de acceso
    - Rutas detectadas de wit y wwt
    - Advertencia si el HTML no se encuentra en el directorio
    """
    print('=' * 54)
    print('  WiiFlow Manager — Backend')
    print(f'  http://localhost:{PORT}')
    print('  Ctrl+C para detener')
    print('=' * 54)

    wwt = which('wwt')
    wit = which('wit')
    print(f'  wit : {wit  or "⚠ NO ENCONTRADO"}')
    print(f'  wwt : {wwt or "⚠ NO ENCONTRADO"}')
    print()

    if not HTML_FILE.exists():
        print(f'  ⚠  No se encuentra {HTML_FILE}')
        print(f'     Asegúrate de que wii-manager.html está en el mismo directorio.')
        print()

    # Escuchar solo en localhost por seguridad.
    # ThreadingHTTPServer (no HTTPServer a secas) para que una petición lenta
    # (p.ej. detectar unidades con lsblk) no deje bloqueadas al resto —
    # HTTPServer es monohilo y serializa todas las peticiones una a una.
    server = http.server.ThreadingHTTPServer(('127.0.0.1', PORT), Handler)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print('\n  Servidor detenido.')
        server.server_close()


if __name__ == '__main__':
    main()