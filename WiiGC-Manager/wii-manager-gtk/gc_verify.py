"""
Verificación de imágenes de GameCube (ISO/GCM completas). Los discos de GameCube no llevan sumas
de comprobación propias (los de Wii sí, y por eso wit/wwt solo verifican
Wii), así que se calcula el SHA1 del disco y se compara con los volcados
conocidos (Redump) que publica GameTDB.

verify() es bloqueante (lee la imagen entera): llamar vía backend.run_async.
"""
import hashlib
import struct
from pathlib import Path

import gametdb
from backend import core

GC_DISC_SIZE = 1459978240   # tamaño de un disco GameCube completo
_GC_MAGIC = 0xc2339f3d      # en el offset 0x1C de la cabecera del disco
_CISO_HEADER = 0x8000       # cabecera de un CISO; después va el primer bloque del disco
_CHUNK = 4 * 1024 * 1024


def is_gamecube_image(path):
    """True si el archivo es una imagen de GameCube (ISO/GCM normal o CISO)."""
    try:
        with open(path, 'rb') as f:
            head = f.read(0x20)
            if head[:4] == b'CISO':
                f.seek(_CISO_HEADER)   # primer bloque de datos = cabecera del disco
                head = f.read(0x20)
        return len(head) == 0x20 and struct.unpack('>I', head[0x1C:0x20])[0] == _GC_MAGIC
    except OSError:
        return False


def verify(path, label=''):
    """
    Devuelve {'ok': True/False/None, 'message': str}. ok=None significa que
    no se ha podido comprobar (GameTDB no tiene sumas de ese juego).
    """
    name = label or Path(path).name
    gametdb.load()
    game_id = (core.inspect_gamecube_file(path).get('id') or '').upper()
    roms = gametdb.roms(game_id)

    if _starts_with(path, b'CISO'):
        # Un CISO no guarda el relleno original del disco (lo sustituye por
        # huecos), así que su contenido nunca coincide con el volcado completo.
        return {'ok': None, 'message': f'{name}: es una imagen CISO (compactada); no se puede comparar con el '
                                       'volcado original. Solo se pueden verificar ISO/GCM completas.'}

    total = Path(path).stat().st_size
    sha1 = hashlib.sha1()
    done = 0
    try:
        with open(path, 'rb') as f:
            while chunk := f.read(_CHUNK):
                sha1.update(chunk)
                done += len(chunk)
                core.set_step(min(99, int(done * 100 / total)), f'Calculando SHA1 de {name}')
    finally:
        core.set_step(None)
    digest = sha1.hexdigest()

    if not roms:
        return {'ok': None, 'message': f'{name}: GameTDB no tiene sumas de comprobación de {game_id or "este juego"}; '
                                       f'no se puede comprobar (SHA1 {digest})'}
    for version, _size, known in roms:
        if known == digest:
            return {'ok': True, 'message': f'{name}: coincide con el volcado conocido'
                                           + (f' ({version})' if version else '')}
    if done != GC_DISC_SIZE:
        hint = f' El tamaño ({done} bytes) no es el de un disco GameCube completo ({GC_DISC_SIZE}).'
    else:
        hint = ' Puede estar dañada o ser un volcado modificado (recortado/parcheado).'
    return {'ok': False, 'message': f'{name}: no coincide con ningún volcado conocido de {game_id} '
                                    f'(SHA1 {digest}).{hint}'}


def _starts_with(path, magic):
    """True si el archivo empieza por esos bytes (la firma del formato)."""
    with open(path, 'rb') as f:
        return f.read(len(magic)) == magic
