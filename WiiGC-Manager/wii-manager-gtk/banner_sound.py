"""
Sonido del banner de un juego de Wii: el que suena en el menú de la consola al
elegir el disco. Está en opening.bnr (raíz del disco), un archivo U8 que lleva
dentro meta/sound.bin: audio BNS (ADPCM de Nintendo), WAV o AIFF, a veces
comprimido con LZ77. Se convierte a WAV y se guarda en
~/.cache/wii-manager-gtk/banners/<ID>.wav para no repetir el trabajo.

Los juegos de GameCube no tienen sonido de banner.
"""
import shlex
import shutil
import struct
from pathlib import Path

from backend import core

_CACHE_DIR = Path.home() / '.cache' / 'wii-manager-gtk' / 'banners'
_MAX_SECONDS = 30           # por si algún banner trae una pista larga
_U8_MAGIC = b'\x55\xaa\x38\x2d'


def wav_path(game, part):
    """
    Ruta del WAV con el sonido del banner del juego (dict de la Videoteca) de
    la unidad part, extrayéndolo si aún no está en caché (bloqueante: llamar
    vía run_async). None si el juego no tiene sonido o no se puede leer.
    """
    game_id = game.get('id') or ''
    if game.get('platform') == 'gc' or not game_id.isalnum():
        return None
    target = _CACHE_DIR / f'{game_id}.wav'
    missing = _CACHE_DIR / f'{game_id}.none'   # ya se intentó y no tiene sonido
    if target.is_file():
        return str(target)
    if missing.exists():
        return None
    _CACHE_DIR.mkdir(parents=True, exist_ok=True)
    wav = None
    try:
        banner = _read_banner(game, part)
    except OSError:
        return None  # unidad retirada u ocupada: no dejar marca, se reintentará
    try:
        wav = to_wav(banner)
    except (ValueError, IndexError, struct.error):
        pass
    if not wav:
        missing.touch()
        return None
    target.write_bytes(wav)
    return str(target)


def _read_banner(game, part):
    """Contenido de opening.bnr, sacado del juego con wit. Lanza OSError si no se puede."""
    wit = core.which('wit') or 'wit'
    # En una partición WBFS el juego se elige con <partición>/<ID6>
    path = game.get('path') or ''
    source = path if path and not core.is_device_path(path) else f"{part}/{game['id']}"
    # wit se ejecuta fuera del Flatpak: la carpeta temporal tiene que verla también el sistema
    tmp = _CACHE_DIR / f"tmp-{game['id']}"
    shutil.rmtree(tmp, ignore_errors=True)
    try:
        _stdout, stderr, rc = core.run(
            f'{wit} EXTRACT {shlex.quote(source)} --psel data --files +/files/opening.bnr '
            f'--dest {shlex.quote(str(tmp))}', timeout=60)
        banner = tmp / 'files' / 'opening.bnr'
        if rc != 0 or not banner.is_file():
            reason = stderr.strip().splitlines()
            raise OSError(reason[-1].strip() if reason else 'wit no pudo leer el banner')
        return banner.read_bytes()
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def to_wav(banner):
    """WAV (bytes) con el sonido de un opening.bnr, o None si no trae sonido. Lanza ValueError si no se entiende."""
    start = banner.find(_U8_MAGIC)
    if start < 0:
        raise ValueError('opening.bnr sin archivo U8')
    sound = _u8_file(banner[start:], 'sound.bin')
    if not sound:
        return None
    if sound[:4] == b'IMD5':
        sound = sound[0x20:]
    if sound[:4] == b'LZ77':
        sound = _lz77(sound[4:])
    if sound[:4] == b'RIFF':
        return bytes(sound)
    if sound[:4] == b'BNS ':
        return _bns_to_wav(sound)
    if sound[:4] == b'FORM':
        return _aiff_to_wav(sound)
    raise ValueError('formato de sonido desconocido')


def _u8_file(data, name):
    """Contenido del archivo llamado name dentro de un archivo U8 (None si no está)."""
    root, = struct.unpack_from('>I', data, 4)
    count, = struct.unpack_from('>I', data, root + 8)
    names = root + count * 12
    for i in range(1, count):
        kind_name, offset, size = struct.unpack_from('>III', data, root + i * 12)
        if kind_name >> 24:
            continue  # carpeta
        pos = names + (kind_name & 0xFFFFFF)
        if data[pos:data.index(b'\0', pos)].decode('ascii', 'replace') == name:
            return data[offset:offset + size]
    return None


def _lz77(data):
    """Descomprime LZ77 de Nintendo (tipo 0x10)."""
    if data[0] != 0x10:
        raise ValueError('compresión no admitida')
    size = int.from_bytes(data[1:4], 'little')
    out = bytearray()
    pos = 4
    while len(out) < size:
        flags = data[pos]
        pos += 1
        for bit in range(8):
            if len(out) >= size:
                break
            if flags & (0x80 >> bit):
                pair = (data[pos] << 8) | data[pos + 1]
                pos += 2
                back = (pair & 0xFFF) + 1
                for _ in range((pair >> 12) + 3):
                    out.append(out[-back])
            else:
                out.append(data[pos])
                pos += 1
    return bytes(out)


def _wav(channels, rate, pcm):
    """Cabecera WAV + muestras PCM de 16 bits little-endian ya intercaladas."""
    return (b'RIFF' + struct.pack('<I', 36 + len(pcm)) + b'WAVEfmt '
            + struct.pack('<IHHIIHH', 16, 1, channels, rate, rate * channels * 2, channels * 2, 16)
            + b'data' + struct.pack('<I', len(pcm)) + pcm)


def _bns_to_wav(data):
    """Convierte un BNS (ADPCM de 4 bits de GameCube/Wii) a WAV."""
    info, _info_size, body, _body_size = struct.unpack_from('>IIII', data, 0x10)
    info += 8   # los desplazamientos de INFO son relativos al final de su cabecera
    body += 8
    codec, _loop, channels = struct.unpack_from('>BBB', data, info)
    rate, = struct.unpack_from('>H', data, info + 4)
    samples, = struct.unpack_from('>I', data, info + 12)
    table, = struct.unpack_from('>I', data, info + 16)
    if codec != 0 or channels not in (1, 2):
        raise ValueError('BNS con un códec no admitido')
    samples = min(samples, rate * _MAX_SECONDS)
    decoded = []
    for ch in range(channels):
        channel, = struct.unpack_from('>I', data, info + table + ch * 4)
        start, adpcm = struct.unpack_from('>II', data, info + channel)
        coefs = struct.unpack_from('>16h', data, info + adpcm)
        decoded.append(_decode_adpcm(data, body + start, samples, coefs))
    pcm = bytearray(samples * channels * 2)
    for ch, values in enumerate(decoded):
        pcm[ch * 2::channels * 2] = values[0::2]
        pcm[ch * 2 + 1::channels * 2] = values[1::2]
    return _wav(channels, rate, bytes(pcm))


def _decode_adpcm(data, pos, samples, coefs):
    """Decodifica un canal: tramas de 8 bytes (cabecera + 14 muestras de 4 bits). Devuelve PCM de 16 bits LE."""
    out = []
    append = out.append
    h1 = h2 = 0
    frames = (samples + 13) // 14
    for f in range(frames):
        base = pos + f * 8
        if base + 8 > len(data):
            break
        header = data[base]
        scale = 1 << (header & 0xF)
        index = (header >> 4) * 2
        c1, c2 = coefs[index & 15], coefs[(index + 1) & 15]
        for byte in data[base + 1:base + 8]:
            for nibble in (byte >> 4, byte & 0xF):
                if nibble > 7:
                    nibble -= 16
                value = (nibble * scale * 2048 + 1024 + c1 * h1 + c2 * h2) >> 11
                if value > 32767:
                    value = 32767
                elif value < -32768:
                    value = -32768
                h2 = h1
                h1 = value
                append(value)
    del out[samples:]
    out.extend([0] * (samples - len(out)))
    return struct.pack(f'<{samples}h', *out)


def _aiff_to_wav(data):
    """Convierte un AIFF PCM de 16 bits a WAV."""
    channels = rate = 0
    pcm = None
    pos = 12
    while pos + 8 <= len(data):
        chunk, size = data[pos:pos + 4], int.from_bytes(data[pos + 4:pos + 8], 'big')
        body = data[pos + 8:pos + 8 + size]
        if chunk == b'COMM':
            channels, _frames, bits = struct.unpack_from('>HIH', body)
            if bits != 16:
                raise ValueError('AIFF que no es de 16 bits')
            # Frecuencia en coma flotante de 80 bits: exponente y mantisa
            exponent, mantissa = struct.unpack_from('>HQ', body, 8)
            rate = int(mantissa * 2.0 ** ((exponent & 0x7FFF) - 16383 - 63))
        elif chunk == b'SSND':
            pcm = bytearray(body[8:])
        pos += 8 + size + (size & 1)
    if not (channels and rate and pcm):
        raise ValueError('AIFF incompleto')
    del pcm[rate * channels * 2 * _MAX_SECONDS:]
    if len(pcm) % 2:
        pcm.pop()
    pcm[0::2], pcm[1::2] = pcm[1::2], pcm[0::2]   # de big-endian a little-endian
    return _wav(channels, rate, bytes(pcm))
