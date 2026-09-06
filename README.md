# shaka-proxy

`shaka-proxy` es un proxy para convertir streams **MPEG-DASH** en una salida **MPEG-TS o HLS** que se pueda abrir directamente con VLC, ffplay u otros reproductores IPTV.

Está pensado sobre todo para streams DASH en directo. El proxy lee el MPD, selecciona las pistas de vídeo y audio, descarga los fragmentos que van apareciendo y, si el contenido usa **CENC/ClearKey**, utiliza **Shaka Packager** para descifrarlo con las claves que se hayan configurado. Después **FFmpeg** remultiplexa el resultado a MPEG-TS sin volver a codificar el vídeo.

La idea es esta:

```text
MPD + segmentos DASH
        │
        ▼
   shaka-proxy
        │
        ├── descarga y sigue el directo
        ├── Shaka Packager (si hay cifrado)
        └── FFmpeg
        │
        ▼
 MPEG-TS / HLS
        │
        ▼
       VLC
```

Al final, en vez de tener que abrir el MPD original y gestionar todo lo que hay detrás, el cliente puede usar una URL normal:

```text
http://127.0.0.1:8090/live/canal/stream.ts
```

o:

```text
http://127.0.0.1:8090/live/canal/index.m3u8
```

> `shaka-proxy` no obtiene claves DRM ni solicita licencias. Las claves ClearKey, tokens, cookies o credenciales que necesite un origen deben ser proporcionadas por el usuario. Úsalo únicamente con contenido para el que tengas autorización.

---

## Qué puede hacer

Actualmente el proxy incluye, entre otras cosas:

- DASH en directo a MPEG-TS.
- Salida HLS.
- Streams CENC/ClearKey mediante Shaka Packager.
- Streams DASH sin cifrar.
- Remux con FFmpeg usando `-c copy`, sin transcodificación.
- Selección automática de calidad con límite configurable.
- Selección de audio.
- Listas M3U como entrada.
- `#KODIPROP:inputstream.adaptive.license_key`.
- ClearKey en formato KID/KEY, JSON y JWK.
- Headers por canal.
- User-Agent y Referer personalizados.
- Tokens que se pueden actualizar desde un archivo sin reiniciar el proxy.
- Reintentos cuando el CDN todavía no tiene disponible un segmento.
- Caché compartida entre clientes.
- Un solo pipeline de procesamiento por canal aunque haya varios espectadores.
- Continuidad MPEG-TS independiente para cada conexión.
- HLS generado a partir de los mismos segmentos ya procesados.
- EPG XMLTV.
- Playlist M3U final generada automáticamente.
- Panel de estado y endpoint JSON.
- Límite de canales activos y cierre automático de sesiones inactivas.
- Proxy de salida, bind a una IP concreta y DNS de egress.
- Soporte para varias instancias usando el mismo código.

---

## Índice

- [Cómo funciona](#cómo-funciona)
- [DASH, CENC y ClearKey](#dash-cenc-y-clearkey)
- [Qué hace Shaka Packager](#qué-hace-shaka-packager)
- [Qué hace FFmpeg](#qué-hace-ffmpeg)
- [Sincronía de audio y vídeo](#sincronía-de-audio-y-vídeo)
- [Selección de vídeo](#selección-de-vídeo)
- [Selección de audio](#selección-de-audio)
- [Seguimiento del directo](#seguimiento-del-directo)
- [Reintentos y segmentos que todavía no existen](#reintentos-y-segmentos-que-todavía-no-existen)
- [Caché](#caché)
- [Varios clientes y continuidad MPEG-TS](#varios-clientes-y-continuidad-mpeg-ts)
- [Salida MPEG-TS](#salida-mpeg-ts)
- [Salida HLS](#salida-hls)
- [M3U de entrada](#m3u-de-entrada)
- [Headers por canal](#headers-por-canal)
- [Tokens dinámicos](#tokens-dinámicos)
- [Acceso al CDN](#acceso-al-cdn)
- [Playlist generada](#playlist-generada)
- [EPG / XMLTV](#epg--xmltv)
- [Estado](#estado)
- [Gestión de sesiones](#gestión-de-sesiones)
- [Instalación](#instalación)
- [Configuración](#configuración)
- [Arranque](#arranque)
- [Reverse proxy / HTTPS](#reverse-proxy--https)
- [Logs](#logs)
- [Tests](#tests)
- [Limitaciones](#limitaciones)
- [Seguridad](#seguridad)

---

# Cómo funciona

Cuando alguien abre un canal, `shaka-proxy` crea una sesión para ese canal o reutiliza la que ya exista.

A partir de ahí el proceso es más o menos este:

```text
Cliente pide /live/canal/stream.ts
              │
              ▼
       cargar / actualizar MPD
              │
              ▼
      elegir vídeo y audio
              │
              ▼
      localizar nuevos segmentos
              │
              ▼
        descargar fragmentos
              │
              ▼
        Shaka Packager
              │
              ▼
            FFmpeg
              │
              ▼
          segmento .ts
              │
        ┌─────┴─────┐
        ▼           ▼
    stream.ts      HLS
```

El MPD se sigue actualizando mientras el canal esté activo. Cuando aparecen nuevos fragmentos, se procesan en orden y se añaden a la ventana del canal.

Una cosa importante es que el procesamiento se hace **por canal**, no por cliente.

Si hay tres VLC viendo el mismo canal:

```text
                    ┌── VLC 1
DASH → Shaka → TS ──┼── VLC 2
                    └── VLC 3
```

No se lanzan tres pipelines completos contra el origen. Los clientes comparten los segmentos que ya ha preparado la sesión del canal.

---

# DASH, CENC y ClearKey

## DASH

Un stream DASH no suele ser un flujo único. El MPD describe varias pistas y una serie de fragmentos pequeños.

Por ejemplo:

```text
manifest.mpd

video/
  init.mp4
  100.m4s
  101.m4s
  102.m4s

audio/
  init.mp4
  300.m4s
  301.m4s
  302.m4s
```

El MPD también puede ofrecer varias calidades de vídeo, diferentes audios, codecs, bitrates, etc.

`shaka-proxy` analiza esa información y decide qué representación utilizar.

## CENC

Si el contenido está cifrado con CENC, los fragmentos descargados no se pueden mandar directamente a FFmpeg como si estuvieran en claro.

En el MPD puede aparecer un `default_KID`. Ese KID identifica la clave que corresponde a una pista.

```text
KID  ->  KEY
```

La KEY no se obtiene del MPD. Tiene que estar ya disponible en la configuración del canal.

## ClearKey

Una entrada M3U puede incluir una clave de esta forma:

```m3u
#KODIPROP:inputstream.adaptive.license_type=clearkey
#KODIPROP:inputstream.adaptive.license_key={00112233445566778899aabbccddeeff:ffeeddccbbaa99887766554433221100}
```

El parser también entiende otros formatos habituales, como un objeto JSON KID -> KEY o una estructura JWK ClearKey.

No es necesario que audio y vídeo utilicen la misma clave. Si el MPD usa KID diferentes, el proxy puede pasar varias claves a Shaka.

---

# Qué hace Shaka Packager

Shaka Packager se utiliza para procesar cada pista DASH.

Para un segmento concreto, el proxy descarga:

```text
init segment
+
media segment
```

y prepara un archivo temporal que Shaka pueda procesar.

Si el canal tiene claves configuradas, se construye el argumento de raw keys:

```text
label=k0:key_id=<KID>:key=<KEY>
label=k1:key_id=<KID>:key=<KEY>
...
```

y Packager se ejecuta con raw-key decryption.

El flujo queda así:

```text
init + fragmento cifrado
          │
          │ KID / KEY
          ▼
    Shaka Packager
          │
          ▼
     pista en claro
```

Si el stream no está cifrado, el mismo pipeline puede procesarlo sin activar el descifrado.

`shaka-proxy` no implementa el cifrado/descifrado CENC por su cuenta. Esa parte se deja a Shaka Packager.

---

# Qué hace FFmpeg

Después de Shaka quedan las pistas de vídeo y audio procesadas, pero siguen estando en el formato de entrada usado por DASH/fMP4.

FFmpeg se encarga de remultiplexarlas a MPEG-TS.

No se pretende transcodificar:

```text
H.264  -> H.264
HEVC   -> HEVC
audio  -> mismo audio
```

El pipeline usa `-c copy`, por lo que normalmente el coste de CPU es mucho menor que el de volver a codificar el canal y no se pierde calidad por una nueva compresión.

Para H.264 y HEVC se aplican los bitstream filters necesarios para llevar el vídeo de MP4 al formato esperado dentro de MPEG-TS.

---

# Sincronía de audio y vídeo

Audio y vídeo no tienen por qué empezar exactamente en el mismo timestamp dentro de cada fragmento.

El proxy intenta conservar el reloj original de DASH para no introducir pequeños saltos o drift al juntar los segmentos.

FFmpeg trabaja con los timestamps originales y aplica un offset común cuando hace falta mantenerlos positivos.

Esto es especialmente importante en un stream que puede quedarse abierto durante horas: un pequeño error repetido en cada fragmento terminaría notándose bastante.

---

# Selección de vídeo

El MPD puede ofrecer varias representaciones:

```text
576p
720p
1080p
2160p
```

El proxy las ordena principalmente por resolución y bitrate.

La altura máxima se controla con:

```json
"max_height": 1080
```

Con ese valor intentará utilizar la mejor representación disponible hasta 1080p.

Con:

```json
"max_height": 0
```

no se aplica un límite de altura.

También se ignoran representaciones que parecen estar destinadas a I-frames/trick mode en lugar de reproducción normal.

---

# Selección de audio

El proxy busca los `AdaptationSet` de audio del MPD.

Desde la versión 1.0.6 se incluyen todas las pistas de audio procesables, con una
representación de la mejor calidad disponible por pista. El español aparece
primero y se marca como predeterminado al remultiplexar; se reconocen `spa`,
`es` y variantes como `es-ES`. Si no hay español se conserva el orden del MPD.

Cuando el contenido está cifrado también se comprueba que exista una clave
conocida para el KID de cada pista. Los audios se incluyen tanto en `stream.ts`
como en los segmentos TS de HLS, con su etiqueta de idioma.

El reproductor puede aplicar su propia preferencia de idioma. Procesar más
pistas aumenta las descargas y el trabajo por segmento; un fallo de audio
mantiene los reintentos existentes para evitar publicar segmentos incompletos.

---

# Seguimiento del directo

Una vez iniciada una sesión, el proxy no vuelve a empezar desde cero con cada cliente.

Mantiene una posición dentro del directo y va actualizando el MPD para encontrar los nuevos segmentos.

Al empezar intenta entrar cerca del borde del directo, pero utilizando un segmento que ya debería estar cerrado y disponible en el CDN.

Después continúa en orden:

```text
... 100 101 102 103 104 105 ...
                ↑
              inicio
```

y pasa a:

```text
103 -> 104 -> 105 -> 106 -> ...
```

Si el MPD cambia de representación de vídeo o audio, la sesión cambia de generación y evita mezclar segmentos producidos con configuraciones incompatibles.

---

# Reintentos y segmentos que todavía no existen

En streaming en directo es bastante normal que un MPD anuncie un segmento y que algún nodo del CDN tarde un poco más en tenerlo listo.

Por eso una descarga no se da por perdida inmediatamente ante determinados errores.

Se contemplan respuestas temporales como:

```text
404
425
429
500
502
503
```

y se reintenta durante un pequeño periodo.

El número general de intentos se puede ajustar con:

```json
"cdn_retries": 3
```

Además, si un segmento falla repetidamente, `segment_failures_before_skip` limita cuánto tiempo puede bloquear el avance del canal.

```json
"segment_failures_before_skip": 2
```

Después de superar ese umbral se marca como fallido y el productor puede continuar con los siguientes segmentos.

---

# Caché

Los segmentos MPEG-TS terminados se mantienen en una pequeña ventana local.

```json
"hls_window": 6
```

Con una ventana de 6, el proxy conserva aproximadamente los últimos seis segmentos preparados.

Esa caché la comparten:

- los clientes de `stream.ts`;
- la salida HLS;
- nuevas conexiones que entren al canal.

Los segmentos antiguos se van eliminando junto con el estado interno que ya no hace falta, de forma que una sesión que permanezca abierta durante mucho tiempo no tenga que conservar todo el historial del canal.

También existe una pequeña caché para los initialization segments, que normalmente no cambian en cada fragmento.

---

# Varios clientes y continuidad MPEG-TS

Aunque el contenido procesado se comparta, cada conexión a `stream.ts` tiene su propio estado MPEG-TS.

Los paquetes TS utilizan continuity counters por PID. Si se concatena contenido sin tener esto en cuenta, algunos reproductores pueden interpretar que existen pérdidas o discontinuidades.

Por eso `shaka-proxy` ajusta los continuity counters al enviar los paquetes a cada cliente.

```text
segmentos compartidos
       │
       ├── VLC 1 -> continuidad propia
       ├── VLC 2 -> continuidad propia
       └── VLC 3 -> continuidad propia
```

Los `.ts` almacenados no se modifican; el ajuste se hace durante la entrega al cliente.

---

# Evitar procesar dos veces el mismo segmento

Cada segmento tiene sincronización propia.

Si el productor, HLS o varios clientes necesitan el mismo fragmento a la vez, el primero lo procesa y el resto reutiliza el resultado cuando está listo.

Esto evita lanzar Shaka y FFmpeg dos veces para el mismo timestamp.

---

# Salida MPEG-TS

La salida principal de un canal es:

```text
/live/<slug>/stream.ts
```

Ejemplo:

```bash
vlc http://127.0.0.1:8090/live/canal-demo/stream.ts
```

`stream.ts` es una respuesta HTTP continua.

Los segmentos se van escribiendo a medida que están disponibles y la entrega se acompasa para no mandar varios segundos de vídeo de golpe y quedarse esperando después.

La respuesta utiliza `video/mp2t` y desactiva el buffering de reverse proxies compatibles mediante:

```text
X-Accel-Buffering: no
```

---

# Salida HLS

También se genera:

```text
/live/<slug>/index.m3u8
```

Los segmentos de la playlist se sirven en:

```text
/live/<slug>/seg_<timestamp>.ts
```

El HLS utiliza la misma caché MPEG-TS que `stream.ts`, por lo que no hace falta volver a procesar el canal solo por utilizar la salida HLS.

La ventana se controla con `hls_window`.

Ejemplo:

```bash
vlc http://127.0.0.1:8090/live/canal-demo/index.m3u8
```

---

# M3U de entrada

La lista de canales se indica con:

```json
"source_m3u": "channels.m3u"
```

Una entrada DASH mínima puede ser:

```m3u
#EXTM3U

#EXTINF:-1 tvg-id="demo" tvg-name="Canal Demo",Canal Demo
https://example.invalid/live/manifest.mpd
```

Con ClearKey:

```m3u
#EXTM3U

#EXTINF:-1 tvg-id="demo" tvg-name="Canal Demo",Canal Demo
#KODIPROP:inputstream=inputstream.adaptive
#KODIPROP:inputstream.adaptive.manifest_type=mpd
#KODIPROP:inputstream.adaptive.license_type=clearkey
#KODIPROP:inputstream.adaptive.license_key={00112233445566778899aabbccddeeff:ffeeddccbbaa99887766554433221100}
https://example.invalid/live/manifest.mpd
```

Las líneas `#KODIPROP` pueden quedarse en la lista aunque el cliente final sea VLC. El proxy lee lo que necesita y no depende de que VLC entienda esas propiedades.

---

# Headers por canal

Se pueden indicar User-Agent y Referer mediante las opciones habituales de VLC:

```m3u
#EXTVLCOPT:http-user-agent=Mozilla/5.0
#EXTVLCOPT:http-referrer=https://example.invalid/
```

También se pueden añadir headers detrás de la URL:

```text
https://example.invalid/manifest.mpd|Header=Value|Otro-Header=Value
```

Estos headers se utilizan al acceder al MPD y a los recursos del origen según corresponda.

Para valores globales existen:

```json
"default_ua": "Mozilla/5.0 Proxy-Shaka",
"referer": "",
"origin": ""
```

No metas tokens, claves o credenciales reales en una playlist que vayas a subir al repositorio.

---

# Tokens dinámicos

Hay soporte para cargar `x-tcdn-token` desde un archivo independiente.

En `config.json`:

```json
"token_file": "token.json"
```

Ejemplo del archivo:

```json
{
  "access_token": "TOKEN",
  "access_token_exp": 1770000000
}
```

El proxy comprueba si el archivo cambia y vuelve a leer el token sin necesidad de reiniciar todo el proceso.

Esto viene bien cuando otro script se encarga de renovar la autenticación:

```text
script de login/renovación
          │
          ▼
      token.json
          │
          ▼
     shaka-proxy
```

`shaka-proxy` únicamente consume el valor. No implementa el login ni la renovación específica de ningún proveedor.

---

# Acceso al CDN

Las descargas del origen se hacen actualmente con `curl`.

Esto permite controlar algunas cosas que son útiles cuando el servidor tiene varias salidas de red:

- IPv4.
- HTTP/1.1.
- redirects.
- timeouts.
- reintentos.
- headers.
- bind a una IP/interfaz.
- proxy.
- resolución DNS específica.

## Proxy

```json
"proxy_url": "socks5://127.0.0.1:1080"
```

Si está configurado, las conexiones de origen pueden salir por ese proxy.

## Bind a una IP

```json
"egress_bind": "172.18.10.2"
```

Permite forzar la conexión hacia el CDN a través de una dirección local concreta.

Puede ser útil con WireGuard, policy routing o servidores con varias interfaces.

## DNS de egress

```json
"egress_dns": "1.1.1.1"
```

Cuando se utiliza junto con `egress_bind`, la resolución puede hacerse usando ese camino de salida y la IP obtenida se pasa después a `curl`.

---

# Playlist generada

El proxy genera automáticamente:

```text
/playlist.m3u8
```

Las entradas DASH se sustituyen por la URL local del proxy.

Por ejemplo:

```text
https://cdn.example/manifest.mpd
```

pasa a ser algo como:

```text
http://127.0.0.1:8090/live/canal-demo/stream.ts
```

Las entradas de la M3U que no sean DASH pueden mantenerse como passthrough con su URL original.

De esta manera la misma lista puede mezclar canales procesados por `shaka-proxy` y canales normales.

---

# Slugs

La URL de cada canal se genera a partir de su nombre.

Por ejemplo:

```text
La 1 HD
```

se convierte en:

```text
la-1-hd
```

y queda disponible como:

```text
/live/la-1-hd/stream.ts
```

Si dos canales terminan generando el mismo slug, se añaden sufijos para que sigan siendo únicos.

---

# Recarga de channels.m3u

La playlist de entrada se vuelve a leer cuando cambia su fecha de modificación.

Eso permite editar `channels.m3u` y que la lista generada y los endpoints de estado recojan los cambios sin tener que reiniciar necesariamente el servicio completo.

---

# EPG / XMLTV

Si la cabecera M3U tiene `url-tvg`, el proxy puede descargar una guía XMLTV:

```m3u
#EXTM3U url-tvg="https://example.invalid/epg.xml"
```

También acepta XMLTV comprimido:

```m3u
#EXTM3U url-tvg="https://example.invalid/epg.xml.gz"
```

El EPG intenta relacionar los canales con la M3U usando `tvg-id` y nombre.

La comparación de nombres es tolerante con diferencias simples de mayúsculas, acentos, espacios y signos.

La guía se mantiene en memoria, se actualiza periódicamente y se puede consultar en:

```text
/epg
```

---

# Estado

Hay dos endpoints:

```text
/status
/status.json
```

`/status` muestra una página sencilla para ver qué está haciendo el proxy.

Entre los datos disponibles están:

- canales cargados;
- sesiones activas;
- espectadores;
- clientes conectados;
- IP y User-Agent;
- tiempo conectado;
- segmentos en caché;
- resolución y framerate seleccionados;
- estado del productor;
- estado del token;
- tiempo de actividad/inactividad.

`/status.json` devuelve la misma clase de información en un formato más cómodo para scripts o monitorización.

---

# Gestión de sesiones

Los canales se crean bajo demanda.

La primera petición a:

```text
/live/canal/stream.ts
```

crea una sesión. Las siguientes reutilizan esa misma sesión mientras siga viva.

El número máximo de canales que pueden estar activos al mismo tiempo se controla con:

```json
"max_channels": 8
```

Si se llega al límite, el proxy intenta cerrar primero una sesión inactiva.

Las sesiones sin clientes se eliminan después de:

```json
"idle_seconds": 45
```

Cuando una sesión se cierra también se detienen su productor y workers y se limpia su caché temporal.

---

# `public_base`

Por defecto las URLs se pueden construir a partir de la propia petición, pero si el proxy está detrás de un dominio o un reverse proxy conviene configurar:

```json
"public_base": "https://tv.example.com"
```

Entonces `/playlist.m3u8` utilizará:

```text
https://tv.example.com/live/canal/stream.ts
```

También puede incluir un prefijo:

```json
"public_base": "https://example.com/proxy1"
```

Esto permite montar varias instancias detrás del mismo dominio.

---

# Varias instancias

`PROXY_SHAKA_HOME` permite reutilizar el mismo código con configuraciones independientes.

Por ejemplo:

```text
/opt/shaka-proxy/             # código

/etc/shaka-proxy/a/
    config.json
    channels.m3u
    token.json
    hls/
    logs/

/etc/shaka-proxy/b/
    config.json
    channels.m3u
    token.json
    hls/
    logs/
```

Cada instancia puede tener su propio puerto, M3U, token, caché y logs.

---

# Instalación

## Requisitos

- Python 3.11 o superior.
- `curl`.
- FFmpeg.
- Shaka Packager.
- Un compilador C y las cabeceras de Python para el build actual.
- Dependencias de `requirements.txt`.

Shaka Packager no se incluye en el repositorio.

Puedes dejarlo en:

```text
bin/packager
```

o configurar otra ruta.

Comprueba primero que todo está disponible:

```bash
python3 --version
curl --version
ffmpeg -version
bin/packager --version
```

## Clonar el repo

```bash
git clone https://github.com/mateodd1/shaka-proxy.git
cd shaka-proxy
```

Crear el entorno virtual:

```bash
python3 -m venv .venv
```

El proyecto incluye `build.sh`:

```bash
./build.sh
```

Después copia los ejemplos:

```bash
cp config.example.json config.json
cp channels.example.m3u channels.m3u
```

y edítalos con tu configuración.

---

# Configuración

Ejemplo:

```json
{
  "listen_host": "127.0.0.1",
  "listen_port": 8090,
  "public_base": "",

  "source_m3u": "channels.m3u",
  "token_file": "token.json",

  "proxy_url": "",
  "egress_bind": "",
  "egress_dns": "",

  "referer": "",
  "origin": "",
  "default_ua": "Mozilla/5.0 Proxy-Shaka",

  "max_height": 1080,

  "idle_seconds": 45,
  "max_channels": 8,
  "origin_concurrency": 4,

  "cdn_retries": 3,
  "remux_timeout": 20,
  "segment_failures_before_skip": 2,

  "hls_window": 6,

  "ffmpeg": "/usr/bin/ffmpeg",
  "packager": "bin/packager",

  "hls_dir": "hls",
  "log_dir": "logs"
}
```

Las opciones principales:

| Opción | Uso |
| --- | --- |
| `listen_host` | IP donde escucha el servidor. |
| `listen_port` | Puerto HTTP. |
| `public_base` | URL pública usada al generar enlaces. |
| `source_m3u` | M3U de entrada. |
| `token_file` | Archivo del token dinámico. |
| `proxy_url` | Proxy para el acceso al origen. |
| `egress_bind` | IP local usada para salir hacia el CDN. |
| `egress_dns` | DNS utilizado con el egress. |
| `referer` | Referer global. |
| `origin` | Origin global. |
| `default_ua` | User-Agent por defecto. |
| `max_height` | Resolución vertical máxima preferida. |
| `idle_seconds` | Tiempo antes de cerrar un canal sin clientes. |
| `max_channels` | Máximo de sesiones activas. |
| `origin_concurrency` | Límite de trabajo simultáneo contra el origen. |
| `cdn_retries` | Reintentos para peticiones al CDN. |
| `remux_timeout` | Timeout del remux. |
| `segment_failures_before_skip` | Fallos antes de saltar un segmento. |
| `hls_window` | Tamaño de la ventana HLS/caché. |
| `ffmpeg` | Ruta a FFmpeg. |
| `packager` | Ruta a Shaka Packager. |
| `hls_dir` | Directorio temporal de segmentos. |
| `log_dir` | Directorio de logs. |

---

# Arranque

Desde la raíz del proyecto:

```bash
PROXY_SHAKA_HOME="$PWD" .venv/bin/python run.py
```

Con el puerto del ejemplo:

```text
http://127.0.0.1:8090/
```

Endpoints útiles:

| URL | Descripción |
| --- | --- |
| `/playlist.m3u8` | Playlist lista para usar. |
| `/live/<slug>/stream.ts` | MPEG-TS continuo. |
| `/live/<slug>/index.m3u8` | HLS. |
| `/live/<slug>/seg_<t>.ts` | Segmentos HLS. |
| `/status` | Estado en web. |
| `/status.json` | Estado en JSON. |
| `/epg` | Guía XMLTV. |

Por ejemplo:

```bash
vlc http://127.0.0.1:8090/playlist.m3u8
```

---

# Reverse proxy / HTTPS

Una configuración habitual es dejar `shaka-proxy` escuchando solo en localhost:

```json
"listen_host": "127.0.0.1"
```

y poner Caddy o Nginx delante:

```text
Internet
   │
 HTTPS
   ▼
Caddy / Nginx
   │
 HTTP
   ▼
shaka-proxy
```

Si la URL externa no coincide con la interna, configura `public_base`.

Para `stream.ts` es importante que el reverse proxy no acumule demasiado contenido antes de enviarlo al cliente.

---

# Logs

Los logs se guardan en:

```text
<log_dir>/proxy-shaka.log
```

Hay rotación automática del fichero para que no crezca indefinidamente.

En ellos se pueden ver cosas como:

- inicio y cierre de sesiones;
- cambios de representación;
- conexiones de clientes;
- reintentos del CDN;
- segmentos fallidos;
- errores de Shaka;
- errores de FFmpeg;
- gaps y stalls.

---

# Algunas decisiones de diseño

Hay varias cosas del proyecto que están hechas específicamente pensando en canales en directo.

### No publicar un segmento a medias

Cada segmento se procesa primero dentro de un directorio temporal. El `.ts` final solo aparece cuando Shaka y FFmpeg han terminado correctamente.

Así un cliente no debería poder abrir un fichero que todavía se esté escribiendo.

### Cancelar procesos correctamente

Si una tarea que estaba ejecutando Shaka o FFmpeg expira o se cancela, el proxy intenta terminar también el subprocess.

La idea es no dejar procesos huérfanos después de cerrar una sesión.

### Mantener el orden

Los workers pueden preparar trabajo en paralelo, pero los segmentos se publican siguiendo el orden del directo.

### Recuperarse de un fallo puntual

Que falle un fragmento no debería tirar todo el canal. Se reintenta y, cuando el fallo persiste más de lo razonable, se puede saltar ese timestamp y continuar.

---

# Tests

Para ejecutar los tests directamente contra el código fuente:

```bash
PYTHONPATH=src .venv/bin/python -m unittest discover -s tests -v
```

O contra el módulo instalado:

```bash
.venv/bin/python -m unittest discover -s tests -v
```

Algunas pruebas multimedia necesitan FFmpeg/Shaka disponibles. Las pruebas que no los necesitan pueden ejecutarse de forma independiente.

---

# Limitaciones

El proxy está hecho alrededor de los MPD y casos de uso para los que lo he ido desarrollando. No pretende implementar absolutamente toda la especificación MPEG-DASH.

A día de hoy:

- el parser está orientado principalmente a `SegmentTemplate` y `SegmentTimeline`;
- el flujo principal trabaja con segmentos identificados por `$Time$`;
- la salida normal utiliza una pista de audio;
- se prioriza `lang="spa"`;
- el remux de vídeo está pensado para H.264 y HEVC;
- no hay transcodificación;
- no hay cliente de licencias DRM;
- no se obtienen claves;
- no se implementan logins ni renovación de tokens específicos de proveedores;
- FFmpeg y Shaka Packager siguen siendo dependencias externas.

Si un MPD utiliza una estructura bastante distinta, seguramente haya que ampliar el parser.

---

# Seguridad

No subas al repositorio archivos reales que contengan:

- claves;
- tokens;
- cookies;
- credenciales;
- URLs privadas.

Para eso están:

```text
config.example.json
channels.example.m3u
```

y los archivos reales deberían quedar fuera de Git.

---

## Resumen rápido

Si solo quieres entender qué hace el proyecto, es esto:

```text
DASH
  │
  ├── si está cifrado: Shaka Packager + ClearKey
  │
  └── si no: procesamiento normal
  │
  ▼
FFmpeg (-c copy)
  │
  ▼
MPEG-TS / HLS
  │
  ▼
VLC
```

Todo lo demás —MPD, segmentos, caché, reintentos, clientes, EPG, tokens y estado— lo gestiona `shaka-proxy` por detrás.
