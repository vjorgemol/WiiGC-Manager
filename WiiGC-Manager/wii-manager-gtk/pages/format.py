"""
Página Formatear/Preparar. Equivalente a view-format + loadFormatDevices()/
selectFormatDevice()/executeFormat() del frontend web.
"""
import gi
gi.require_version('Gtk', '4.0')
gi.require_version('Adw', '1')
from gi.repository import Gtk, Adw, GLib

from backend import core, run_async
from widgets.operation_status import OperationStatus
from widgets.device_row import DeviceRow


class FormatPage(Gtk.Box):
    """
    on_formatted(ruta) se llama tras un formateo correcto, con la ruta por la que
    explorar la unidad; get_device_path() da la unidad explorada, que es donde se
    instalan los cargadores.
    """

    def __init__(self, log, on_formatted=None, get_device_path=lambda: ''):
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=16)
        self.set_margin_top(16)
        self.set_margin_bottom(16)
        self.set_margin_start(16)
        self.set_margin_end(16)

        self._log = log
        self._on_formatted = on_formatted
        self._get_device_path = get_device_path
        self._selected_device = None
        self._fs_type = 'fat32'

        self.append(self._build_intro())
        self.append(self._build_device_section())
        self.append(self._build_options_section())
        self.append(self._build_execute_section())
        self.append(self._build_loaders_section())

        self.refresh_devices()

    # ── Intro ────────────────────────────────────────────────────
    @staticmethod
    def _build_intro():
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        title = Gtk.Label(label='Preparar y Formatear Unidad (Wii & GameCube)', xalign=0)
        title.add_css_class('title-2')
        desc = Gtk.Label(
            label='Prepara un pendrive USB, disco externo o tarjeta SD para WiiFlow, '
                  'USB Loader GX y Nintendont. Protege automáticamente los discos del sistema.',
            xalign=0, wrap=True)
        desc.add_css_class('dim-label')
        box.append(title)
        box.append(desc)
        return box

    # ── 1. Dispositivos ──────────────────────────────────────────
    def _build_device_section(self):
        group = Adw.PreferencesGroup(title='1 · Seleccionar unidad o partición')

        header = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL)
        refresh_btn = Gtk.Button(label='↺ Actualizar unidades')
        refresh_btn.connect('clicked', lambda *_: self.refresh_devices())
        header.append(Gtk.Box(hexpand=True))
        header.append(refresh_btn)
        group.set_header_suffix(header)

        self._device_list_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
        scroller = Gtk.ScrolledWindow(min_content_height=220)
        scroller.set_child(self._device_list_box)
        group.add(scroller)

        self._target_summary = Gtk.Label(label='Ninguna unidad seleccionada', xalign=0)
        self._target_summary.add_css_class('dim-label')
        group.add(self._target_summary)
        return group

    def refresh_devices(self):
        """Vuelve a detectar discos y particiones y repinta la lista."""
        for child in list(self._device_list_box):
            self._device_list_box.remove(child)
        self._device_list_box.append(Gtk.Label(label='Detectando unidades de almacenamiento…'))
        run_async(core.get_block_devices, on_done=self._on_devices_loaded, on_error=self._on_devices_error)

    def _on_devices_loaded(self, devices):
        """Pinta los dispositivos formateables; los del sistema ni se muestran."""
        for child in list(self._device_list_box):
            self._device_list_box.remove(child)

        rows_added = 0
        for dev in devices:
            partitions = [p for p in dev.get('partitions', []) if not p.get('is_system')]
            if not dev.get('partitions'):
                # Disco sin particiones (p.ej. una unidad sin formatear): se
                # muestra solo si no es del sistema.
                if dev.get('is_system'):
                    continue
                self._device_list_box.append(DeviceRow(dev, on_select=self._select_device))
                rows_added += 1
            else:
                if not partitions:
                    continue  # todas las particiones son del sistema: ocultar el disco entero
                header = Gtk.Label(label=f"💿 {dev.get('model') or dev['name']} — {dev['size_formatted']}", xalign=0)
                header.add_css_class('caption-heading')
                header.set_margin_top(6)
                self._device_list_box.append(header)
                for part in partitions:
                    self._device_list_box.append(DeviceRow(part, on_select=self._select_device))
                    rows_added += 1

        if rows_added == 0:
            self._device_list_box.append(Gtk.Label(
                label='No se detectaron unidades de almacenamiento (externas a tu sistema).'))

    def _on_devices_error(self, error):
        for child in list(self._device_list_box):
            self._device_list_box.remove(child)
        self._device_list_box.append(Gtk.Label(label=f'Error al detectar dispositivos: {error}'))

    def _select_device(self, device):
        """Clic en un dispositivo: queda como destino del formateo."""
        self._selected_device = device
        self._target_summary.set_text(
            f"{device['path']} — {device.get('model') or device['name']} — {device['size_formatted']}")
        self._execute_btn.set_sensitive(True)

    # ── 2. Opciones ──────────────────────────────────────────────
    def _build_options_section(self):
        group = Adw.PreferencesGroup(title='2 · Sistema de archivos y estructura')

        fat32_row = Adw.ActionRow(title='FAT32 — Recomendado',
                                   subtitle='Compatible con Wii (.wbfs) y GameCube (.iso) a la vez.')
        wbfs_row = Adw.ActionRow(title='WBFS Puro',
                                  subtitle='Partición clásica gestionada con wwt. Solo Wii, sin GameCube.')
        self._fat32_check = Gtk.CheckButton()
        self._fat32_check.set_active(True)
        wbfs_check = Gtk.CheckButton(group=self._fat32_check)
        self._fat32_check.connect('toggled', self._on_fs_type_toggled)
        fat32_row.add_prefix(self._fat32_check)
        wbfs_row.add_prefix(wbfs_check)
        fat32_row.set_activatable_widget(self._fat32_check)
        wbfs_row.set_activatable_widget(wbfs_check)
        group.add(fat32_row)
        group.add(wbfs_row)

        self._label_row = Adw.EntryRow(title='Etiqueta del volumen')
        self._label_row.set_text('WII')
        group.add(self._label_row)

        self._folders_row = Adw.SwitchRow(title='Crear carpetas',
                                           subtitle='/wbfs, /games, /apps, /wiiflow')
        self._folders_row.set_active(True)
        group.add(self._folders_row)

        return group

    def _on_fs_type_toggled(self, btn):
        """La etiqueta y las carpetas solo se aplican al formatear en FAT32."""
        self._fs_type = 'fat32' if btn.get_active() else 'wbfs'
        sensitive = self._fs_type == 'fat32'
        self._label_row.set_sensitive(sensitive)
        self._folders_row.set_sensitive(sensitive)

    # ── 3. Ejecutar ──────────────────────────────────────────────
    def _build_execute_section(self):
        group = Adw.PreferencesGroup(title='3 · Ejecutar formateo')
        warning = Gtk.Label(
            xalign=0, wrap=True,
            label='⚠️ El formateo eliminará todos los archivos de la unidad o partición seleccionada.')
        warning.add_css_class('warning')
        group.add(warning)

        self._execute_btn = Gtk.Button(label='⚡ Formatear y preparar unidad seleccionada')
        self._execute_btn.add_css_class('destructive-action')
        self._execute_btn.set_sensitive(False)
        self._execute_btn.connect('clicked', self._on_execute_clicked)
        self._execute_btn.set_margin_top(12)
        group.add(self._execute_btn)

        # Progreso y resultado a la vista: el terminal de resultados puede estar oculto
        self._progress = Gtk.ProgressBar(show_text=True, visible=False)
        self._progress.set_margin_top(12)
        group.add(self._progress)
        self._result_label = Gtk.Label(xalign=0, wrap=True, visible=False)
        self._result_label.set_margin_top(12)
        group.add(self._result_label)
        self._formatting = False
        return group

    def _poll_progress(self):
        """Cada 200 ms durante el formateo: paso y porcentaje que publica el backend."""
        if not self._formatting:
            self._progress.set_visible(False)
            return GLib.SOURCE_REMOVE
        progress = core.api_progress({})  # solo consulta el paso actual: no bloquea
        if progress['active'] and progress['percent'] is not None:
            self._progress.set_fraction(progress['percent'] / 100)
            self._progress.set_text(f"{progress['percent']} % — {progress['step']}")
        return GLib.SOURCE_CONTINUE

    def _show_result(self, lines, ok):
        """Oculta la barra y deja a la vista el resultado del formateo, en verde o rojo."""
        self._formatting = False
        self._progress.set_visible(False)
        self._result_label.set_label('\n'.join(lines))
        self._result_label.remove_css_class('success')
        self._result_label.remove_css_class('error')
        self._result_label.add_css_class('success' if ok else 'error')
        self._result_label.set_visible(True)

    # ── 4. Cargadores sin formatear ──────────────────────────────
    def _build_loaders_section(self):
        group = Adw.PreferencesGroup(
            title='4 · Instalar o actualizar cargadores (sin formatear)',
            description='Descarga Nintendont, WiiFlow Lite y USB Loader GX en la unidad explorada, sin borrar nada. '
                        'Los que ya están en su última versión se dejan como están.')
        self._app_rows = {}
        for key, app in core.MANAGED_APPS.items():
            row = Adw.SwitchRow(title=app['name'], subtitle='Sin comprobar')
            row.set_active(True)
            group.add(row)
            self._app_rows[key] = row

        self._loaders_btn = Gtk.Button(label='Instalar / actualizar en la unidad', css_classes=['suggested-action'])
        self._loaders_btn.set_margin_top(12)
        self._loaders_btn.connect('clicked', lambda *_: self._install_loaders())
        group.add(self._loaders_btn)

        self._loaders_status = OperationStatus()
        self._loaders_status.set_margin_top(12)
        group.add(self._loaders_status)
        return group

    def refresh_loaders(self):
        """Consulta qué cargadores hay en la unidad explorada y si tienen versión nueva."""
        path = self._get_device_path()
        if not path or not core.is_mounted_dir(path):
            for row in self._app_rows.values():
                row.set_subtitle('Explora antes una unidad montada (FAT32) desde la barra lateral')
            self._loaders_btn.set_sensitive(False)
            return
        self._loaders_btn.set_sensitive(not self._loaders_status.busy)
        for row in self._app_rows.values():
            row.set_subtitle('Comprobando…')
        run_async(core.api_loaders_status, {'path': path},
                  on_done=lambda data: self._on_loaders_status(path, data),
                  on_error=lambda e: self._on_loaders_status(path, {'error': str(e)}))

    def _on_loaders_status(self, path, data):
        """Escribe en cada fila el estado de su cargador: no instalado, al día, desactualizado…"""
        if path != self._get_device_path():
            return  # se cambió de unidad mientras se consultaba
        for key, row in self._app_rows.items():
            info = (data.get('loaders') or {}).get(key)
            if not info:
                row.set_subtitle(data.get('error', 'No se pudo comprobar'))
                continue
            latest = info['latest_version'] or 'desconocida (sin conexión con GitHub)'
            row.set_subtitle({
                'missing':  f'No instalado · última versión: {latest}',
                'current':  f"Instalado {info['installed_version']} · al día",
                'outdated': f"Instalado {info['installed_version']} · disponible {latest}",
                'unknown':  f'Instalado (versión desconocida) · última versión: {latest}',
            }[info['state']])

    def _install_loaders(self):
        """Descarga e instala en la unidad explorada los cargadores marcados."""
        path = self._get_device_path()
        keys = [key for key, row in self._app_rows.items() if row.get_active()]
        if not keys or self._loaders_status.busy:
            return
        self._loaders_btn.set_sensitive(False)
        self._log.append(f'Instalando / actualizando cargadores en {path}…', 'info')
        self._loaders_status.start(f'Instalando / actualizando cargadores en {path}… No desconectes la unidad.')
        run_async(core.api_loaders_install, {'path': path, 'loaders': keys},
                  on_done=self._on_loaders_installed,
                  on_error=lambda e: self._on_loaders_installed({'error': str(e)}))

    def _on_loaders_installed(self, data):
        self._loaders_btn.set_sensitive(True)
        if data.get('error'):
            self._log.append(f"✗ {data['error']}", 'err')
            self._loaders_status.finish(f"✗ {data['error']}", False)
            return
        texts = {'installed': '✓ {name} {version} instalado', 'updated': '✓ {name} actualizado a {version}',
                 'current': '✓ {name} {version} ya estaba al día', 'error': '✗ {name}: {error}'}
        lines = [texts[r['action']].format(name=r['name'], version=r.get('version', ''), error=r.get('error', ''))
                 for r in data.get('results', {}).values()]
        ok = all(r['action'] != 'error' for r in data.get('results', {}).values())
        for line in lines:
            self._log.append(line, 'ok' if line.startswith('✓') else 'err')
        self._loaders_status.finish('\n'.join(lines), ok)
        self.refresh_loaders()

    def _on_execute_clicked(self, _btn):
        """Pide confirmación: el formateo borra todo el contenido de la unidad."""
        if not self._selected_device:
            return
        dev = self._selected_device
        folders = self._folders_row.get_active() if self._fs_type == 'fat32' else False

        dialog = Adw.AlertDialog(
            heading='Confirmar formateo',
            body=(f"Dispositivo: {dev['path']}\n"
                  f"Formato: {self._fs_type.upper()}"
                  + (f"  |  Etiqueta: {self._label_row.get_text()}  |  Carpetas: {'Sí' if folders else 'No'}"
                     if self._fs_type == 'fat32' else ''))
        )
        dialog.add_response('cancel', 'Cancelar')
        dialog.add_response('format', 'Formatear')
        dialog.set_response_appearance('format', Adw.ResponseAppearance.DESTRUCTIVE)
        dialog.connect('response', self._on_confirm_response, dev, folders)
        dialog.present(self.get_root())

    def _on_confirm_response(self, dialog, response, dev, folders):
        """Confirmado: lanza el formateo en un hilo y arranca el sondeo del progreso."""
        if response != 'format':
            return
        params = {
            'device': dev['path'],
            'fs_type': self._fs_type,
            'label': self._label_row.get_text() or 'WII',
            'create_folders': folders,
            'confirm': True,
        }
        self._execute_btn.set_sensitive(False)
        self._formatting = True
        self._result_label.set_visible(False)
        self._progress.set_fraction(0)
        self._progress.set_text('0 % — Iniciando…')
        self._progress.set_visible(True)
        GLib.timeout_add(200, self._poll_progress)
        self._log.append(f"⚡ Iniciando formateo de {dev['path']} como {self._fs_type.upper()}…", 'info')
        run_async(core.api_format, params, on_done=self._on_format_done, on_error=self._on_format_error)

    def _on_format_done(self, data):
        self._execute_btn.set_sensitive(True)
        lines = []
        if data.get('success'):
            lines.append(f"✓ Formateo completado: {data.get('device')} → {data.get('fs_type')}")
            self._log.append(lines[-1], 'ok')
            if data.get('folders_created'):
                self._log.append(f"✓ Carpetas creadas: {', '.join(data['folders_created'])}", 'ok')
                lines.append('✓ Carpetas creadas')
        else:
            lines.append(f"✗ Error: {data.get('error') or data.get('stderr') or 'desconocido'}")
            self._log.append(lines[-1], 'err')
        self._show_result(lines, bool(data.get('success')))
        self.refresh_devices()
        if data.get('success') and self._on_formatted:
            # Ruta por la que se explora la unidad recién formateada: punto de
            # montaje (FAT32) o el propio dispositivo (WBFS)
            self._on_formatted(data.get('mount_path') or data.get('device', ''))
        # La unidad recién formateada no tiene cargadores: reflejarlo en la sección de abajo
        self.refresh_loaders()

    def _on_format_error(self, error):
        self._execute_btn.set_sensitive(True)
        self._log.append(f"✗ Error de conexión: {error}", 'err')
        self._show_result([f'✗ Error: {error}'], False)
