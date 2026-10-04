# WiiFlow Manager

GUI web para gestionar juegos Wii en particiones WBFS/USB, construida sobre las herramientas [`wit`](https://wit.wiimm.de/) y [`wwt`](https://wit.wiimm.de/) de Wiimm.

> Funciona exclusivamente en **Linux**. Requiere Python 3 y tener `wit`/`wwt` instalados.

📖 **[Manual de uso](MANUAL.html)** de la app de escritorio (WiiGC Manager, GTK4): instalación, Videoteca, formateo, conversión, migración y verificación.

---

## Características

- 📋 **Listar juegos** en la partición WBFS con título, ID, tamaño y región
- ➕ **Añadir juegos** desde archivos ISO / WBFS
- ❌ **Eliminar juegos** individualmente o en lote
- 💾 **Exportar a ISO** cualquier juego de la partición
- 🔄 **Convertir** ISO → WBFS y WBFS → ISO (también en lote)
- 🚚 **Migrar** todos los juegos de una unidad a otra, p. ej. a un disco de más capacidad (solo en la app GTK4)
- 📤 **Copiar** los juegos seleccionados a otra unidad (solo en la app GTK4)
- ✅ **Verificar integridad** de juegos con `wit VERIFY`
- 🖼️ **Carátulas automáticas** desde [GameTDB](https://www.gametdb.com/Wii) con fallback por región y tipo
- 📊 **Estadísticas** de espacio usado y libre en el USB
- 🔍 **Búsqueda y filtrado** en tiempo real
- 🖥️ **Terminal integrado** que muestra los comandos ejecutados y su salida
- ⚙️ **Detección automática** del USB con `--auto` si no se configura ruta
- 🎨 Vista en **cuadrícula** o **lista**

---

## Requisitos

- Linux (cualquier distribución moderna)
- Python 3.6+
- `wit` y `wwt` instalados y accesibles en el PATH

### Instalar wit/wwt

Descarga el paquete para tu distribución desde la web oficial:

```
https://wit.wiimm.de/download.html
```

En Debian/Ubuntu puede estar disponible directamente:

```bash
sudo apt install wit
```

---

## Instalación

```bash
git clone https://github.com/TU_USUARIO/wii-manager.git
cd wii-manager
```

No hay dependencias de Python adicionales. Solo se usa la biblioteca estándar.

---

## Uso

### 1. Arrancar el servidor

```bash
python3 wii-manager-server.py
```

Verás algo así:

```
══════════════════════════════════════════════════════
  WiiFlow Manager — Backend
  http://localhost:8765
  Ctrl+C para detener
══════════════════════════════════════════════════════
  wit : /usr/bin/wit
  wwt : /usr/bin/wwt
```

### 2. Abrir la GUI

```bash
xdg-open http://localhost:8765
```

O abre manualmente `http://localhost:8765` en tu navegador.

### 3. Explorar el USB

- Introduce la ruta de tu partición WBFS en el campo lateral (ej. `/dev/sdb1`)
- Pulsa **Explorar dispositivo**
- Si dejas el campo vacío, se usará `wwt --auto` para detectar la partición automáticamente

> **Nota sobre permisos:** si `wwt` no puede acceder a `/dev/sdb1`, ejecuta el servidor con `sudo` o añade tu usuario al grupo `disk`:
> ```bash
> sudo usermod -aG disk $USER
> # Cierra sesión y vuelve a entrar para que tenga efecto
> ```

---

## Comandos que genera la GUI

| Operación | Comando |
|---|---|
| Listar juegos | `wwt LIST -p /dev/sdb1 --long` |
| Añadir ISO | `wwt ADD -p /dev/sdb1 "juego.iso"` |
| Eliminar juego | `wwt REMOVE -p /dev/sdb1 RMCP01` |
| Exportar a ISO | `wwt EXTRACT -p /dev/sdb1 RMCP01 --dest /ruta/` |
| Verificar partición | `wwt VERIFY -p /dev/sdb1` |
| Verificar archivo | `wit VERIFY "juego.iso"` |
| Espacio en disco | `wwt SPACE -p /dev/sdb1` |
| Detección automática | `wwt --auto LIST` |

---

## Carátulas

Las carátulas se cargan desde [GameTDB](https://art.gametdb.com) bajo demanda pulsando el botón **⊡ Carátulas**.

Si no existe carátula para la región configurada, la GUI prueba automáticamente en este orden:

**Regiones:** `ES → EN → FR → DE → IT → PT → AU → US → JA → KO`

**Tipos:** `cover3D → cover → coverfull → disc`

Las URLs encontradas se cachean en memoria durante la sesión para no repetir peticiones.

La región y el tipo preferidos se configuran en **Ajustes → Carátulas**.

---

## Lanzador de escritorio

Desde **Ajustes** puedes generar un archivo `.desktop` para lanzar la aplicación directamente desde tu entorno de escritorio (GNOME, KDE, XFCE…).

También puedes crearlo manualmente:

```ini
[Desktop Entry]
Version=1.0
Type=Application
Name=WiiFlow Manager
Comment=Gestor de juegos Wii (wit/wwt GUI)
Exec=bash -c "cd '/ruta/wii-manager' && python3 wii-manager-server.py & sleep 1 && xdg-open http://localhost:8765"
Icon=applications-games
Terminal=false
Categories=Game;Utility;
```

Guárdalo en `~/.local/share/applications/wiimanager.desktop`.

---

## Estructura del proyecto

```
wii-manager/
├── wii-manager.html         # Frontend (HTML + CSS + JS, sin dependencias)
├── wii-manager-server.py    # Backend (Python 3, stdlib únicamente)
└── README.md
```

---

## API del servidor

El servidor expone una API REST local en `http://localhost:8765`:

| Endpoint | Método | Descripción |
|---|---|---|
| `/api/status` | GET | Detecta rutas de wit y wwt |
| `/api/list` | GET | Lista juegos y espacio en disco |
| `/api/add` | POST | Añade un ISO a la partición |
| `/api/remove` | POST | Elimina un juego por ID |
| `/api/extract` | POST | Exporta un juego a ISO |
| `/api/verify` | POST | Verifica integridad |
| `/api/run` | POST | Ejecuta un comando wit/wwt arbitrario |

Todos los endpoints aceptan el parámetro `part` (ruta de la partición). Si se omite o está vacío, se usa `--auto`.

---

## Licencia

MIT
