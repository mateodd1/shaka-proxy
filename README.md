# proxy-shaka

Proxy independiente DASH → MPEG-TS para VLC y otros clientes. Descarga
segmentos con reintentos, descifra con Shaka Packager y remultiplexa con FFmpeg
sin recodificar.

Incluye caché compartida para varios clientes, entrada en el borde, continuidad
MPEG-TS por conexión, recuperación ordenada de segmentos y cierre limpio.
La caché conserva los segmentos preparados aunque la CDN devuelva un manifiesto
anterior. El audio y el vídeo mantienen su reloj común entre fragmentos.
La guía XMLTV ignora los canales ajenos a la lista, también en la versión
compilada con Cython.

## Instalación

Requiere Python 3.11+, un compilador C y las cabeceras de Python para compilar,
curl, FFmpeg y [Shaka Packager](https://github.com/shaka-project/shaka-packager).
Instala Packager en `bin/packager` con permiso de ejecución o indica su ruta
absoluta en `config.json`. El binario no se distribuye en esta repo.

```bash
python3 -m venv .venv
./build.sh
cp config.example.json config.json
cp channels.example.m3u channels.m3u
```

Edita `config.json` y `channels.m3u`. No guardes credenciales ni tokens en Git.
El ejemplo usa `/usr/bin/ffmpeg`; ajusta la ruta a tu instalación.

El formato M3U admite las propiedades habituales de Kodi/InputStream Adaptive,
incluidas `manifest_type=mpd`, `license_type=clearkey` y
`license_key={kid:key}`. Las directivas `#KODIPROP` se consumen localmente y no
se envían al cliente VLC. Para cada entrada DASH, el proxy publica la salida
descifrada en `/live/<slug>/stream.ts`; también puedes usar el índice HLS
`/live/<slug>/index.m3u8`.

Ejemplo

```m3u
#EXTINF:-1 tvg-id="Canal HD" tvg-name="Canal",Canal
#KODIPROP:inputstream=inputstream.adaptive
#KODIPROP:inputstream.adaptive.manifest_type=mpd
#KODIPROP:inputstream.adaptive.license_type=clearkey
#KODIPROP:inputstream.adaptive.license_key={kid hexadecimal:key hexadecimal}
https://cdn.example/live/index.mpd/
```

## Ejecución

```bash
PROXY_SHAKA_HOME="$PWD" .venv/bin/python run.py
```

Expone `/playlist.m3u8`, `/live/<slug>/stream.ts`, `/live/<slug>/index.m3u8`,
`/status`, `/status.json` y `/epg`. Abre `http://127.0.0.1:8090/playlist.m3u8`
en VLC. Si lo publicas por HTTPS, configura `public_base` con la URL externa,
incluido su prefijo si lo hay. El estado se actualiza sin recargar la página.

Para cargar la guía, declara `url-tvg="https://example.com/guide.xml.gz"` en la
línea `#EXTM3U` de tu lista. Se admiten XMLTV y XMLTV comprimido con gzip.

## Varias instancias y HTTPS

Cada instancia necesita su propio directorio de configuración, lista, token,
caché y logs, un `listen_port` libre y un `public_base` distinto.
`PROXY_SHAKA_HOME` selecciona ese directorio; las rutas relativas de la
configuración se resuelven dentro de él. El código y los binarios pueden
compartirse.

En `deploy/` se incluyen ejemplos de un servicio systemd por instancia y rutas
Caddy/Nginx para publicar dos instancias con prefijos distintos. Consulta
[deploy/README.md](deploy/README.md) para instalarlos.

`token_file` es un archivo local opcional con `access_token` y
`access_token_exp` (fecha de caducidad Unix), para orígenes que usan
`x-tcdn-token`. Si no existe, se usan las cabeceras de la lista. El proxy recarga
el token cuando cambia; su obtención o renovación corresponde a la integración
de cada origen. Esta repo no incluye un generador de cuentas ni una tarea cron
específica de un proveedor.

## Pruebas

```bash
PYTHONPATH=src .venv/bin/python -m unittest discover -s tests -v
.venv/bin/python -m unittest discover -s tests -v
```

La primera ejecución prueba el fuente y la segunda el módulo compilado. La
prueba multimedia genera su propio vídeo y audio; se omite si faltan los
binarios. La prueba EPG utiliza una guía ficticia y no necesita acceso a la red.

El repositorio no contiene listas reales, tokens, credenciales ni cachés.
