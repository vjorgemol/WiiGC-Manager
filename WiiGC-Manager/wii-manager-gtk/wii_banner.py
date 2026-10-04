"""
Banner animado de un juego de Wii: la escena que se ve en el menú de la consola
al elegir el disco. No es un vídeo: opening.bnr lleva dentro meta/banner.bin,
un archivo U8 con una composición (BRLYT: paneles e imágenes en árbol), sus
animaciones (BRLAN: curvas por fotograma, a 60 fps) y las texturas (TPL).

Este módulo solo lee esos formatos; quien dibuja es widgets/banner_paintable.py.
Es una versión simplificada de lo que hace la consola: se dibujan las imágenes
(pic1) con su textura, color y transparencia; no los textos, las ventanas ni
las mezclas de color a medida de cada material.
"""
import struct
from array import array

from banner_sound import _U8_MAGIC, _lz77

FPS = 60
INTENSITY_FORMATS = (0, 1)      # I4, I8: texturas de un solo canal, usadas como máscara de transparencia

# Grupos de paneles por idioma de la consola: solo se muestra el de un idioma
LANGUAGES = ('JPN', 'ENG', 'GER', 'FRA', 'SPA', 'ITA', 'NED', 'CHN', 'KOR')
PREFERRED_LANGUAGES = ('SPA', 'ENG')


class Pane:
    """Panel de la composición: un nodo del árbol, con o sin imagen."""

    def __init__(self, name, kind):
        self.name = name
        self.kind = kind            # 'pan1', 'pic1', 'txt1', 'wnd1', 'bnd1'
        self.children = []
        self.visible = True
        self.hidden = False             # de un idioma que no es el elegido: nunca se dibuja
        self.influence_alpha = False    # su transparencia afecta también a sus hijos
        self.origin = 4             # 0-8: izquierda/centro/derecha × arriba/centro/abajo
        self.alpha = 255
        self.translate = [0.0, 0.0, 0.0]
        self.rotate = [0.0, 0.0, 0.0]
        self.scale = [1.0, 1.0]
        self.size = [0.0, 0.0]
        # Solo en las imágenes (pic1)
        self.material = None
        self.vertex_colors = [255] * 16     # RGBA de las esquinas: TL, TR, BL, BR
        self.tex_coords = None              # [(s, t)] de TL, TR, BL, BR


class Material:
    def __init__(self, name):
        self.name = name
        self.fore = [0, 0, 0, 0]            # color de las zonas negras de la textura
        self.back = [255, 255, 255, 255]    # color de las zonas blancas
        self.color = [255, 255, 255, 255]   # color del material (tinte)
        self.textures = []                  # [nombre de textura, wrap_s, wrap_t]
        self.tex_srt = []                   # [[transS, transT, rot, scaleS, scaleT]]
        self.additive = False


class Banner:
    """Composición lista para dibujar: tamaño, árbol de paneles, materiales, texturas y animaciones."""

    def __init__(self):
        self.width, self.height = 608.0, 456.0
        self.root = None
        self.panes = {}         # nombre → Pane
        self.materials = {}     # nombre → Material
        self.textures = {}      # nombre → (ancho, alto, RGBA, formato TPL)
        self.start = None       # Animation que se reproduce una vez (o None)
        self.loop = None        # Animation que se repite


class Animation:
    def __init__(self, frames):
        self.frames = frames
        self.tracks = []        # (nombre, es_material, tipo, índice, objetivo, [(fotograma, valor, pendiente)], escalonada)
        self.texture_names = []


def load(opening_bnr):
    """Banner de un opening.bnr (bytes). Lanza ValueError si no se entiende."""
    start = opening_bnr.find(_U8_MAGIC)
    if start < 0:
        raise ValueError('opening.bnr sin archivo U8')
    meta = u8_files(opening_bnr[start:])
    data = meta.get('meta/banner.bin')
    if not data:
        raise ValueError('opening.bnr sin banner.bin')
    files = u8_files(_unwrap(data))
    layouts = [name for name in files if name.endswith('.brlyt')]
    if not layouts:
        raise ValueError('banner.bin sin composición')
    banner = _read_brlyt(files[layouts[0]])
    for name, content in files.items():
        if name.endswith('.tpl'):
            try:
                banner.textures[name.rsplit('/', 1)[-1]] = decode_tpl(content)
            except (ValueError, IndexError, struct.error):
                pass  # textura en un formato no admitido: su imagen no se dibuja
    anims = {name.rsplit('/', 1)[-1].lower(): content for name, content in files.items() if name.endswith('.brlan')}
    for name, content in anims.items():
        animation = _read_brlan(content)
        if 'start' in name:
            banner.start = animation
        elif 'loop' in name or banner.loop is None:
            banner.loop = animation
    return banner


def _unwrap(data):
    """Quita la cabecera IMD5 y la compresión LZ77 que envuelven a banner.bin."""
    if data[:4] == b'IMD5':
        data = data[0x20:]
    if data[:4] == b'LZ77':
        data = _lz77(data[4:])
    return data


def u8_files(data):
    """Archivos de un U8: { 'carpeta/archivo': contenido }."""
    if data[:4] != _U8_MAGIC:
        raise ValueError('no es un archivo U8')
    root, = struct.unpack_from('>I', data, 4)
    count, = struct.unpack_from('>I', data, root + 8)
    names = root + count * 12
    files, dirs = {}, []    # dirs: [(nombre, índice del primer nodo de fuera)]
    for i in range(1, count):
        while dirs and i >= dirs[-1][1]:
            dirs.pop()
        kind_name, offset, size = struct.unpack_from('>III', data, root + i * 12)
        pos = names + (kind_name & 0xFFFFFF)
        name = data[pos:data.index(b'\0', pos)].decode('ascii', 'replace')
        if kind_name >> 24:
            dirs.append((name, size))
        else:
            files['/'.join([d for d, _end in dirs] + [name])] = data[offset:offset + size]
    return files


def _name(data, pos, length):
    return data[pos:pos + length].split(b'\0', 1)[0].decode('ascii', 'replace')


# ── Composición (BRLYT) ──────────────────────────────────────────────

def _read_brlyt(data):
    if data[:4] != b'RLYT':
        raise ValueError('composición desconocida')
    banner = Banner()
    header_size, sections = struct.unpack_from('>HH', data, 12)
    pos = header_size
    texture_names, materials = [], []
    stack, last = [], None
    groups = {}     # nombre del grupo → nombres de sus paneles
    for _ in range(sections):
        magic, size = data[pos:pos + 4], struct.unpack_from('>I', data, pos + 4)[0]
        if magic == b'lyt1':
            banner.width, banner.height = struct.unpack_from('>ff', data, pos + 12)
        elif magic == b'txl1':
            count, = struct.unpack_from('>H', data, pos + 8)
            base = pos + 12
            for i in range(count):
                offset, = struct.unpack_from('>I', data, base + i * 8)
                texture_names.append(_name(data, base + offset, 256))
        elif magic == b'mat1':
            count, = struct.unpack_from('>H', data, pos + 8)
            for i in range(count):
                offset, = struct.unpack_from('>I', data, pos + 12 + i * 4)
                material = _read_material(data, pos + offset, texture_names)
                materials.append(material)
                banner.materials[material.name] = material
        elif magic in (b'pan1', b'pic1', b'txt1', b'wnd1', b'bnd1'):
            pane = _read_pane(data, pos, magic.decode(), materials)
            banner.panes[pane.name] = pane
            if stack:
                stack[-1].children.append(pane)
            elif banner.root is None:
                banner.root = pane
            last = pane
        elif magic == b'grp1':
            count, = struct.unpack_from('>H', data, pos + 24)
            groups[_name(data, pos + 8, 16)] = [_name(data, pos + 28 + i * 16, 16) for i in range(count)]
        elif magic == b'pas1' and last is not None:
            stack.append(last)
        elif magic == b'pae1' and stack:
            last = stack.pop()
        pos += size
    if banner.root is None:
        raise ValueError('composición vacía')
    # Textos y logotipos traducidos: se ocultan los de los idiomas que no tocan
    shown = next((lang for lang in PREFERRED_LANGUAGES if lang in groups), None)
    if shown:
        keep = set(groups[shown])
        for lang in LANGUAGES:
            for name in groups.get(lang, []) if lang != shown else []:
                if name not in keep and name in banner.panes:
                    banner.panes[name].hidden = True
    return banner


def _read_material(data, pos, texture_names):
    material = Material(_name(data, pos, 20))
    material.fore = list(struct.unpack_from('>4h', data, pos + 20))
    material.back = list(struct.unpack_from('>4h', data, pos + 28))
    flags, = struct.unpack_from('>I', data, pos + 60)
    p = pos + 64
    for _ in range(flags & 0xF):
        index, wrap_s, wrap_t = struct.unpack_from('>HBB', data, p)
        name = texture_names[index] if index < len(texture_names) else ''
        material.textures.append([name, wrap_s & 3, wrap_t & 3])
        p += 4
    for _ in range((flags >> 4) & 0xF):
        material.tex_srt.append(list(struct.unpack_from('>5f', data, p)))
        p += 20
    p += 4 * ((flags >> 8) & 0xF)           # generadores de coordenadas
    if (flags >> 25) & 1:
        p += 4                              # control de canales
    if (flags >> 27) & 1:
        material.color = list(data[p:p + 4])
        p += 4
    if (flags >> 12) & 1:
        p += 4                              # tabla de intercambio
    p += 20 * ((flags >> 13) & 3)           # transformaciones indirectas
    p += 4 * ((flags >> 15) & 7)            # etapas indirectas
    p += 16 * ((flags >> 18) & 0x1F)        # etapas de color (TEV)
    if (flags >> 23) & 1:
        p += 4                              # comparación de alfa
    if (flags >> 24) & 1:
        # Modo de mezcla: tipo, origen, destino, operación. Destino «uno» = suma (brillos, luces)
        kind, _src, dst, _op = data[p:p + 4]
        material.additive = kind == 1 and dst == 1
    return material


def _read_pane(data, pos, kind, materials):
    pane = Pane(_name(data, pos + 12, 16), kind)
    flags, pane.origin, pane.alpha = data[pos + 8], data[pos + 9], data[pos + 10]
    pane.visible = bool(flags & 1)
    pane.influence_alpha = bool(flags & 2)
    values = struct.unpack_from('>10f', data, pos + 36)
    pane.translate, pane.rotate = list(values[0:3]), list(values[3:6])
    pane.scale, pane.size = list(values[6:8]), list(values[8:10])
    if kind == 'pic1':
        pane.vertex_colors = list(data[pos + 76:pos + 92])
        index, sets = struct.unpack_from('>HB', data, pos + 92)
        pane.material = materials[index] if index < len(materials) else None
        if sets:
            coords = struct.unpack_from('>8f', data, pos + 96)
            pane.tex_coords = [(coords[i], coords[i + 1]) for i in range(0, 8, 2)]
    return pane


# ── Animaciones (BRLAN) ──────────────────────────────────────────────

def _read_brlan(data):
    if data[:4] != b'RLAN':
        raise ValueError('animación desconocida')
    header_size, sections = struct.unpack_from('>HH', data, 12)
    pos = header_size
    for _ in range(sections):
        magic, size = data[pos:pos + 4], struct.unpack_from('>I', data, pos + 4)[0]
        if magic == b'pai1':
            return _read_pai1(data, pos)
        pos += size
    raise ValueError('animación vacía')


def _read_pai1(data, pos):
    frames, _loop, textures, entries, entries_offset = struct.unpack_from('>HBxHHI', data, pos + 8)
    animation = Animation(max(frames, 1))
    names = pos + 20
    for i in range(textures):
        offset, = struct.unpack_from('>I', data, names + i * 4)
        animation.texture_names.append(_name(data, names + offset, 256))
    for i in range(entries):
        entry = pos + struct.unpack_from('>I', data, pos + entries_offset + i * 4)[0]
        name = _name(data, entry, 20)
        tags, is_material = data[entry + 20], data[entry + 21]
        for t in range(tags):
            tag = entry + struct.unpack_from('>I', data, entry + 24 + t * 4)[0]
            kind = data[tag:tag + 4].decode('ascii', 'replace')
            for e in range(data[tag + 4]):
                item = tag + struct.unpack_from('>I', data, tag + 8 + e * 4)[0]
                index, target, data_type, count, offset = struct.unpack_from('>BBBxHxxI', data, item)
                keys = []
                if data_type == 1:      # escalonada: fotograma, valor entero
                    for k in range(count):
                        frame, value = struct.unpack_from('>fH', data, item + offset + k * 8)
                        keys.append((frame, value, 0.0))
                else:                   # curva de Hermite: fotograma, valor, pendiente
                    for k in range(count):
                        keys.append(struct.unpack_from('>3f', data, item + offset + k * 12))
                if keys:
                    animation.tracks.append((name, bool(is_material), kind, index, target, keys, data_type == 1))
    return animation


def sample(keys, frame, step):
    """Valor de una pista en un fotograma: escalonado o interpolado por Hermite entre sus claves."""
    if frame <= keys[0][0]:
        return keys[0][1]
    if frame >= keys[-1][0]:
        return keys[-1][1]
    low, high = 0, len(keys) - 1
    while high - low > 1:
        mid = (low + high) // 2
        if keys[mid][0] <= frame:
            low = mid
        else:
            high = mid
    f0, v0, s0 = keys[low]
    f1, v1, s1 = keys[high]
    if step or f1 == f0:
        return v0
    span = f1 - f0
    t = (frame - f0) / span
    t2, t3 = t * t, t * t * t
    return (v0 * (2 * t3 - 3 * t2 + 1) + v1 * (-2 * t3 + 3 * t2)
            + s0 * span * (t3 - 2 * t2 + t) + s1 * span * (t3 - t2))


# ── Texturas (TPL) ───────────────────────────────────────────────────

_TABLES = {}


def _table(name):
    """Tabla valor de píxel → RGBA para los formatos de 16 bits (se calcula una vez)."""
    if name not in _TABLES:
        table = []
        if name == 'rgb565':
            for v in range(65536):
                r, g, b = v >> 11, (v >> 5) & 0x3F, v & 0x1F
                table.append(bytes(((r << 3) | (r >> 2), (g << 2) | (g >> 4), (b << 3) | (b >> 2), 255)))
        elif name == 'rgb5a3':
            for v in range(65536):
                if v & 0x8000:
                    r, g, b = (v >> 10) & 0x1F, (v >> 5) & 0x1F, v & 0x1F
                    table.append(bytes(((r << 3) | (r >> 2), (g << 3) | (g >> 2), (b << 3) | (b >> 2), 255)))
                else:
                    a, r, g, b = (v >> 12) & 7, (v >> 8) & 0xF, (v >> 4) & 0xF, v & 0xF
                    table.append(bytes((r * 17, g * 17, b * 17, (a << 5) | (a << 2) | (a >> 1))))
        elif name == 'ia8':     # alfa en el byte alto, intensidad en el bajo
            for v in range(65536):
                i = v & 0xFF
                table.append(bytes((i, i, i, v >> 8)))
        _TABLES[name] = table
    return _TABLES[name]


def decode_tpl(data):
    """Primera imagen de un TPL como (ancho, alto, RGBA de 8 bits sin premultiplicar, formato)."""
    if data[:4] != b'\x00\x20\xaf\x30':
        raise ValueError('textura desconocida')
    table, = struct.unpack_from('>I', data, 8)
    header, palette_header = struct.unpack_from('>II', data, table)
    height, width, fmt, offset = struct.unpack_from('>HHII', data, header)
    palette = None
    if palette_header:
        count, _unpacked, pal_fmt, pal_offset = struct.unpack_from('>HBxII', data, palette_header)
        values = array('H', data[pal_offset:pal_offset + count * 2])
        values.byteswap()
        palette = [_table(('ia8', 'rgb565', 'rgb5a3')[pal_fmt])[v] for v in values]
    return width, height, _decode(data, offset, width, height, fmt, palette), fmt


def _decode(data, pos, width, height, fmt, palette):
    """Deshace los bloques de la textura y convierte sus píxeles a RGBA."""
    # (ancho de bloque, alto de bloque, bytes por bloque)
    block_w, block_h, block_bytes = {0: (8, 8, 32), 1: (8, 4, 32), 2: (8, 4, 32), 3: (4, 4, 32), 4: (4, 4, 32),
                                     5: (4, 4, 32), 6: (4, 4, 64), 8: (8, 8, 32), 9: (8, 4, 32),
                                     14: (8, 8, 32)}.get(fmt, (0, 0, 0))
    if not block_w:
        raise ValueError(f'formato de textura {fmt} no admitido')
    cols, rows = (width + block_w - 1) // block_w, (height + block_h - 1) // block_h
    blocks = []
    for i in range(cols * rows):
        raw = data[pos + i * block_bytes:pos + (i + 1) * block_bytes]
        if len(raw) < block_bytes:
            raw = raw + bytes(block_bytes - len(raw))
        blocks.append(_decode_block(raw, fmt, palette))
    # Cada bloque decodificado son block_h filas de block_w píxeles RGBA
    row_bytes = block_w * 4
    out = []
    for by in range(rows):
        line_blocks = blocks[by * cols:(by + 1) * cols]
        for y in range(block_h):
            if by * block_h + y >= height:
                break
            line = b''.join([b[y * row_bytes:(y + 1) * row_bytes] for b in line_blocks])
            out.append(line[:width * 4])
    return b''.join(out)


def _decode_block(raw, fmt, palette):
    # En los formatos de solo intensidad (I4, I8) la consola usa la intensidad también como alfa
    if fmt == 0:        # I4
        return b''.join([bytes((v * 17,)) * 4 for byte in raw for v in (byte >> 4, byte & 0xF)])
    if fmt == 1:        # I8
        return b''.join([bytes((v, v, v, v)) for v in raw])
    if fmt == 2:        # IA4
        return b''.join([bytes(((v & 0xF) * 17,)) * 3 + bytes(((v >> 4) * 17,)) for v in raw])
    if fmt in (3, 4, 5):
        values = array('H', raw)
        values.byteswap()
        table = _table(('ia8', 'rgb565', 'rgb5a3')[fmt - 3])
        return b''.join([table[v] for v in values])
    if fmt == 6:        # RGBA8: por bloque, 16 pares AR y 16 pares GB
        return b''.join([bytes((raw[i * 2 + 1], raw[32 + i * 2], raw[33 + i * 2], raw[i * 2])) for i in range(16)])
    if fmt == 8:        # CI4
        return b''.join([palette[v] if v < len(palette) else b'\0\0\0\0'
                         for byte in raw for v in (byte >> 4, byte & 0xF)])
    if fmt == 9:        # CI8
        return b''.join([palette[v] if v < len(palette) else b'\0\0\0\0' for v in raw])
    # CMPR: 2×2 sub-bloques de 4×4 comprimidos como DXT1
    subs = [_decode_dxt1(raw[i * 8:i * 8 + 8]) for i in range(4)]
    rows = []
    for top in (0, 2):
        for y in range(4):
            rows.append(subs[top][y] + subs[top + 1][y])
    return b''.join(rows)


def _decode_dxt1(raw):
    """Sub-bloque de 4×4: dos colores RGB565 y 2 bits por píxel. Devuelve sus 4 filas."""
    c0, c1 = struct.unpack_from('>HH', raw)
    table = _table('rgb565')
    p0, p1 = table[c0], table[c1]
    if c0 > c1:
        p2 = bytes(((2 * p0[i] + p1[i]) // 3 for i in range(3))) + b'\xff'
        p3 = bytes(((p0[i] + 2 * p1[i]) // 3 for i in range(3))) + b'\xff'
    else:
        p2 = bytes(((p0[i] + p1[i]) // 2 for i in range(3))) + b'\xff'
        p3 = b'\0\0\0\0'
    colors = (p0, p1, p2, p3)
    return [colors[b >> 6] + colors[(b >> 4) & 3] + colors[(b >> 2) & 3] + colors[b & 3] for b in raw[4:8]]
