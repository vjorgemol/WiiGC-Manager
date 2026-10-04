"""
Dibuja el banner animado de un juego de Wii (ver wii_banner.py) como un
Gdk.Paintable: la Videoteca lo pone en el hueco de la carátula y avanza el
tiempo con set_time(). Cada fotograma se recalculan las animaciones y se
recorre el árbol de paneles pintando sus imágenes.
"""
import math

import gi
gi.require_version('Gtk', '4.0')
gi.require_version('Gdk', '4.0')
gi.require_version('Gsk', '4.0')
gi.require_version('Graphene', '1.0')
from gi.repository import Gdk, GLib, GObject, Graphene, Gsk

import wii_banner

def _rect(x, y, width, height):
    return Graphene.Rect().init(x, y, width, height)


def _clamp(value):
    return 0.0 if value < 0 else 1.0 if value > 255 else value / 255


class BannerPaintable(GObject.Object, Gdk.Paintable):
    def __init__(self, banner):
        super().__init__()
        self._banner = banner
        self._textures = {}
        self._masks = {name for name, texture in banner.textures.items() if texture[3] in wii_banner.INTENSITY_FORMATS}
        for name, (width, height, rgba, _fmt) in banner.textures.items():
            self._textures[name] = Gdk.MemoryTexture.new(
                width, height, Gdk.MemoryFormat.R8G8B8A8, GLib.Bytes.new(rgba), width * 4)
        # Animaciones ya resueltas a (objeto, atributo, posición, claves, escalonada)
        self._start = self._bind(banner.start)
        self._loop = self._bind(banner.loop)
        self._start_frames = banner.start.frames if banner.start else 0
        self._loop_frames = banner.loop.frames if banner.loop else 1
        self.set_time(0.0)

    # ── Animación ────────────────────────────────────────────────
    def _bind(self, animation):
        """Enlaza cada pista de la animación con el valor del panel o material que mueve."""
        bound = []
        if animation is None:
            return bound
        for name, is_material, kind, index, target, keys, step in animation.tracks:
            if is_material:
                material = self._banner.materials.get(name)
                if material is None:
                    continue
                if kind == 'RLMC' and target < 12:
                    bound.append(((material.color, material.fore, material.back)[target // 4], target % 4, keys, step))
                elif kind == 'RLTS' and index < len(material.tex_srt) and target < 5:
                    bound.append((material.tex_srt[index], target, keys, step))
                elif kind == 'RLTP' and index < len(material.textures):
                    names = animation.texture_names
                    bound.append((material.textures[index], 0, [(f, names[int(v)] if int(v) < len(names) else '', 0)
                                                               for f, v, _s in keys], True))
                continue
            pane = self._banner.panes.get(name)
            if pane is None:
                continue
            if kind == 'RLPA' and target < 10:
                values = (pane.translate, pane.rotate, pane.scale, pane.size)
                group, position = ((0, target), (1, target - 3), (2, target - 6), (3, target - 8))[
                    0 if target < 3 else 1 if target < 6 else 2 if target < 8 else 3]
                bound.append((values[group], position, keys, step))
            elif kind == 'RLVI':
                bound.append((pane, 'visible', keys, True))
            elif kind == 'RLVC' and target < 16:
                bound.append((pane.vertex_colors, target, keys, step))
            elif kind == 'RLVC' and target == 16:
                bound.append((pane, 'alpha', keys, step))
        return bound

    @staticmethod
    def _apply(bound, frame):
        for obj, position, keys, step in bound:
            if isinstance(keys[0][1], str):
                # Cambio de textura: la de la última clave ya alcanzada
                value = keys[0][1]
                for key_frame, name, _slope in keys:
                    if key_frame <= frame:
                        value = name
                obj[position] = value
                continue
            value = wii_banner.sample(keys, frame, step)
            if position == 'visible':
                obj.visible = bool(value)
            elif position == 'alpha':
                obj.alpha = value
            else:
                obj[position] = value

    def set_time(self, seconds):
        """Coloca la animación en el instante dado: primero la de entrada (una vez) y luego la que se repite."""
        frame = seconds * wii_banner.FPS
        if self._start:
            self._apply(self._start, min(frame, self._start_frames))
        if frame >= self._start_frames or not self._start:
            self._apply(self._loop, (frame - self._start_frames) % self._loop_frames)
        self.invalidate_contents()

    # ── Dibujo ───────────────────────────────────────────────────
    def do_get_intrinsic_width(self):
        return int(self._banner.width)

    def do_get_intrinsic_height(self):
        return int(self._banner.height)

    def do_get_intrinsic_aspect_ratio(self):
        return self._banner.width / self._banner.height

    def do_snapshot(self, snapshot, width, height):
        banner = self._banner
        snapshot.push_clip(_rect(0, 0, width, height))
        snapshot.save()
        # La composición tiene el origen en el centro y el eje Y hacia arriba
        snapshot.translate(Graphene.Point().init(width / 2, height / 2))
        snapshot.scale(width / banner.width, height / banner.height)
        self._draw(snapshot, banner.root, 1.0)
        snapshot.restore()
        snapshot.pop()

    def _draw(self, snapshot, pane, alpha):
        if not pane.visible or pane.hidden:
            return
        own_alpha = alpha * min(max(pane.alpha, 0), 255) / 255
        snapshot.save()
        snapshot.translate(Graphene.Point().init(pane.translate[0], -pane.translate[1]))
        if pane.rotate[2]:
            snapshot.rotate(-pane.rotate[2])
        # Los giros en X e Y son en 3D: se aproximan aplastando el panel
        scale_x = pane.scale[0] * math.cos(math.radians(pane.rotate[1]))
        scale_y = pane.scale[1] * math.cos(math.radians(pane.rotate[0]))
        if scale_x and scale_y:
            snapshot.scale(scale_x, scale_y)
            if pane.kind == 'pic1' and own_alpha > 0.004:
                self._draw_picture(snapshot, pane, own_alpha)
            child_alpha = own_alpha if pane.influence_alpha else alpha
            for child in pane.children:
                self._draw(snapshot, child, child_alpha)
        snapshot.restore()

    def _draw_picture(self, snapshot, pane, alpha):
        width, height = pane.size
        if width <= 0 or height <= 0:
            return
        x = (0, -width / 2, -width)[pane.origin % 3]
        y = (0, -height / 2, -height)[min(pane.origin // 3, 2)]
        bounds = _rect(x, y, width, height)
        colors = pane.vertex_colors
        corners = [[_clamp(c) for c in colors[i:i + 4]] for i in (0, 4, 8, 12)]     # TL, TR, BL, BR
        tint = [sum(corner[i] for corner in corners) / 4 for i in range(4)]
        material = pane.material
        texture = self._textures.get(material.textures[0][0]) if material and material.textures else None

        # Degradado de transparencia entre esquinas (fundidos): se aplica como máscara
        alphas = [corner[3] for corner in corners]
        gradient = max(alphas) - min(alphas) > 0.02
        if gradient:
            snapshot.push_mask(Gsk.MaskMode.ALPHA)
            vertical = abs(alphas[0] - alphas[2]) + abs(alphas[1] - alphas[3]) >= abs(alphas[0] - alphas[1]) + abs(alphas[2] - alphas[3])
            first = (alphas[0] + alphas[1]) / 2 if vertical else (alphas[0] + alphas[2]) / 2
            last = (alphas[2] + alphas[3]) / 2 if vertical else (alphas[1] + alphas[3]) / 2
            stops = []
            for offset, value in ((0.0, first), (1.0, last)):
                stop = Gsk.ColorStop()
                stop.offset = offset
                color = Gdk.RGBA()
                color.red = color.green = color.blue = 1.0
                color.alpha = value
                stop.color = color
                stops.append(stop)
            end = Graphene.Point().init(x, y + height) if vertical else Graphene.Point().init(x + width, y)
            snapshot.append_linear_gradient(bounds, Graphene.Point().init(x, y), end, stops)
            snapshot.pop()
            tint[3] = 1.0

        if texture is None:
            # Sin textura: rectángulo de color liso
            color = Gdk.RGBA()
            color.red, color.green, color.blue, color.alpha = tint[0], tint[1], tint[2], tint[3] * alpha
            snapshot.append_color(color, bounds)
        else:
            self._draw_textured(snapshot, pane, material, texture, bounds, tint, alpha)
        if gradient:
            snapshot.pop()

    def _draw_textured(self, snapshot, pane, material, texture, bounds, tint, alpha):
        # Color final = (primer plano + (fondo − primer plano) × textura) × tinte
        fore = [_clamp(c) for c in material.fore]
        back = [_clamp(c) for c in material.back]
        scale = [(back[i] - fore[i]) * tint[i] for i in range(4)]
        offset = [fore[i] * tint[i] for i in range(4)]
        scale[3] *= alpha
        offset[3] *= alpha
        rows = [[scale[0], 0, 0, 0], [0, scale[1], 0, 0], [0, 0, scale[2], 0], [0, 0, 0, scale[3]]]
        if material.additive:
            # Mezcla por suma (brillos): lo oscuro no debe tapar, así que pasa a ser transparente
            rows[3] = [scale[3] / 3] * 3 + [0]
        matrix = Graphene.Matrix()
        # Graphene guarda las matrices por columnas: se pasa traspuesta
        matrix.init_from_float([rows[r][c] for c in range(4) for r in range(4)])
        snapshot.push_color_matrix(matrix, Graphene.Vec4().init(*offset))

        # Segunda textura de un solo canal: es la máscara de transparencia de la primera
        # (imágenes comprimidas sin canal alfa, con la silueta aparte)
        mask = None
        if len(material.textures) > 1 and material.textures[1][0] in self._masks \
                and material.textures[0][0] not in self._masks:
            mask = self._textures.get(material.textures[1][0])
        if mask is not None:
            snapshot.push_mask(Gsk.MaskMode.ALPHA)
            self._append_texture(snapshot, pane, material, mask, bounds)
            snapshot.pop()
        self._append_texture(snapshot, pane, material, texture, bounds)
        if mask is not None:
            snapshot.pop()
        snapshot.pop()

    @staticmethod
    def _append_texture(snapshot, pane, material, texture, bounds):
        """Añade la textura recortada (o repetida) a la zona del panel, según sus coordenadas de textura."""
        # Zona de la textura que se ve: de las coordenadas de las esquinas y de la transformación del material
        coords = pane.tex_coords or [(0.0, 0.0), (1.0, 0.0), (0.0, 1.0), (1.0, 1.0)]
        (s0, t0), (s1, _t), (_s, t1) = coords[0], coords[1], coords[2]
        if material.tex_srt:
            trans_s, trans_t, _rot, scale_s, scale_t = material.tex_srt[0]
            s0, s1 = [(s - 0.5) * scale_s + 0.5 + trans_s for s in (s0, s1)]
            t0, t1 = [(t - 0.5) * scale_t + 0.5 + trans_t for t in (t0, t1)]
        span_s, span_t = s1 - s0, t1 - t0
        x, y, width, height = bounds.get_x(), bounds.get_y(), bounds.get_width(), bounds.get_height()
        if abs(span_s) < 1e-6 or abs(span_t) < 1e-6:
            # Nada que ver: un nodo vacío, para no descuadrar las máscaras que esperan contenido
            snapshot.append_color(Gdk.RGBA(), bounds)
            return
        # Se dibuja en «espacio de textura»: una copia entera de la imagen ocupa tile_w × tile_h
        # (negativos si va reflejada), y de ahí se ve solo el trozo que piden las coordenadas
        tile_w, tile_h = width / span_s, height / span_t
        snapshot.save()
        snapshot.translate(Graphene.Point().init(x - s0 * tile_w, y - t0 * tile_h))
        snapshot.scale(1 if tile_w > 0 else -1, 1 if tile_h > 0 else -1)
        tile_w, tile_h = abs(tile_w), abs(tile_h)
        tile = _rect(0, 0, tile_w, tile_h)
        visible = _rect(min(s0, s1) * tile_w, min(t0, t1) * tile_h, width, height)
        _name, wrap_s, wrap_t = material.textures[0]
        if wrap_s or wrap_t:
            snapshot.push_repeat(visible, tile)
        else:
            snapshot.push_clip(visible)
        snapshot.append_scaled_texture(texture, Gsk.ScalingFilter.LINEAR, tile)
        snapshot.pop()
        snapshot.restore()
