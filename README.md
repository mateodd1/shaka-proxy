# proxy-shaka

Proxy independiente DASH → MPEG-TS para VLC y otros clientes. Descarga
segmentos con reintentos, descifra con Shaka Packager y remultiplexa con FFmpeg
sin recodificar.

Incluye caché compartida para varios clientes, entrada en el borde, continuidad
MPEG-TS por conexión, recuperación ordenada de segmentos y cierre limpio.

## Instalación

Requiere Python 3.11+, FFmpeg y [Shaka Packager](https://github.com/shaka-project/shaka-packager).

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
cp config.example.json config.json
cp channels.example.m3u channels.m3u
```

Edita `config.json` y `channels.m3u`. No guardes credenciales ni tokens en Git.

## Ejecución

```bash
PROXY_SHAKA_HOME="$PWD" .venv/bin/python run.py
```

Expone `/live/<slug>/stream.ts`, `/live/<slug>/index.m3u8`, `/status` y
`/status.json`.

## Pruebas

```bash
PYTHONPATH=src .venv/bin/python -m unittest discover -s tests -v
```

El repositorio no contiene listas reales, tokens, credenciales ni cachés.
