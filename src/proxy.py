#!/usr/bin/env python3
"""DASH (cabeceras + ClearKey) -> HLS on-demand, con DVR de 2h."""
from __future__ import annotations

import asyncio
import base64
import gzip
import html
import io
import json
import logging
from logging.handlers import RotatingFileHandler
import os
import re
import shutil
import socket
import tempfile
import time
import unicodedata
import uuid
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional
from urllib.parse import quote, urljoin, urlparse
from xml.etree import ElementTree as ET
from zoneinfo import ZoneInfo

from aiohttp import ClientTimeout, TCPConnector, web
from aiohttp.abc import AbstractResolver
from aiohttp_socks import ProxyConnector, ProxyError, ProxyTimeoutError
import aiohttp

def _home() -> Path:
    env = os.environ.get("PROXY_SHAKA_HOME")
    if env:
        return Path(env)
    here = Path(__file__).resolve().parent
    if (here / "config.json").is_file():
        return here
    cwd = Path.cwd()
    if (cwd / "config.json").is_file():
        return cwd
    return Path.cwd()


SCRIPT_DIR = _home()
CONFIG_PATH = SCRIPT_DIR / "config.json"
MPD_NS = "urn:mpeg:dash:schema:mpd:2011"
CENC_NS = "urn:mpeg:cenc:2013"
ET.register_namespace("", MPD_NS)
ET.register_namespace("cenc", CENC_NS)
ET.register_namespace("mspr", "urn:microsoft:playready")
ET.register_namespace("xsi", "http://www.w3.org/2001/XMLSchema-instance")

log = logging.getLogger("proxy-shaka")


def dns_query_a(name: str, server: str, bind: str, timeout: float = 3.0) -> Optional[str]:
    tid = int.from_bytes(os.urandom(2), "big")
    q = tid.to_bytes(2, "big") + b"\x01\x00\x00\x01\x00\x00\x00\x00\x00\x00"
    for part in name.strip(".").split("."):
        label = part.encode("idna")
        q += bytes([len(label)]) + label
    q += b"\x00\x00\x01\x00\x01"
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.settimeout(timeout)
        sock.bind((bind, 0))
        sock.sendto(q, (server, 53))
        data, _src = sock.recvfrom(512)
    finally:
        sock.close()
    if len(data) < 12:
        return None
    ancount = int.from_bytes(data[6:8], "big")
    i = 12
    qd = int.from_bytes(data[4:6], "big")
    for _ in range(qd):
        while i < len(data) and data[i] != 0:
            if data[i] & 0xC0 == 0xC0:
                i += 2
                break
            i += 1 + data[i]
        else:
            i += 1
        i += 4
    for _ in range(ancount):
        if i >= len(data):
            break
        if data[i] & 0xC0 == 0xC0:
            i += 2
        else:
            while i < len(data) and data[i] != 0:
                i += 1 + data[i]
            i += 1
        if i + 10 > len(data):
            break
        typ = int.from_bytes(data[i : i + 2], "big")
        rdlen = int.from_bytes(data[i + 8 : i + 10], "big")
        i += 10
        if typ == 1 and rdlen == 4 and i + 4 <= len(data):
            return socket.inet_ntoa(data[i : i + 4])
        i += rdlen
    return None


class EgressResolver(AbstractResolver):
    def __init__(self, bind: str, dns: str):
        self.bind = bind
        self.dns = dns

    async def resolve(self, host: str, port: int = 0, family: int = socket.AF_INET):
        loop = asyncio.get_running_loop()
        ip = None
        try:
            ip = await loop.run_in_executor(None, dns_query_a, host, self.dns, self.bind)
        except Exception:
            ip = None
        if not ip:
            infos = await loop.getaddrinfo(host, port, family=socket.AF_INET, type=socket.SOCK_STREAM)
            ip = infos[0][4][0]
        return [
            {
                "hostname": host,
                "host": ip,
                "port": port,
                "family": socket.AF_INET,
                "proto": socket.IPPROTO_TCP,
                "flags": socket.AI_NUMERICHOST,
            }
        ]

    async def close(self) -> None:
        return None


def socks_ipv4_url(url: str) -> str:
    parsed = urlparse(url)
    host = parsed.hostname
    port = parsed.port or 1080
    if not host:
        return url
    infos = socket.getaddrinfo(host, port, family=socket.AF_INET, type=socket.SOCK_STREAM)
    ip = infos[0][4][0]
    user = parsed.username
    password = parsed.password
    auth = ""
    if user is not None:
        auth = quote(user, safe="")
        if password is not None:
            auth += ":" + quote(password, safe="")
        auth += "@"
    log.info("socks %s -> %s:%s", host, ip, port)
    return f"{parsed.scheme}://{auth}{ip}:{port}"


def empty_dir(path: Path) -> None:
    """Delete files and subdirs inside path; keep path itself."""
    try:
        path.mkdir(parents=True, exist_ok=True)
    except OSError:
        return
    try:
        children = list(path.iterdir())
    except OSError:
        return
    for child in children:
        try:
            if child.is_symlink() or child.is_file():
                child.unlink()
            else:
                shutil.rmtree(child, ignore_errors=True)
        except OSError:
            pass


async def run_cmd(cmd: list[str], timeout: float = 15.0) -> tuple[int, bytes]:
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        _, err = await asyncio.wait_for(proc.communicate(), timeout)
        return proc.returncode or 0, err or b""
    except (asyncio.TimeoutError, TimeoutError):
        if proc.returncode is None:
            proc.kill()
            try:
                await proc.wait()
            except Exception:
                pass
        return 124, b"timeout"
    except (asyncio.CancelledError, Exception):
        if proc.returncode is None:
            proc.kill()
            try:
                await proc.wait()
            except Exception:
                pass
        raise


async def run_cmd_out(cmd: list[str], timeout: float = 16.0) -> tuple[int, bytes, bytes]:
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        out, err = await asyncio.wait_for(proc.communicate(), timeout)
        return proc.returncode or 0, out or b"", err or b""
    except (asyncio.TimeoutError, TimeoutError):
        if proc.returncode is None:
            proc.kill()
            try:
                await proc.wait()
            except Exception:
                pass
        return 124, b"", b"timeout"
    except (asyncio.CancelledError, Exception):
        if proc.returncode is None:
            proc.kill()
            try:
                await proc.wait()
            except Exception:
                pass
        raise


def qtag(tag: str) -> str:
    return f"{{{MPD_NS}}}{tag}"


def cenc_attr(name: str) -> str:
    return f"{{{CENC_NS}}}{name}"


def load_config() -> dict:
    with open(CONFIG_PATH, "r", encoding="utf-8") as fh:
        cfg = json.load(fh)
    for key in ("source_m3u", "token_file", "packager", "hls_dir", "log_dir"):
        value = cfg.get(key)
        if value and not Path(value).is_absolute():
            cfg[key] = str(SCRIPT_DIR / value)
    return cfg


def b64url_to_hex(value: str) -> str:
    pad = "=" * ((4 - len(value) % 4) % 4)
    return base64.urlsafe_b64decode(value + pad).hex()


def only_hex(value: str) -> str:
    return re.sub(r"[^0-9a-fA-F]", "", value).lower()


def parse_keys(raw: str) -> dict[str, str]:
    raw = (raw or "").strip()
    if not raw:
        return {}
    keys: dict[str, str] = {}
    if '"kty"' in raw:
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            return {}
        for item in data.get("keys", []):
            try:
                kid = b64url_to_hex(item["kid"])
                key = b64url_to_hex(item["k"])
            except Exception:
                continue
            if kid and key:
                keys[kid] = key
        return keys
    if raw.startswith("{") and '"' in raw:
        try:
            obj = json.loads(raw)
            if isinstance(obj, dict) and "keys" not in obj:
                for kid, key in obj.items():
                    hkid, hkey = only_hex(str(kid)), only_hex(str(key))
                    if hkid and hkey:
                        keys[hkid] = hkey
                return keys
        except json.JSONDecodeError:
            pass
    inner = raw.strip().strip("{}")
    for part in inner.split(","):
        if ":" not in part:
            continue
        kid, key = part.split(":", 1)
        hkid, hkey = only_hex(kid), only_hex(key)
        if hkid and hkey:
            keys[hkid] = hkey
    return keys


def slugify(name: str) -> str:
    s = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode("ascii")
    s = re.sub(r"[^a-zA-Z0-9]+", "-", s).strip("-").lower()
    return s or "ch"


def pick_key(keys: dict[str, str], kid: Optional[str]) -> Optional[str]:
    if not keys:
        return None
    if kid:
        hkid = kid.replace("-", "").lower()
        if hkid in keys:
            return keys[hkid]
    return next(iter(keys.values()))


def shaka_keys_arg(keys: dict[str, str]) -> str:
    parts = []
    for i, (kid, key) in enumerate(keys.items()):
        parts.append(f"label=k{i}:key_id={kid}:key={key}")
    return ",".join(parts)


def adaptation_kid(aset: ET.Element) -> Optional[str]:
    for cp in aset.findall(qtag("ContentProtection")):
        kid = cp.get(cenc_attr("default_KID")) or cp.get("default_KID")
        if kid:
            return kid.replace("-", "").lower()
    return None


def is_iframe_set(aset: ET.Element) -> bool:
    if aset.get("maxPlayoutRate"):
        return True
    for rep in aset.findall(qtag("Representation")):
        if "iframe" in (rep.get("id") or "").lower():
            return True
    return False


def expand_timeline(stempl: ET.Element) -> tuple[list[tuple[int, int]], int]:
    timescale = int(stempl.get("timescale") or 1)
    timeline = stempl.find(qtag("SegmentTimeline"))
    if timeline is None:
        return [], timescale
    entries: list[tuple[int, int]] = []
    t = 0
    for s in timeline.findall(qtag("S")):
        d = int(s.get("d") or 0)
        r = int(s.get("r") or 0)
        if s.get("t") is not None:
            t = int(s.get("t"))
        for _ in range(r + 1):
            entries.append((t, d))
            t += d
    return entries[-16:], timescale


def fill_template(tmpl: str, rep_id: str, t: Optional[int] = None) -> str:
    out = tmpl.replace("$RepresentationID$", rep_id)
    if t is not None:
        out = out.replace("$Time$", str(t))
    return out


@dataclass
class DashTrack:
    kind: str
    rep_id: str
    timescale: int
    init_rel: str
    media_tmpl: str
    kid: Optional[str]
    entries: list[tuple[int, int]]
    codecs: str = ""
    height: int = 0
    bandwidth: int = 0
    fps: float = 0.0
    lang: str = ""


def parse_framerate(raw: Optional[str]) -> float:
    raw = (raw or "").strip()
    if not raw:
        return 0.0
    try:
        if "/" in raw:
            num, den = raw.split("/", 1)
            den_f = float(den)
            if den_f == 0:
                return 0.0
            return float(num) / den_f
        return float(raw)
    except ValueError:
        return 0.0


def format_quality(height: int, fps: float) -> str:
    if height <= 0:
        return "—"
    if fps <= 0:
        return f"{height}p"
    if abs(fps - round(fps)) < 0.05:
        return f"{height}p{int(round(fps))}"
    return f"{height}p{fps:.2f}".rstrip("0").rstrip(".")


def _rep_rank(rep: ET.Element) -> tuple[int, int]:
    return (int(rep.get("height") or 0), int(rep.get("bandwidth") or 0))


def iso639(lang: str) -> str:
    code = (lang or "").strip().lower()
    if len(code) >= 3 and code[:3].isalpha():
        return code[:3]
    return "und"


def select_tracks(
    mpd: bytes,
    max_height: int,
    keys: Optional[dict[str, str]] = None,
) -> tuple[Optional[DashTrack], list[DashTrack]]:
    """Pick the highest video rendition across all AdaptationSets.

    Some providers split AVC/HEVC into two ladders that both advertise the same
    maxHeight (1080). Ranking by AdaptationSet maxHeight therefore selected
    the last set, which is the SD ladder (576p). Rank Representations by
    actual height then bandwidth instead. max_height <= 0 means no cap.
    """
    root = ET.fromstring(mpd)
    video_sets, audio_sets = [], []
    for aset in root.iter(qtag("AdaptationSet")):
        if is_iframe_set(aset):
            continue
        ctype = aset.get("contentType") or ""
        mime = aset.get("mimeType") or ""
        if ctype == "video" or mime.startswith("video/"):
            video_sets.append(aset)
        elif ctype == "audio" or mime.startswith("audio/"):
            audio_sets.append(aset)

    def from_pair(aset: ET.Element, kind: str, rep: ET.Element) -> Optional[DashTrack]:
        stempl = aset.find(qtag("SegmentTemplate"))
        if stempl is None:
            return None
        entries, timescale = expand_timeline(stempl)
        return DashTrack(
            kind=kind,
            rep_id=rep.get("id") or "",
            timescale=timescale,
            init_rel=fill_template(stempl.get("initialization") or "", rep.get("id") or ""),
            media_tmpl=stempl.get("media") or "",
            kid=adaptation_kid(aset),
            entries=entries,
            codecs=rep.get("codecs") or "",
            height=int(rep.get("height") or 0),
            bandwidth=int(rep.get("bandwidth") or 0),
            fps=parse_framerate(rep.get("frameRate") or aset.get("frameRate") or aset.get("maxFrameRate")),
            lang=aset.get("lang") or "",
        )

    def best_rep(aset: ET.Element, cap: int) -> Optional[ET.Element]:
        reps = list(aset.findall(qtag("Representation")))
        if cap > 0:
            under = [r for r in reps if int(r.get("height") or 0) <= cap]
            if under:
                reps = under
        if not reps:
            return None
        return max(reps, key=_rep_rank)

    video = None
    best_key = (-1, -1)
    for aset in video_sets:
        rep = best_rep(aset, max_height)
        if rep is None:
            continue
        key = _rep_rank(rep)
        if key > best_key:
            track = from_pair(aset, "video", rep)
            if track is not None:
                video, best_key = track, key

    spa_sets = [aset for aset in audio_sets if aset.get("lang") == "spa"]
    other_sets = [aset for aset in audio_sets if aset.get("lang") != "spa"]
    audios: list[DashTrack] = []
    seen: set[str] = set()
    for aset in spa_sets + other_sets:
        kid = adaptation_kid(aset)
        if keys and kid and kid not in keys:
            continue
        arep = best_rep(aset, 0)
        if arep is None:
            continue
        rid = arep.get("id") or ""
        if rid in seen:
            continue
        track = from_pair(aset, "audio", arep)
        if track is None:
            continue
        seen.add(rid)
        audios.append(track)
    spa = [a for a in audios if (a.lang or "").lower() == "spa"]
    audios = spa[:1] or audios[:1]
    return video, audios


def nearest_audio(video: DashTrack, t: int, audio: DashTrack) -> Optional[tuple[int, int]]:
    if not audio.entries:
        return None
    vsec = t / video.timescale
    return min(audio.entries, key=lambda e: abs(e[0] / audio.timescale - vsec))


@dataclass
class Channel:
    slug: str
    name: str
    extinf: str
    url: str
    keys: dict[str, str] = field(default_factory=dict)
    ua: str = ""
    referer: str = ""
    extra_headers: dict[str, str] = field(default_factory=dict)
    is_dash: bool = True
    logo: str = ""
    tvg_id: str = ""


def parse_logo(extinf: str) -> str:
    m = re.search(r'tvg-logo="([^"]+)"', extinf or "", re.I)
    if not m:
        return ""
    url = m.group(1).strip()
    if url.startswith("https://") or url.startswith("http://"):
        return url
    return ""


def parse_tvg_id(extinf: str) -> str:
    m = re.search(r'tvg-id="([^"]+)"', extinf or "", re.I)
    return m.group(1).strip() if m else ""


def normalize_epg_name(value: str) -> str:
    value = unicodedata.normalize("NFKD", value).encode("ascii", "ignore").decode("ascii")
    return re.sub(r"[^a-z0-9]+", "", value.lower())


class Playlist:
    def __init__(self, path: str):
        self.path = Path(path)
        self.mtime = 0.0
        self.header = "#EXTM3U"
        self.channels: list[Channel] = []
        self.by_slug: dict[str, Channel] = {}

    def maybe_reload(self, default_ua: str, default_referer: str) -> None:
        try:
            mtime = self.path.stat().st_mtime
        except OSError:
            return
        if mtime == self.mtime and self.channels:
            return
        self.mtime = mtime
        self._parse(default_ua, default_referer)
        log.info("playlist recargada: %s canales", len(self.channels))

    def _parse(self, default_ua: str, default_referer: str) -> None:
        text = self.path.read_text(encoding="utf-8", errors="replace")
        header = "#EXTM3U"
        channels: list[Channel] = []
        used: set[str] = set()
        extinf = ""
        keys: dict[str, str] = {}
        ua = default_ua
        referer = default_referer
        extra: dict[str, str] = {}
        for raw in text.splitlines():
            line = raw.strip()
            if line.startswith("#EXTM3U"):
                header = line
                continue
            if line.startswith("#EXTINF:"):
                extinf = line
                continue
            if line.startswith("#KODIPROP:inputstream.adaptive.license_key="):
                keys = parse_keys(line.split("=", 1)[1])
                continue
            if line.startswith("#EXTVLCOPT:"):
                opt = line[len("#EXTVLCOPT:") :]
                if opt.lower().startswith("http-user-agent="):
                    ua = opt.split("=", 1)[1].strip() or ua
                elif opt.lower().startswith("http-referrer="):
                    referer = opt.split("=", 1)[1].strip() or referer
                continue
            if not line or line.startswith("#"):
                continue
            url, pipe_headers = split_url_headers(line)
            extra.update(pipe_headers)
            name = "canal"
            if extinf:
                name = extinf.rsplit(",", 1)[-1].strip() or name
            slug = unique_slug(name, used)
            channels.append(
                Channel(
                    slug=slug,
                    name=name,
                    extinf=extinf or f"#EXTINF:-1,{name}",
                    url=url,
                    keys=keys,
                    ua=ua or default_ua,
                    referer=referer,
                    extra_headers=dict(extra),
                    is_dash=".mpd" in url.lower(),
                    logo=parse_logo(extinf),
                    tvg_id=parse_tvg_id(extinf),
                )
            )
            extinf = ""
            keys = {}
            ua = default_ua
            referer = default_referer
            extra = {}
        self.header = header
        self.channels = channels
        self.by_slug = {ch.slug: ch for ch in channels}

    def epg_url(self) -> str:
        m = re.search(r'\burl-tvg="([^"]+)"', self.header, re.I)
        return m.group(1).strip() if m else ""


def parse_xmltv_time(value: str) -> Optional[datetime]:
    value = (value or "").strip()
    for fmt in ("%Y%m%d%H%M%S %z", "%Y%m%d%H%M %z"):
        try:
            return datetime.strptime(value, fmt).astimezone(timezone.utc)
        except ValueError:
            pass
    return None


class EPG:
    """Small in-memory view of the XMLTV guide used by the M3U playlist."""

    def __init__(self, cfg: dict, playlist: Playlist):
        self.cfg = cfg
        self.playlist = playlist
        self.updated = 0.0
        self.programmes: dict[str, list[dict]] = {}
        self.error = ""
        self.lock = asyncio.Lock()

    async def snapshot(self) -> dict:
        ttl = float(self.cfg.get("epg_ttl_seconds", 900))
        if not self.programmes or time.monotonic() - self.updated > ttl:
            async with self.lock:
                if not self.programmes or time.monotonic() - self.updated > ttl:
                    try:
                        await self._reload()
                    except Exception as exc:
                        self.error = str(exc)
                        log.warning("EPG no disponible: %s", exc)
        now = datetime.now(timezone.utc)
        rows = []
        for ch in self.playlist.channels:
            items = self.programmes.get(ch.slug, [])
            current = next((p for p in items if p["start"] <= now < p["stop"]), None)
            following = next((p for p in items if p["start"] >= now), None)
            rows.append({"channel": ch, "current": current, "next": following, "programmes": items})
        return {"rows": rows, "updated": self.updated, "error": self.error, "available": bool(self.programmes)}

    async def _reload(self) -> None:
        url = self.playlist.epg_url()
        if not url.startswith(("https://", "http://")):
            raise RuntimeError("la lista no declara una URL EPG válida")
        timeout = ClientTimeout(total=45, sock_connect=10, sock_read=35)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.get(url, headers={"User-Agent": self.cfg["default_ua"]}) as resp:
                if resp.status != 200:
                    raise RuntimeError(f"EPG HTTP {resp.status}")
                raw = await resp.read()
        if url.lower().endswith(".gz") or raw[:2] == b"\x1f\x8b":
            raw = gzip.decompress(raw)

        wanted: dict[str, str] = {}
        for ch in self.playlist.channels:
            for name in (ch.tvg_id, ch.name):
                key = normalize_epg_name(name)
                if key:
                    wanted.setdefault(key, ch.slug)
        channel_to_slug: dict[str, str] = {}
        programmes: dict[str, list[dict]] = {ch.slug: [] for ch in self.playlist.channels}
        now = datetime.now(timezone.utc)
        horizon = now.timestamp() + 24 * 3600
        context = ET.iterparse(io.BytesIO(raw), events=("end",))
        for _, elem in context:
            if elem.tag == "channel":
                channel_id = elem.get("id") or ""
                for name in [channel_id] + [n.text or "" for n in elem.findall("display-name")]:
                    slug = wanted.get(normalize_epg_name(name), "")
                    if slug:
                        channel_to_slug[channel_id] = slug
                        break
                elem.clear()
                continue
            if elem.tag != "programme":
                continue
            slug = channel_to_slug.get(elem.get("channel") or "", "")
            if slug:
                start = parse_xmltv_time(elem.get("start") or "")
                stop = parse_xmltv_time(elem.get("stop") or "")
                if start and stop and stop.timestamp() > now.timestamp() and start.timestamp() < horizon:
                    stop = min(stop, datetime.fromtimestamp(horizon, tz=timezone.utc))
                    title = (elem.findtext("title") or "Sin título").strip()
                    subtitle = (elem.findtext("sub-title") or "").strip()
                    programmes[slug].append({"start": start, "stop": stop, "title": title, "subtitle": subtitle})
            elem.clear()
        for items in programmes.values():
            items.sort(key=lambda p: p["start"])
        if not any(programmes.values()):
            raise RuntimeError("no se han encontrado canales coincidentes en el EPG")
        self.programmes = programmes
        self.updated = time.monotonic()
        self.error = ""
        log.info("EPG actualizado: %s canales", sum(bool(v) for v in programmes.values()))


def split_url_headers(line: str) -> tuple[str, dict[str, str]]:
    if "|" not in line:
        return line, {}
    url, rest = line.split("|", 1)
    headers = {}
    for part in rest.split("|"):
        if "=" not in part:
            continue
        key, value = part.split("=", 1)
        headers[key.strip()] = value.strip()
    return url.strip(), headers


def unique_slug(name: str, used: set[str]) -> str:
    base = slugify(name)
    slug = base
    n = 2
    while slug in used:
        slug = f"{base}-{n}"
        n += 1
    used.add(slug)
    return slug


class TokenStore:
    def __init__(self, path: str):
        self.path = Path(path)
        self.mtime = 0.0
        self.token = ""
        self.exp: Optional[int] = None

    def get(self) -> str:
        try:
            mtime = self.path.stat().st_mtime
        except OSError:
            return self.token
        if mtime != self.mtime:
            try:
                data = json.loads(self.path.read_text(encoding="utf-8"))
                self.token = data.get("access_token") or ""
                self.exp = data.get("access_token_exp")
                self.mtime = mtime
            except (OSError, json.JSONDecodeError):
                pass
        return self.token


CDN_BODY_MAX = 10 * 1024 * 1024
CDN_REQ_SEC = 8.0
INIT_BODY_MAX = 2 * 1024 * 1024


def abort_connector(connector) -> None:
    if connector is None:
        return
    acquired = getattr(connector, "_acquired", None)
    if acquired:
        for proto in list(acquired):
            try:
                proto.abort()
            except Exception:
                pass
    conns = getattr(connector, "_conns", None)
    if conns:
        for keyed in list(conns.values()):
            for item in list(keyed):
                proto = item[0] if isinstance(item, tuple) else item
                try:
                    proto.abort()
                except Exception:
                    pass


async def read_body_limited(resp: aiohttp.ClientResponse, limit: int = CDN_BODY_MAX) -> bytes:
    cl = resp.content_length
    if cl is not None and cl > limit:
        raise aiohttp.ClientPayloadError(f"CDN body {cl} bytes")
    chunks: list[bytes] = []
    n = 0
    async for chunk in resp.content.iter_chunked(64 * 1024):
        n += len(chunk)
        if n > limit:
            raise aiohttp.ClientPayloadError(f"CDN body > {limit}")
        chunks.append(chunk)
    return b"".join(chunks)


class DashOrigin:
    def __init__(self, cfg: dict, tokens: TokenStore):
        self.cfg = cfg
        self.tokens = tokens
        self.session: Optional[aiohttp.ClientSession] = None
        self.cdn_base: dict[str, str] = {}
        self.init_cache: dict[tuple[str, str], bytes] = {}
        self._sess_lock = asyncio.Lock()
        self._fails = 0
        # Limit work per channel, not the whole service to a single segment.
        # A small amount of parallelism is required to keep several live
        # channels at the edge without overloading the WireGuard uplink.
        concurrency = max(1, int(cfg.get("origin_concurrency", 4)))
        self.work_sem = asyncio.Semaphore(concurrency)

    def _make_session(self) -> aiohttp.ClientSession:
        timeout = ClientTimeout(total=None, sock_connect=6, sock_read=12)
        bind = (self.cfg.get("egress_bind") or "").strip()
        dns = (self.cfg.get("egress_dns") or "").strip()
        proxy = (self.cfg.get("proxy_url") or "").strip()
        if bind:
            resolver = EgressResolver(bind, dns) if dns else None
            connector = TCPConnector(
                family=socket.AF_INET,
                local_addr=(bind, 0),
                limit=8,
                ttl_dns_cache=30,
                force_close=True,
                enable_cleanup_closed=True,
                resolver=resolver,
            )
        elif proxy:
            connector = ProxyConnector.from_url(
                socks_ipv4_url(proxy),
                rdns=False,
                family=socket.AF_INET,
                limit=8,
            )
        else:
            connector = TCPConnector(
                family=socket.AF_INET,
                limit=8,
                force_close=True,
                enable_cleanup_closed=True,
            )
        return aiohttp.ClientSession(connector=connector, timeout=timeout)

    async def start(self) -> None:
        bind = (self.cfg.get("egress_bind") or "").strip()
        dns = (self.cfg.get("egress_dns") or "").strip()
        if bind:
            log.info("egress wg bind=%s dns=%s curl", bind, dns or "system")
        self.session = None

    async def recycle(self) -> None:
        async with self._sess_lock:
            old = self.session
            self.session = self._make_session()
            self._fails = 0
            self.cdn_base.clear()
        if old is not None:
            try:
                abort_connector(old.connector)
            except Exception:
                pass
            try:
                await asyncio.wait_for(old.close(), timeout=1)
            except Exception:
                pass
        log.warning("cdn session recycled")

    async def _note_fail(self) -> None:
        self._fails += 1
        if self._fails >= 2:
            await self.recycle()

    async def _curl_hop(self, url: str, headers: dict[str, str]) -> tuple[int, bytes, str, str]:
        bind = (self.cfg.get("egress_bind") or "").strip()
        dns = (self.cfg.get("egress_dns") or "").strip()
        proxy = (self.cfg.get("proxy_url") or "").strip()
        parsed = urlparse(url)
        host = parsed.hostname or ""
        port = parsed.port or (443 if parsed.scheme == "https" else 80)
        ip = None
        if dns and bind and host:
            loop = asyncio.get_running_loop()
            try:
                ip = await loop.run_in_executor(None, dns_query_a, host, dns, bind)
            except Exception:
                ip = None
        hdr_fd, hdr_path = tempfile.mkstemp(prefix="cdn-h-")
        body_fd, body_path = tempfile.mkstemp(prefix="cdn-b-")
        os.close(hdr_fd)
        os.close(body_fd)
        try:
            cmd = [
                "curl",
                "-4",
                "-sS",
                "--http1.1",
                "--max-time",
                str(int(CDN_REQ_SEC)),
                "--connect-timeout",
                "5",
                "-D",
                hdr_path,
                "-o",
                body_path,
                "-w",
                "%{http_code}",
                "--path-as-is",
            ]
            if bind:
                cmd += ["--interface", bind]
            elif proxy:
                cmd += ["--proxy", proxy, "--noproxy", ""]
            if ip and host:
                cmd += ["--resolve", f"{host}:{port}:{ip}"]
            for key, value in headers.items():
                cmd += ["-H", f"{key}: {value}"]
            cmd.append(url)
            rc, out, err = await run_cmd_out(cmd, timeout=CDN_REQ_SEC + 2)
            if rc != 0:
                raise OSError(err.decode("utf-8", "replace")[:200] or f"curl rc={rc}")
            try:
                status = int((out or b"0").strip() or b"0")
            except ValueError:
                status = 0
            try:
                raw_hdr = Path(hdr_path).read_bytes()
            except OSError:
                raw_hdr = b""
            try:
                body = Path(body_path).read_bytes()
            except OSError:
                body = b""
            if len(body) > CDN_BODY_MAX:
                raise aiohttp.ClientPayloadError(f"CDN body {len(body)} bytes")
            loc = ""
            for line in raw_hdr.splitlines():
                low = line.lower()
                if low.startswith(b"location:"):
                    loc = line.split(b":", 1)[1].strip().decode("utf-8", "replace")
            return status, body, url, loc
        finally:
            try:
                os.unlink(hdr_path)
            except OSError:
                pass
            try:
                os.unlink(body_path)
            except OSError:
                pass

    async def _get(self, url: str, headers: dict[str, str]) -> tuple[int, bytes, str]:
        delay = 0.2
        last_exc: Optional[BaseException] = None
        attempts = max(1, int(self.cfg.get("cdn_retries", 3)))
        for attempt in range(attempts):
            try:
                current = url
                for _hop in range(6):
                    status, body, final, loc = await self._curl_hop(current, headers)
                    if loc and status in (301, 302, 303, 307, 308):
                        current = urljoin(str(final), loc)
                        continue
                    self._fails = 0
                    return status, body, current
                return 0, b"", current
            except (
                ProxyTimeoutError,
                ProxyError,
                asyncio.TimeoutError,
                TimeoutError,
                aiohttp.ClientConnectionError,
                aiohttp.ClientPayloadError,
                OSError,
            ) as exc:
                last_exc = exc
                if attempt + 1 < attempts:
                    log.warning("cdn retry %s/%s %s", attempt + 1, attempts, exc)
                else:
                    log.warning("cdn failed after %s attempts: %s", attempts, exc)
                await self._note_fail()
                if attempt + 1 < attempts:
                    await asyncio.sleep(delay)
                delay = min(delay * 1.7, 2.0)
        if last_exc is not None:
            raise last_exc
        raise web.HTTPBadGateway(text="CDN sin respuesta")

    async def close(self) -> None:
        sess = self.session
        self.session = None
        if sess is not None:
            try:
                await asyncio.wait_for(sess.close(), timeout=2)
            except Exception:
                pass

    def headers_for(self, ch: Channel) -> dict[str, str]:
        token = self.tokens.get() or ch.extra_headers.get("x-tcdn-token", "")
        headers = {
            "User-Agent": ch.ua or self.cfg["default_ua"],
            "Origin": self.cfg["origin"],
            "Referer": ch.referer or self.cfg["referer"],
            "Cache-Control": "no-cache",
            "Pragma": "no-cache",
        }
        if token:
            headers["x-tcdn-token"] = token
        for key, value in ch.extra_headers.items():
            if key.lower() == "x-tcdn-token" and token:
                continue
            headers[key] = value
        return headers

    async def fetch_mpd(self, ch: Channel) -> bytes:
        headers = self.headers_for(ch)
        url = ch.url
        sep = "&" if "?" in url else "?"
        bust = f"{url}{sep}_={int(time.time() * 1000)}"
        status, body, final = await self._get(bust, headers)
        if status != 200:
            status, body, final = await self._get(url, headers)
        if status != 200:
            raise web.HTTPBadGateway(text=f"MPD HTTP {status}")
        self.cdn_base[ch.slug] = final
        return body

    async def fetch_rel(self, ch: Channel, rel: str, use_init_cache: bool = False) -> bytes:
        if use_init_cache:
            cached = self.init_cache.get((ch.slug, rel))
            if cached:
                return cached
        base = self.cdn_base.get(ch.slug)
        if not base:
            await self.fetch_mpd(ch)
            base = self.cdn_base[ch.slug]
        url = urljoin(base, rel)
        headers = self.headers_for(ch)
        delay = 0.2
        for _ in range(3):
            status, data, _final = await self._get(url, headers)
            if status == 200 and data:
                if use_init_cache:
                    if 64 < len(data) <= INIT_BODY_MAX:
                        self.init_cache[(ch.slug, rel)] = data
                    return data
                if len(data) < 256:
                    await asyncio.sleep(delay)
                    delay = min(delay * 1.5, 1.0)
                    continue
                return data
            if status not in (404, 425, 429, 500, 502, 503):
                raise web.HTTPBadGateway(text=f"CDN HTTP {status}")
            await asyncio.sleep(delay)
            delay = min(delay * 1.5, 1.0)
        raise web.HTTPNotFound(text="segmento CDN no disponible")


class ChannelSession:
    WINDOW = 6

    def __init__(self, ch: Channel, cfg: dict, origin: DashOrigin):
        self.ch = ch
        self.cfg = cfg
        self.origin = origin
        self.last_access = time.monotonic()
        self.started_mono = time.monotonic()
        self.started_at = time.time()
        self.viewers = 0
        self.clients: dict[str, dict] = {}
        self.mpd_at = 0.0
        self.video: Optional[DashTrack] = None
        self.audios: list[DashTrack] = []
        self.hls_dir = Path(cfg["hls_dir"]) / ch.slug
        empty_dir(self.hls_dir)
        self.locks: dict[int, asyncio.Lock] = {}
        self._lock_users: dict[int, int] = {}
        self.refresh_lock = asyncio.Lock()
        self.sem = asyncio.Semaphore(2)
        self.stopped = False
        self.task: Optional[asyncio.Task] = None
        self._workers: set[asyncio.Task] = set()
        self.generation = 0
        self.publish_origin: Optional[int] = None
        self.t_to_seq: dict[int, int] = {}
        self.next_seq = 1
        self.ts_origin_t: Optional[int] = None
        self._next_t: Optional[int] = None
        self._published: dict[int, int] = {}
        self._skip_t: set[int] = set()
        self._fail_t: dict[int, int] = {}
        self._last_mux = time.monotonic()
        self._edge_seen: Optional[int] = None
        self._edge_since = time.monotonic()
        self.window = max(3, int(cfg.get("hls_window", self.WINDOW)))

    def touch(self) -> None:
        self.last_access = time.monotonic()

    def start_producer(self) -> None:
        if self.task is None or self.task.done():
            self.stopped = False
            self.task = asyncio.create_task(self._producer(), name=f"prod-{self.ch.slug}")

    async def stop(self) -> None:
        self.stopped = True
        if self.task and not self.task.done():
            self.task.cancel()
            try:
                await self.task
            except (asyncio.CancelledError, Exception):
                pass
        workers = list(self._workers)
        for worker in workers:
            worker.cancel()
        if workers:
            await asyncio.gather(*workers, return_exceptions=True)
        empty_dir(self.hls_dir)

    async def refresh(self) -> None:
        async with self.refresh_lock:
            if time.monotonic() - self.mpd_at < 1.0 and self.video is not None:
                return
            body = await self.origin.fetch_mpd(self.ch)
            video, audios = select_tracks(body, int(self.cfg.get("max_height", 0)), self.ch.keys)
            if video is None or not video.entries:
                raise web.HTTPBadGateway(text="MPD sin video")
            prev = self.video.rep_id if self.video else None
            prev_audio = tuple(a.rep_id for a in self.audios)
            new_audio = tuple(a.rep_id for a in audios)
            if prev and (prev != video.rep_id or prev_audio != new_audio):
                self.generation += 1
                for path in self.hls_dir.glob("seg_*.ts"):
                    try:
                        path.unlink()
                    except OSError:
                        pass
                self.ts_origin_t = None
                self._next_t = None
                self._published.clear()
                self.publish_origin = None
                self.t_to_seq.clear()
                self.next_seq = 1
                self._skip_t.clear()
                self._fail_t.clear()
            self.video, self.audios = video, audios
            self.mpd_at = time.monotonic()
            if prev != video.rep_id or prev_audio != new_audio:
                log.info(
                    "tracks %s video=%s %s %dkbit %s audio=%s",
                    self.ch.slug,
                    video.rep_id,
                    format_quality(video.height, video.fps),
                    video.bandwidth // 1000,
                    video.codecs,
                    ",".join(f"{a.lang or 'und'}:{a.rep_id}" for a in audios) or "-",
                )

    def closed_entries(self) -> list[tuple[int, int]]:
        assert self.video is not None
        entries = self.video.entries
        if len(entries) > 2:
            return entries[:-2]
        if len(entries) > 1:
            return entries[:-1]
        return entries

    def ready_entries(self, pin: bool = False) -> list[tuple[int, int]]:
        # A CDN can return an older/shorter MPD on a subsequent refresh.
        # Completed media must stay available independently of that snapshot.
        entries: list[tuple[int, int]] = []
        for t, d in sorted(self._published.items()):
            path = self.hls_dir / f"seg_{t}.ts"
            try:
                ok = path.is_file() and path.stat().st_size > 40000
            except OSError:
                ok = False
            if ok:
                entries.append((t, d))
        return entries[-self.window:]

    def _seq(self, t: int) -> int:
        if t not in self.t_to_seq:
            self.t_to_seq[t] = self.next_seq
            self.next_seq += 1
        return self.t_to_seq[t]

    def build_index(self) -> str:
        assert self.video is not None
        entries = self.ready_entries(pin=True)
        if not entries:
            raise web.HTTPBadGateway(text="sin segmentos listos")
        for t, _d in entries:
            self._seq(t)
        base = (self.cfg.get("public_base") or "").rstrip("/")
        prefix = f"{base}/live/{self.ch.slug}" if base else f"live/{self.ch.slug}"
        durs = [d / self.video.timescale for _, d in entries]
        target = max(2, int(max(durs) + 0.999))
        lines = [
            "#EXTM3U",
            "#EXT-X-VERSION:3",
            f"#EXT-X-TARGETDURATION:{target}",
            f"#EXT-X-MEDIA-SEQUENCE:{self._seq(entries[0][0])}",
        ]
        for t, d in entries:
            dur = d / self.video.timescale
            lines.append(f"#EXTINF:{dur:.3f},")
            lines.append(f"{prefix}/seg_{t}.ts")
        return "\n".join(lines) + "\n"

    def bsf(self) -> str:
        codecs = (self.video.codecs if self.video else "") or ""
        if codecs.startswith("hvc1") or codecs.startswith("hev1"):
            return "hevc_mp4toannexb"
        return "h264_mp4toannexb"

    async def remux(self, t: int) -> Path:
        worker = asyncio.current_task()
        self._workers.add(worker)
        try:
            return await self._remux(t)
        finally:
            self._workers.discard(worker)

    @asynccontextmanager
    async def _segment_work(self, t: int):
        lock = self.locks.setdefault(t, asyncio.Lock())
        # Include queued and awakened waiters, even while lock.locked() is false.
        self._lock_users[t] = self._lock_users.get(t, 0) + 1
        try:
            async with lock:
                try:
                    yield
                finally:
                    # Only the lock owner may remove the working directory.
                    tmpdir = self.hls_dir / f".tmp-{t}"
                    if tmpdir.exists():
                        shutil.rmtree(tmpdir, ignore_errors=True)
        finally:
            self._lock_users[t] -= 1
            if not self._lock_users[t]:
                self._lock_users.pop(t)

    async def _remux(self, t: int) -> Path:
        if self.stopped:
            raise web.HTTPBadGateway(text="canal parado")
        await self.refresh()
        assert self.video is not None
        out = self.hls_dir / f"seg_{t}.ts"
        if out.is_file() and out.stat().st_size > 40000:
            return out
        async with self._segment_work(t):
            if out.is_file() and out.stat().st_size > 40000:
                return out
            video = self.video
            duration = next((d for timestamp, d in video.entries if timestamp == t), None)
            if duration is None:
                raise web.HTTPBadGateway(text="segmento fuera del manifiesto; reintentar")
            vrel = fill_template(video.media_tmpl, video.rep_id, t)
            audio_jobs: list[DashTrack] = []
            for audio in self.audios:
                aent = nearest_audio(video, t, audio)
                if aent is None:
                    raise web.HTTPBadGateway(text="segmento de audio no disponible")
                audio_jobs.append(audio)

            async def _audio_pair(audio: DashTrack) -> tuple[DashTrack, bytes, bytes]:
                aent = nearest_audio(video, t, audio)
                assert aent is not None
                arel = fill_template(audio.media_tmpl, audio.rep_id, aent[0])
                ainit, aseg = await gather_owned(
                    self.origin.fetch_rel(self.ch, audio.init_rel, use_init_cache=True),
                    self.origin.fetch_rel(self.ch, arel),
                )
                return audio, ainit, aseg

            async with self.origin.work_sem:
                vinit, vseg = await gather_owned(
                    self.origin.fetch_rel(self.ch, video.init_rel, use_init_cache=True),
                    self.origin.fetch_rel(self.ch, vrel),
                )
                audio_parts: list[tuple[DashTrack, bytes, bytes]] = []
                if audio_jobs:
                    got = await asyncio.gather(
                        *[_audio_pair(a) for a in audio_jobs],
                        return_exceptions=True,
                    )
                    for item in got:
                        if isinstance(item, Exception):
                            log.warning("audio fetch %s t=%s: %s", self.ch.slug, t, item)
                            raise web.HTTPBadGateway(text="descarga de audio incompleta")
                        audio_parts.append(item)
            if self.stopped:
                raise web.HTTPBadGateway(text="canal parado")
            tmpdir = self.hls_dir / f".tmp-{t}"
            if tmpdir.exists():
                shutil.rmtree(tmpdir, ignore_errors=True)
            tmpdir.mkdir(parents=True, exist_ok=True)
            vfile = tmpdir / "v.mp4"
            vfile.write_bytes(vinit + vseg)
            afiles: list[tuple[DashTrack, Path, Path]] = []
            for i, (audio, ainit, aseg) in enumerate(audio_parts):
                if not ainit or not aseg:
                    continue
                afile = tmpdir / f"a{i}.mp4"
                afile.write_bytes(ainit + aseg)
                adec = tmpdir / f"adec{i}.mp4"
                afiles.append((audio, afile, adec))
            vdec = tmpdir / "vdec.mp4"
            packager = self.cfg.get("packager") or str(SCRIPT_DIR / "bin" / "packager")
            keys_arg = shaka_keys_arg(self.ch.keys)

            async def _decrypt(src: Path, kind: str, dest: Path) -> tuple[int, bytes]:
                cmd = [packager, f"in={src},stream={kind},out={dest}"]
                if keys_arg:
                    cmd += ["--enable_raw_key_decryption", "--keys", keys_arg]
                cmd.append("--quiet")
                return await run_cmd(cmd)

            decrypt_jobs = [_decrypt(vfile, "video", vdec)]
            for _audio, afile, adec in afiles:
                decrypt_jobs.append(_decrypt(afile, "audio", adec))
            decrypt_out = await asyncio.gather(*decrypt_jobs, return_exceptions=True)
            if self.stopped:
                shutil.rmtree(tmpdir, ignore_errors=True)
                raise web.HTTPBadGateway(text="canal parado")
            vres = decrypt_out[0]
            if isinstance(vres, Exception):
                shutil.rmtree(tmpdir, ignore_errors=True)
                log.warning("shaka %s t=%s video %s", self.ch.slug, t, vres)
                raise web.HTTPBadGateway(text="no se pudo descifrar")
            vrc, verr = vres
            if vrc != 0 or not vdec.is_file() or vdec.stat().st_size < 20000:
                shutil.rmtree(tmpdir, ignore_errors=True)
                log.warning("shaka %s t=%s rc=%s %s", self.ch.slug, t, vrc, (verr or b"")[-400:])
                raise web.HTTPBadGateway(text="no se pudo descifrar")
            ready_audio: list[tuple[DashTrack, Path]] = []
            for i, (audio, _afile, adec) in enumerate(afiles):
                ares = decrypt_out[i + 1]
                if isinstance(ares, Exception):
                    log.warning("shaka audio %s %s t=%s: %s", self.ch.slug, audio.lang, t, ares)
                    continue
                arc, aerr = ares
                if arc != 0 or not adec.is_file() or adec.stat().st_size <= 1000:
                    log.warning(
                        "shaka audio %s %s t=%s rc=%s %s",
                        self.ch.slug,
                        audio.lang,
                        t,
                        arc,
                        (aerr or b"")[-200:],
                    )
                    continue
                ready_audio.append((audio, adec))
            if len(ready_audio) != len(audio_jobs):
                raise web.HTTPBadGateway(text="audio incompleto; reintentar segmento")
            tmp_out = tmpdir / "out.ts"
            if self.ts_origin_t is None:
                self.ts_origin_t = t
            # Preserve the common DASH clock, including fractional audio
            # boundaries. Normalizing each input/segment separately loses it.
            # One second of headroom keeps initial decode timestamps positive.
            offset = 1.0 - self.ts_origin_t / max(video.timescale, 1)
            mux = [
                self.cfg["ffmpeg"],
                "-y",
                "-hide_banner",
                "-loglevel",
                "error",
                "-nostdin",
                "-copyts",
                "-i",
                str(vdec),
            ]
            for _audio, adec in ready_audio:
                mux += ["-i", str(adec)]
            mux += ["-map", "0:v:0"]
            for i in range(len(ready_audio)):
                mux += ["-map", f"{i + 1}:a:0"]
            mux += ["-c", "copy", "-bsf:v", self.bsf()]
            for i, (audio, _adec) in enumerate(ready_audio):
                mux += [f"-metadata:s:a:{i}", f"language={iso639(audio.lang)}"]
                mux += [f"-disposition:a:{i}", "default" if i == 0 else "0"]
            mux += [
                "-muxdelay",
                "0",
                "-muxpreload",
                "0",
                "-output_ts_offset",
                f"{offset:.6f}",
                "-avoid_negative_ts",
                "disabled",
                "-mpegts_copyts",
                "1",
                "-f",
                "mpegts",
                str(tmp_out),
            ]
            rc, err = await run_cmd(mux)
            if self.stopped:
                shutil.rmtree(tmpdir, ignore_errors=True)
                raise web.HTTPBadGateway(text="canal parado")
            if rc != 0 or not tmp_out.is_file() or tmp_out.stat().st_size < 40000:
                shutil.rmtree(tmpdir, ignore_errors=True)
                log.warning("mux %s t=%s rc=%s %s", self.ch.slug, t, rc, (err or b"")[-300:])
                raise web.HTTPBadGateway(text="no se pudo remuxear")
            try:
                tmp_out.replace(out)
            except OSError:
                shutil.rmtree(tmpdir, ignore_errors=True)
                raise web.HTTPBadGateway(text="canal parado")
            self._published[t] = duration
            shutil.rmtree(tmpdir, ignore_errors=True)
            self._prune_cache()
            return out

    def _prune_cache(self) -> None:
        keep = set(sorted(self._published)[-self.window:])
        for t in list(self._published):
            if t not in keep:
                self._published.pop(t, None)
        if self.video and self.video.entries:
            oldest = min(t for t, _d in self.video.entries)
            if self._next_t is not None:
                oldest = min(oldest, self._next_t)
            # Retain recoverable/future timestamps, published media and all work
            # users. Neither sequence allocation nor the transport clock resets.
            protected = keep | self._lock_users.keys()
            for state in (self.locks, self.t_to_seq, self._fail_t):
                for t in list(state):
                    if t < oldest and t not in protected:
                        state.pop(t, None)
            self._skip_t.difference_update(
                {t for t in self._skip_t if t < oldest and t not in protected})
        for path in self.hls_dir.glob("seg_*.ts"):
            m = re.match(r"seg_(\d+)\.ts$", path.name)
            if not m or int(m.group(1)) in keep:
                continue
            try:
                path.unlink()
            except OSError:
                pass

    async def _producer(self) -> None:
        log.info("producer %s", self.ch.slug)
        while not self.stopped:
            try:
                try:
                    async with asyncio.timeout(14):
                        await self.refresh()
                except TimeoutError:
                    log.warning("producer refresh hung %s", self.ch.slug)
                    try:
                        await asyncio.wait_for(self.origin.recycle(), timeout=4)
                    except Exception:
                        pass
                    await asyncio.sleep(0.3)
                    continue
                closed = self.closed_entries()
                if not closed:
                    await asyncio.sleep(1.0)
                    continue
                if self.ts_origin_t is None:
                    self.ts_origin_t = closed[-1][0]
                edge_t = closed[-1][0]
                if edge_t != self._edge_seen:
                    self._edge_seen = edge_t
                    self._edge_since = time.monotonic()
                elif time.monotonic() - self._edge_since > 10:
                    log.warning("mpd stale %s edge=%s, recycle", self.ch.slug, edge_t)
                    self.mpd_at = 0
                    try:
                        await asyncio.wait_for(self.origin.recycle(), timeout=5)
                    except Exception:
                        pass
                    self._edge_since = time.monotonic()
                pending = self.next_pending(closed)
                want_t = pending[0] if pending else None
                if want_t is not None:
                    remux_timeout = max(12.0, float(self.cfg.get("remux_timeout", 20)))
                    try:
                        async with asyncio.timeout(remux_timeout):
                            await self.remux(want_t)
                        self._last_mux = time.monotonic()
                        self._fail_t.pop(want_t, None)
                        self._next_t = want_t + pending[1]
                    except Exception as exc:
                        log.warning("producer remux %s t=%s: %s %s", self.ch.slug, want_t, type(exc).__name__, exc)
                        self._fail_t[want_t] = self._fail_t.get(want_t, 0) + 1
                        failures_before_skip = max(1, int(self.cfg.get("segment_failures_before_skip", 2)))
                        if self._fail_t[want_t] >= failures_before_skip:
                            log.warning("segment skipped %s t=%s", self.ch.slug, want_t)
                            self._skip_t.add(want_t)
                            self._next_t = want_t + pending[1]
                self._prune_cache()
            except asyncio.CancelledError:
                raise
            except web.HTTPException as exc:
                log.warning("producer %s: %s", self.ch.slug, exc)
            except (
                ProxyTimeoutError,
                ProxyError,
                asyncio.TimeoutError,
                TimeoutError,
                aiohttp.ClientConnectionError,
                aiohttp.ClientPayloadError,
            ) as exc:
                log.warning("producer %s origin: %s", self.ch.slug, exc)
            except Exception:
                log.exception("producer %s", self.ch.slug)
            await asyncio.sleep(0.35)

    def next_pending(self, closed: list[tuple[int, int]]) -> Optional[tuple[int, int]]:
        """Join at the edge once, then recover available segments in order."""
        if not closed:
            return None
        if self._next_t is None:
            self._next_t = closed[-1][0]
        if self._next_t < closed[0][0]:
            log.warning("segments expired %s expected=%s available=%s", self.ch.slug, self._next_t, closed[0][0])
            self._next_t = closed[0][0]
        for t, d in closed:
            if t < self._next_t:
                continue
            path = self.hls_dir / f"seg_{t}.ts"
            if t in self._skip_t or (path.is_file() and path.stat().st_size > 40000):
                self._next_t = t + d
                continue
            return t, d
        return None

    async def wait_ready(self, n: int = 3, timeout: float = 20.0) -> None:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if len(self.ready_entries()) >= n:
                return
            await asyncio.sleep(0.25)
        if not self.ready_entries():
            raise web.HTTPBadGateway(text="el canal no arranco a tiempo")


class AppState:
    def __init__(self, cfg: dict):
        self.cfg = cfg
        self.playlist = Playlist(cfg["source_m3u"])
        self.epg = EPG(cfg, self.playlist)
        self.tokens = TokenStore(cfg["token_file"])
        self.origin = DashOrigin(cfg, self.tokens)
        self.sessions: dict[str, ChannelSession] = {}
        self.lock = asyncio.Lock()

    async def get_session(self, slug: str) -> ChannelSession:
        async with self.lock:
            self.playlist.maybe_reload(self.cfg["default_ua"], self.cfg["referer"])
            ch = self.playlist.by_slug.get(slug)
            if ch is None or not ch.is_dash:
                raise web.HTTPNotFound(text="canal no encontrado")
            sess = self.sessions.get(slug)
            if sess is None:
                live = len(self.sessions)
                if live >= int(self.cfg.get("max_channels", 6)):
                    idle_sessions = [
                        candidate
                        for candidate in self.sessions.values()
                        if candidate.viewers <= 0 and not candidate.clients
                    ]
                    if not idle_sessions:
                        raise web.HTTPServiceUnavailable(
                            text="todos los canales disponibles estan en uso",
                            headers={"Retry-After": "5"},
                        )
                    oldest = min(idle_sessions, key=lambda s: s.last_access)
                    await self._drop(oldest.ch.slug)
                sess = ChannelSession(ch, self.cfg, self.origin)
                self.sessions[slug] = sess
            sess.touch()
            return sess

    async def _drop(self, slug: str) -> None:
        sess = self.sessions.pop(slug, None)
        if sess is None:
            return
        await sess.stop()
        empty_dir(sess.hls_dir)
        try:
            sess.hls_dir.rmdir()
        except OSError:
            await asyncio.sleep(0.2)
            empty_dir(sess.hls_dir)
            try:
                sess.hls_dir.rmdir()
            except OSError:
                pass
        log.info("suelto %s", slug)

    async def reaper(self) -> None:
        idle = float(self.cfg.get("idle_seconds", 30))
        while True:
            await asyncio.sleep(2)
            now = time.monotonic()
            async with self.lock:
                slugs = [
                    s
                    for s, sess in self.sessions.items()
                    if sess.viewers <= 0
                    and not sess.clients
                    and now - sess.last_access > idle
                ]
            for slug in slugs:
                async with self.lock:
                    await self._drop(slug)


def m3u8_headers() -> dict[str, str]:
    return {
        "Content-Type": "application/vnd.apple.mpegurl",
        "Cache-Control": "no-store, no-cache, must-revalidate",
        "Access-Control-Allow-Origin": "*",
    }


def playlist_headers() -> dict[str, str]:
    return {
        "Content-Type": "audio/x-mpegurl",
        "Cache-Control": "no-store, no-cache, must-revalidate",
        "Access-Control-Allow-Origin": "*",
    }


def public_base(cfg: dict) -> str:
    return (cfg.get("public_base") or "").rstrip("/")


def channel_url(cfg: dict, slug: str) -> str:
    base = public_base(cfg)
    if base:
        return f"{base}/live/{slug}/stream.ts"
    return f"live/{slug}/stream.ts"


async def gather_owned(*aws):
    """Do not leave sibling downloads running after failure or cancellation."""
    tasks = [asyncio.ensure_future(aw) for aw in aws]
    try:
        return await asyncio.gather(*tasks)
    finally:
        for task in tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)


class TSContinuity:
    """Each HTTP connection owns counters; shared cached segments stay intact."""

    def __init__(self):
        self.counters: dict[int, int] = {}

    def rewrite(self, data: bytes) -> bytes:
        if len(data) % 188:
            raise ValueError("MPEG-TS incompleto")
        output = bytearray(data)
        for pos in range(0, len(output), 188):
            if output[pos] != 0x47:
                raise ValueError("MPEG-TS fuera de sincronía")
            pid = ((output[pos + 1] & 31) << 8) | output[pos + 2]
            control = (output[pos + 3] >> 4) & 3
            if pid == 8191 or control == 0:
                continue
            cc = output[pos + 3] & 15
            if pid in self.counters:
                cc = (self.counters[pid] + bool(control & 1)) & 15
            output[pos + 3] = (output[pos + 3] & 240) | cc
            self.counters[pid] = cc
        return bytes(output)


async def write_ts(resp: web.StreamResponse, data: bytes, duration: float = 0.0, continuity=None) -> None:
    if continuity is not None:
        data = continuity.rewrite(data)
    pkt = 188
    total = (len(data) // pkt) * pkt
    if total <= 0:
        return
    view = memoryview(data)[:total]
    chunk = pkt * 512
    t0 = time.monotonic()
    sent = 0
    while sent < total:
        nxt = min(sent + chunk, total)
        await resp.write(view[sent:nxt].tobytes())
        sent = nxt
        if duration > 0.4:
            target = t0 + duration * 0.98 * (sent / total)
            delay = target - time.monotonic()
            if delay > 0.004:
                await asyncio.sleep(delay)


def render_playlist(playlist: Playlist, cfg: dict) -> str:
    """One URL/slug mapping for the HTTP playlist and generate.py exports."""
    lines = [playlist.header, ""]
    for ch in playlist.channels:
        lines.append(ch.extinf)
        if ch.is_dash:
            lines.append(channel_url(cfg, ch.slug))
        else:
            lines.append(ch.url)
        lines.append("")
    return "\n".join(lines) + "\n"


async def handle_playlist(request: web.Request) -> web.Response:
    state: AppState = request.app["state"]
    state.playlist.maybe_reload(state.cfg["default_ua"], state.cfg["referer"])
    return web.Response(
        text=render_playlist(state.playlist, state.cfg), headers=playlist_headers()
    )


async def handle_stream(request: web.Request) -> web.StreamResponse:
    state: AppState = request.app["state"]
    sess = await state.get_session(request.match_info["slug"])
    sess.start_producer()
    resp = web.StreamResponse(
        status=200,
        headers={
            "Content-Type": "video/mp2t",
            "Cache-Control": "no-store, no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
            "Access-Control-Allow-Origin": "*",
        },
    )
    await resp.prepare(request)
    last_t: Optional[int] = None
    join_t: Optional[int] = None
    join_at = 0.0
    continuity = TSContinuity()
    expected_t: Optional[int] = None
    sess.viewers += 1
    client_id = uuid.uuid4().hex
    forwarded = request.headers.get("X-Forwarded-For") or request.headers.get("X-Real-IP") or request.remote or ""
    client_ip = forwarded.split(",")[0].strip()
    client_ua = request.headers.get("User-Agent") or ""
    sess.clients[client_id] = {
        "ip": client_ip,
        "ua": client_ua,
        "since": time.time(),
        "since_mono": time.monotonic(),
    }
    log.info("stream open %s viewers=%s ip=%s", sess.ch.slug, sess.viewers, client_ip)
    started = time.monotonic()
    last_output = started
    generation = getattr(sess, "generation", 0)
    try:
        while not sess.stopped:
            if request.transport is None or request.transport.is_closing():
                break
            sess.touch()
            if getattr(sess, "generation", 0) != generation:
                log.warning("stream tracks changed %s; closing for reconnect", sess.ch.slug)
                break
            entries = sess.ready_entries(pin=True)
            if last_t is None:
                # Pin the latest ready segment while a small startup margin
                # builds. Chasing newer segments would discard that margin.
                if entries and all(t != join_t for t, _d in entries):
                    join_t, join_d = entries[-1]
                    margin = min(3.0, join_d / max((sess.video.timescale if sess.video else 1), 1) / 2)
                    join_at = min(started + 25.0, time.monotonic() + margin)
                entries = ([entry for entry in entries if entry[0] == join_t]
                           if time.monotonic() >= join_at else [])
            wrote = False
            for t, d in entries:
                if last_t is not None and t <= last_t:
                    continue
                path = sess.hls_dir / f"seg_{t}.ts"
                try:
                    data = path.read_bytes()
                except OSError:
                    continue
                if len(data) < 40000:
                    continue
                dur = d / max((sess.video.timescale if sess.video else 1), 1)
                if expected_t is not None and t != expected_t:
                    log.warning("stream gap %s expected=%s received=%s", sess.ch.slug, expected_t, t)
                await write_ts(resp, data, 0.0 if last_t is None else dur, continuity=continuity)
                last_t = t
                expected_t = t + d
                last_output = time.monotonic()
                wrote = True
                break
            if not wrote:
                if last_t is None and time.monotonic() - started > 25:
                    break
                if last_t is not None and time.monotonic() - last_output > 30:
                    log.warning("stream stalled %s; closing for reconnect", sess.ch.slug)
                    break
                await asyncio.sleep(0.12)
    except (ConnectionResetError, ConnectionError, asyncio.CancelledError, BrokenPipeError):
        pass
    except Exception:
        log.exception("stream %s", sess.ch.slug)
    finally:
        sess.viewers = max(0, sess.viewers - 1)
        sess.clients.pop(client_id, None)
        sess.touch()
        log.info("stream close %s viewers=%s", sess.ch.slug, sess.viewers)
        try:
            await resp.write_eof()
        except Exception:
            pass
    return resp


async def handle_live_index(request: web.Request) -> web.Response:
    state: AppState = request.app["state"]
    sess = await state.get_session(request.match_info["slug"])
    sess.start_producer()
    try:
        await sess.refresh()
        await sess.wait_ready(n=3, timeout=25)
        body = sess.build_index()
    except web.HTTPException:
        raise
    except Exception:
        log.exception("index %s", sess.ch.slug)
        raise web.HTTPBadGateway(text="error arrancando canal")
    return web.Response(text=body, headers=m3u8_headers())


SAFE_SEG = re.compile(r"^seg_(\d+)\.ts$")


async def handle_live_seg(request: web.Request) -> web.StreamResponse:
    state: AppState = request.app["state"]
    slug = request.match_info["slug"]
    name = request.match_info["name"]
    m = SAFE_SEG.match(name)
    if not m:
        raise web.HTTPNotFound()
    t = int(m.group(1))
    sess = await state.get_session(slug)
    sess.start_producer()
    path = sess.hls_dir / f"seg_{t}.ts"
    if not (path.is_file() and path.stat().st_size > 40000):
        path = await sess.remux(t)
    return web.FileResponse(
        path,
        headers={
            "Content-Type": "video/mp2t",
            "Cache-Control": "public, max-age=60",
            "Access-Control-Allow-Origin": "*",
        },
    )


def fmt_duration(seconds: float) -> str:
    total = max(0, int(seconds))
    h, rem = divmod(total, 3600)
    m, s = divmod(rem, 60)
    if h:
        return f"{h}h {m:02d}m {s:02d}s"
    if m:
        return f"{m}m {s:02d}s"
    return f"{s}s"


def session_snapshot(state: AppState) -> dict:
    now = time.monotonic()
    live = []
    for slug, sess in sorted(state.sessions.items(), key=lambda kv: -kv[1].started_mono):
        clients = []
        for c in sess.clients.values():
            clients.append(
                {
                    "ip": c.get("ip") or "",
                    "ua": c.get("ua") or "",
                    "connected": fmt_duration(now - c.get("since_mono", now)),
                }
            )
        live.append(
            {
                "slug": slug,
                "name": sess.ch.name,
                "logo": sess.ch.logo,
                "active": fmt_duration(now - sess.started_mono),
                "active_s": int(now - sess.started_mono),
                "idle": fmt_duration(now - sess.last_access),
                "idle_s": round(now - sess.last_access, 1),
                "viewers": sess.viewers,
                "clients": clients,
                "cached": len(list(sess.hls_dir.glob("seg_*.ts"))),
                "producer": bool(sess.task and not sess.task.done()),
                "height": sess.video.height if sess.video else 0,
                "fps": round(sess.video.fps, 3) if sess.video and sess.video.fps else 0,
                "quality": format_quality(
                    sess.video.height if sess.video else 0,
                    sess.video.fps if sess.video else 0.0,
                ),
                "started_at": datetime.fromtimestamp(sess.started_at, tz=timezone.utc).strftime(
                    "%Y-%m-%d %H:%M:%S UTC"
                ),
            }
        )
    token = state.tokens.get()
    exp = state.tokens.exp
    token_left = None
    if exp:
        token_left = int(exp - time.time())
    return {
        "channels_total": len(state.playlist.channels),
        "open": len(live),
        "live": live,
        "token_ok": bool(token),
        "token_left_s": token_left,
        "now": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC"),
    }


def render_status_page(data: dict) -> str:
    initial_json = json.dumps(data, ensure_ascii=False, separators=(",", ":")).replace(
        "<", "\\u003c"
    )
    page = """<!DOCTYPE html>
<html lang="es">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
<title>Shaka-proxy · Estado</title>
<style>
  :root {
    color-scheme: dark;
    --bg: #0c0f14; --panel: #151a22; --panel-2: #1a202a;
    --line: #29313e; --text: #eef1f5; --muted: #929cab;
    --green: #55d98a; --green-bg: #112d20; --blue: #77b7ff;
    --amber: #f2c66d; --red: #ff8585;
  }
  * { box-sizing: border-box; }
  html { -webkit-text-size-adjust: 100%; }
  body {
    margin: 0; min-height: 100vh; font-family: Inter, ui-sans-serif, system-ui, sans-serif;
    background: radial-gradient(circle at 80% -10%, #17283a 0, transparent 38rem), var(--bg);
    color: var(--text); line-height: 1.45;
  }
  main { max-width: 1120px; margin: 0 auto; padding: 34px 20px calc(50px + env(safe-area-inset-bottom)); }
  header { margin-bottom: 22px; }
  h1 { margin: 0; font-size: 1.45rem; letter-spacing: -.025em; }
  .cards { display: grid; grid-template-columns: repeat(4, 1fr); gap: 12px; margin-bottom: 20px; }
  .card {
    min-width: 0; padding: 15px 16px; border: 1px solid var(--line); border-radius: 14px;
    background: linear-gradient(145deg, rgba(27,34,44,.95), rgba(18,23,31,.95));
    box-shadow: 0 12px 30px rgba(0,0,0,.12);
  }
  .card .k { color: var(--muted); font-size: .72rem; font-weight: 650; text-transform: uppercase; letter-spacing: .075em; }
  .card .v { margin-top: 5px; font-size: 1.28rem; font-weight: 650; font-variant-numeric: tabular-nums; overflow-wrap: anywhere; }
  .card.warning .v { color: var(--amber); }
  .section-head { margin: 0 2px 9px; }
  h2 { margin: 0; font-size: .96rem; letter-spacing: -.01em; }
  .table-wrap { overflow: hidden; border: 1px solid var(--line); border-radius: 14px; background: var(--panel); }
  table { width: 100%; border-collapse: collapse; }
  th, td { padding: 11px 13px; text-align: left; vertical-align: middle; border-bottom: 1px solid var(--line); }
  tbody tr:last-child td { border-bottom: 0; }
  th { color: var(--muted); font-size: .69rem; font-weight: 650; text-transform: uppercase; letter-spacing: .065em; background: #121720; }
  tbody tr { transition: background .18s ease; }
  .channel-summary.has-clients { cursor: pointer; }
  .channel-summary.has-clients:hover,
  .channel-summary.has-clients:focus-visible,
  .channel-summary[aria-expanded="true"] { background: var(--panel-2); outline: none; }
  .channel-summary:focus-visible { box-shadow: inset 0 0 0 2px var(--blue); }
  .channel-summary[aria-expanded="true"] td { border-bottom: 0; }
  .mono { font-variant-numeric: tabular-nums; font-family: ui-monospace, SFMono-Regular, monospace; font-size: .84rem; }
  .muted { color: var(--muted); }
  .channel { display: flex; align-items: center; gap: 10px; min-width: 0; }
  .channel strong { overflow-wrap: anywhere; }
  .logo, .logo-ph { width: 38px; height: 38px; flex: none; object-fit: contain; }
  .logo-ph { display: grid; place-items: center; border-radius: 9px; background: #222a36; color: var(--muted); font-size: .7rem; }
  .client-count-wrap { display: inline-flex; align-items: center; gap: 8px; }
  .client-count {
    display: inline-grid; min-width: 1.8rem; height: 1.8rem; padding: 0 .5rem;
    place-items: center; border-radius: 999px; background: #222b38;
    color: var(--text); font: 650 .82rem/1 ui-monospace, SFMono-Regular, monospace;
  }
  .client-arrow { color: var(--muted); font-size: .82rem; transition: transform .18s ease; }
  .channel-summary[aria-expanded="true"] .client-arrow { transform: rotate(180deg); }
  .client-details td { padding: 0 13px 13px; background: var(--panel-2); }
  .client-list {
    margin: 0; padding: 0; list-style: none;
    border: 1px solid var(--line); border-radius: 10px; background: #11161e;
  }
  .client { min-width: 0; padding: 10px 12px; }
  .client + .client { border-top: 1px solid var(--line); }
  .ua { color: #d8dde5; font-size: .78rem; overflow-wrap: anywhere; word-break: break-word; }
  .client-meta { margin-top: 3px; color: var(--muted); font-size: .7rem; overflow-wrap: anywhere; }
  .empty { margin: 0; padding: 42px 18px; text-align: center; color: var(--muted); }
  [hidden] { display: none !important; }
  noscript p { padding: 12px; color: var(--amber); background: #332812; border-radius: 10px; }
  @media (max-width: 860px) {
    .cards { grid-template-columns: 1fr 1fr; }
    .table-wrap { border: 0; background: transparent; overflow: visible; }
    thead { display: none; }
    table, tbody, tr, td { display: block; width: 100%; }
    tr {
      background: #1a1d24; border: 1px solid #2a2f3a; border-radius: 12px;
      margin-bottom: 10px; padding: 12px 14px; box-shadow: 0 10px 24px rgba(0,0,0,.1);
    }
    td {
      display: flex; justify-content: space-between; align-items: flex-start;
      gap: 14px; border: 0; padding: 6px 0;
    }
    td::before {
      content: attr(data-label);
      color: var(--muted); font-size: .67rem; font-weight: 650;
      text-transform: uppercase; letter-spacing: .055em; flex: 0 0 6.2rem; padding-top: 3px;
    }
    td:first-child {
      display: block; padding: 0 0 10px; margin-bottom: 4px;
      border-bottom: 1px solid var(--line);
    }
    td:first-child::before { display: none; }
    .ua { max-width: min(58vw, 360px); text-align: right; }
    .channel-summary[aria-expanded="true"] {
      margin-bottom: 0; border-bottom: 0; border-radius: 12px 12px 0 0;
    }
    .client-details {
      margin-top: 0; padding: 0 14px 12px; border-top: 0;
      border-radius: 0 0 12px 12px; background: var(--panel-2);
    }
    .client-details td { display: block; margin: 0; padding: 0; border: 0; background: transparent; }
    .client-details td::before { display: none; }
    .client .ua, .client-meta { max-width: none; text-align: left; }
  }
  @media (max-width: 520px) {
    main { padding: 18px 12px calc(30px + env(safe-area-inset-bottom)); }
    header { margin-bottom: 16px; }
    h1 { font-size: 1.2rem; }
    .cards { gap: 8px; margin-bottom: 16px; }
    .card { padding: 12px; border-radius: 12px; }
    .card .v { font-size: 1.08rem; }
    .section-head { margin-bottom: 8px; }
  }
  @media (prefers-reduced-motion: reduce) { * { animation: none !important; transition: none !important; } }
</style>
</head>
<body>
<main>
  <header><h1>Shaka-proxy</h1></header>
  <div class="cards">
    <div class="card"><div class="k">Canales abiertos</div><div id="open" class="v">—</div></div>
    <div class="card"><div class="k">Espectadores</div><div id="viewers" class="v">—</div></div>
    <div class="card"><div class="k">En la lista</div><div id="total" class="v">—</div></div>
    <div id="token-card" class="card"><div class="k">Token CDN</div><div id="token" class="v">—</div></div>
  </div>
  <div class="section-head"><h2>Actividad</h2></div>
  <div class="table-wrap">
    <table id="channels" hidden>
      <thead><tr><th>Canal</th><th>Activo</th><th>Sin tráfico</th><th>Clientes</th><th>Calidad</th><th>Búfer</th></tr></thead>
      <tbody id="channel-body"></tbody>
    </table>
    <p id="empty" class="empty">No hay canales abiertos ahora mismo.</p>
  </div>
  <noscript><p>Activa JavaScript para recibir actualizaciones sin recargar la página.</p></noscript>
</main>
<script id="initial-data" type="application/json">__INITIAL_DATA__</script>
<script>
(() => {
  const byId = id => document.getElementById(id);
  const table = byId('channels');
  const tbody = byId('channel-body');
  const empty = byId('empty');
  const endpoint = new URL('status.json', window.location.href);
  const expanded = new Set();
  let timer = 0;
  let loading = false;

  function duration(value) {
    const total = Math.max(0, Math.floor(Number(value) || 0));
    const h = Math.floor(total / 3600);
    const m = Math.floor((total % 3600) / 60);
    const s = total % 60;
    if (h) return `${h}h ${String(m).padStart(2, '0')}m`;
    if (m) return `${m}m ${String(s).padStart(2, '0')}s`;
    return `${s}s`;
  }

  function element(tag, className, text) {
    const node = document.createElement(tag);
    if (className) node.className = className;
    if (text !== undefined) node.textContent = text;
    return node;
  }

  function cell(label, className, text) {
    const td = element('td', className, text);
    td.dataset.label = label;
    return td;
  }

  function channelRows(ch) {
    const clients = Array.isArray(ch.clients) ? ch.clients : [];
    const expandable = clients.length > 0;
    const open = expandable && expanded.has(ch.slug);
    const summary = document.createElement('tr');
    const details = document.createElement('tr');
    const detailsId = `client-details-${ch.slug || 'channel'}`;
    summary.className = `channel-summary${expandable ? ' has-clients' : ''}`;
    summary.dataset.slug = ch.slug || '';
    summary.tabIndex = expandable ? 0 : -1;
    summary.setAttribute('role', 'button');
    summary.setAttribute('aria-expanded', String(open));
    summary.setAttribute('aria-controls', detailsId);
    if (!expandable) summary.setAttribute('aria-disabled', 'true');
    const nameCell = cell('Canal');
    const name = element('div', 'channel');
    if (ch.logo) {
      const logo = element('img', 'logo');
      logo.src = ch.logo;
      logo.alt = '';
      logo.loading = 'lazy';
      logo.referrerPolicy = 'no-referrer';
      logo.addEventListener('error', () => logo.replaceWith(element('span', 'logo-ph', 'TV')));
      name.append(logo);
    } else {
      name.append(element('span', 'logo-ph', 'TV'));
    }
    name.append(element('strong', '', ch.name || ch.slug || 'Canal'));
    nameCell.append(name);
    summary.append(nameCell);
    summary.append(cell('Activo', 'mono', duration(ch.active_s)));
    summary.append(cell('Sin tráfico', 'mono', duration(ch.idle_s)));

    const clientsCell = cell('Clientes');
    const countWrap = element('span', 'client-count-wrap');
    const count = element('span', 'client-count', String(clients.length));
    count.setAttribute('aria-label', `${clients.length} cliente${clients.length === 1 ? '' : 's'} conectado${clients.length === 1 ? '' : 's'}`);
    countWrap.append(count);
    if (expandable) {
      const arrow = element('span', 'client-arrow', '⌄');
      arrow.setAttribute('aria-hidden', 'true');
      countWrap.append(arrow);
    }
    clientsCell.append(countWrap);
    summary.append(clientsCell);
    summary.append(cell('Calidad', 'mono', ch.quality || '—'));
    summary.append(cell('Búfer', 'mono', `${Number(ch.cached) || 0} seg.`));

    details.id = detailsId;
    details.className = 'client-details';
    details.hidden = !open;
    const detailsCell = cell('Clientes');
    detailsCell.colSpan = 6;
    const clientList = element('ul', 'client-list');
    clients.forEach(client => {
      const item = element('li', 'client');
      item.append(element('div', 'ua', client.ua || 'Cliente sin identificar'));
      const metadata = [client.ip, client.connected].filter(Boolean).join(' · ');
      if (metadata) item.append(element('div', 'client-meta', metadata));
      clientList.append(item);
    });
    detailsCell.append(clientList);
    details.append(detailsCell);

    function setOpen(value) {
      if (!expandable) return;
      if (value) expanded.add(ch.slug); else expanded.delete(ch.slug);
      summary.setAttribute('aria-expanded', String(value));
      details.hidden = !value;
    }
    if (expandable) {
      summary.addEventListener('click', () => setOpen(!expanded.has(ch.slug)));
      summary.addEventListener('keydown', event => {
        if (event.key !== 'Enter' && event.key !== ' ') return;
        event.preventDefault();
        setOpen(!expanded.has(ch.slug));
      });
    }
    return [summary, details];
  }

  function render(data) {
    const live = Array.isArray(data.live) ? data.live : [];
    byId('open').textContent = String(Number(data.open) || 0);
    byId('viewers').textContent = String(live.reduce((sum, ch) => sum + (Number(ch.viewers) || 0), 0));
    byId('total').textContent = String(Number(data.channels_total) || 0);
    const left = data.token_left_s;
    const tokenCard = byId('token-card');
    tokenCard.classList.toggle('warning', !data.token_ok || left === null || Number(left) < 1800);
    byId('token').textContent = !data.token_ok ? 'No disponible' : left === null ? 'Desconocido' : Number(left) <= 0 ? 'Caducado' : duration(left);
    const fragment = document.createDocumentFragment();
    const slugs = new Set(live.map(ch => ch.slug));
    expanded.forEach(slug => { if (!slugs.has(slug)) expanded.delete(slug); });
    live.forEach(ch => channelRows(ch).forEach(row => fragment.append(row)));
    tbody.replaceChildren(fragment);
    table.hidden = live.length === 0;
    empty.hidden = live.length !== 0;
  }

  function schedule(delay) {
    window.clearTimeout(timer);
    timer = window.setTimeout(refresh, delay);
  }

  async function refresh() {
    if (loading) return;
    loading = true;
    const controller = new AbortController();
    const timeout = window.setTimeout(() => controller.abort(), 5000);
    try {
      const response = await fetch(endpoint, {cache: 'no-store', signal: controller.signal});
      if (!response.ok) throw new Error(`HTTP ${response.status}`);
      render(await response.json());
    } catch (error) {
      // Keep the last valid data on screen and retry silently.
    } finally {
      window.clearTimeout(timeout);
      loading = false;
      schedule(document.hidden ? 12000 : 4000);
    }
  }

  try { render(JSON.parse(byId('initial-data').textContent)); } catch (error) { /* next poll repairs it */ }
  document.addEventListener('visibilitychange', () => schedule(document.hidden ? 12000 : 0));
  window.addEventListener('online', () => schedule(0));
  schedule(0);
})();
</script>
</body>
</html>
"""
    return page.replace("__INITIAL_DATA__", initial_json)


async def handle_status(request: web.Request) -> web.Response:
    state: AppState = request.app["state"]
    state.playlist.maybe_reload(state.cfg["default_ua"], state.cfg["referer"])
    data = session_snapshot(state)
    return web.Response(text=render_status_page(data), content_type="text/html", charset="utf-8")


def fmt_epg_time(value: datetime) -> str:
    return value.astimezone(ZoneInfo("Europe/Madrid")).strftime("%H:%M")


def render_epg_page(data: dict) -> str:
    now = datetime.now(timezone.utc)
    rows = []
    for item in data["rows"]:
        ch: Channel = item["channel"]
        logo = ch.logo
        logo_html = (
            f"<img src='{html.escape(logo, quote=True)}' alt='' referrerpolicy='no-referrer' loading='lazy'>"
            if logo else "<span class='logo'></span>"
        )
        programme_html = []
        search_terms = [ch.name, ch.tvg_id]
        for p in item["programmes"]:
            visible_start = max(now, p["start"])
            minutes = max(1, (p["stop"] - visible_start).total_seconds() / 60)
            width = max(110, round(minutes * 2))
            is_now = p["start"] <= now < p["stop"]
            is_next = p is item["next"]
            subtitle = f"<span class='subtitle'>{html.escape(p['subtitle'])}</span>" if p["subtitle"] else ""
            programme_html.append(
                f"<div class='programme {'now' if is_now else ''}{' next' if is_next else ''}' style='--duration:{width}px'>"
                f"<span class='label'>{'Ahora · ' if is_now else ('Después · ' if is_next else '')}{fmt_epg_time(p['start'])}–{fmt_epg_time(p['stop'])}</span>"
                f"<strong>{html.escape(p['title'])}</strong>{subtitle}</div>"
            )
        if not programme_html:
            programme_html.append("<div class='programme empty-programme'><span class='muted'>Sin datos</span></div>")
        search = html.escape(" ".join(search_terms), quote=True)
        rows.append(
            f"<article class='channel' data-search='{search}'>"
            f"<div class='channel-name'>{logo_html}<strong>{html.escape(ch.name)}</strong></div>"
            f"<div class='schedule'>{''.join(programme_html)}</div></article>"
        )
    notice = ""
    if data["error"]:
        notice = f"<p class='notice'>No se ha podido actualizar el EPG: {html.escape(data['error'])}</p>"
    return f"""<!DOCTYPE html>
<html lang="es">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
<meta http-equiv="refresh" content="900">
<title>Guía TV</title>
<style>
  :root {{ color-scheme: dark; }} * {{ box-sizing: border-box; }}
  body {{ margin:0; background:#0f1115; color:#e8eaed; font:15px/1.4 ui-sans-serif,system-ui,sans-serif; }}
  main {{ max-width:none; margin:0; padding:28px 5% 48px; }}
  header {{ margin-bottom:18px; }} h1 {{ margin:0; font-size:1.4rem; }} .muted,.label {{ color:#9aa0a6; }}
  input {{ display:block; width:min(100%, 360px); margin:0 auto 14px; padding:9px 12px; border:1px solid #2a2f3a; border-radius:10px; background:#1a1d24; color:inherit; font:inherit; }}
  .epg-scroll {{ overflow:auto; padding-bottom:10px; }} .timeline {{ width:max-content; min-width:100%; }}
  .guide-head,.channel {{ display:grid; grid-template-columns:180px max-content; }}
  .guide-head {{ color:#9aa0a6; font-size:.76rem; font-weight:600; text-transform:uppercase; letter-spacing:.04em; padding:0 14px 7px; }}
  .guide-head span:last-child {{ padding-left:14px; }}
  .channel {{ background:#1a1d24; border:1px solid #2a2f3a; border-radius:12px; overflow:hidden; margin-bottom:8px; }}
  .channel[hidden] {{ display:none; }}
  .channel-name {{ position:sticky; left:0; z-index:1; display:flex; align-items:center; gap:10px; width:180px; padding:12px 14px; border-right:1px solid #2a2f3a; background:#1a1d24; }}
  img,.logo {{ width:32px; height:32px; object-fit:contain; flex:none; }} .logo {{ display:block; }}
  .schedule {{ display:flex; }}
  .programme {{ flex:0 0 var(--duration); width:var(--duration); min-height:64px; padding:10px 14px; display:grid; gap:2px; border-left:1px solid #2a2f3a; }}
  .programme.now {{ background:#16351f; box-shadow:inset 3px 0 #64d98b; }} .programme.now .label {{ color:#8fe3aa; }}
  .programme strong,.subtitle {{ overflow:hidden; text-overflow:ellipsis; white-space:nowrap; }} .empty-programme {{ --duration:180px; }}
  .label {{ font-size:.76rem; text-transform:uppercase; letter-spacing:.04em; }} .subtitle {{ color:#c2c6cc; font-size:.86rem; }}
  .notice {{ padding:12px 14px; border-radius:10px; background:#3a2d15; color:#f0d48a; }}
  @media (max-width:700px) {{
    main {{ padding:16px 12px 28px; }}
    .guide-head {{ display:none; }}
    .epg-scroll {{ overflow:visible; }} .timeline {{ width:auto; }}
    .channel {{ display:block; margin-bottom:10px; }}
    .channel-name {{ position:static; width:auto; padding:11px 13px; border-right:0; border-bottom:1px solid #2a2f3a; }}
    img,.logo {{ width:30px; height:30px; }}
    .schedule {{ display:block; }}
    .programme {{ width:auto; min-height:0; padding:10px 13px; border-left:0; }}
    .programme:nth-child(n+3) {{ display:none; }} .programme + .programme {{ border-top:1px solid #2a2f3a; }}
    .programme strong,.subtitle {{ white-space:normal; }}
  }}
</style>
</head>
<body><main>
  <header><h1>Guía TV</h1></header>
  {notice}
  <input id="filter" type="search" placeholder="Buscar canal" autocomplete="off">
  <div class="epg-scroll"><section class="timeline"><div class="guide-head"><span>Canal</span><span>Programación · desliza para avanzar →</span></div>{''.join(rows)}</section></div>
</main>
<script>document.getElementById('filter').addEventListener('input',e=>{{const q=e.target.value.trim().toLocaleLowerCase('es');document.querySelectorAll('.channel').forEach(x=>x.hidden=q&&!x.dataset.search.toLocaleLowerCase('es').includes(q));}});</script>
</body></html>"""


async def handle_epg(request: web.Request) -> web.Response:
    state: AppState = request.app["state"]
    state.playlist.maybe_reload(state.cfg["default_ua"], state.cfg["referer"])
    data = await state.epg.snapshot()
    return web.Response(text=render_epg_page(data), content_type="text/html", charset="utf-8")


async def handle_status_json(request: web.Request) -> web.Response:
    state: AppState = request.app["state"]
    state.playlist.maybe_reload(state.cfg["default_ua"], state.cfg["referer"])
    return web.json_response(session_snapshot(state))


async def on_startup(app: web.Application) -> None:
    state: AppState = app["state"]
    hls = Path(state.cfg["hls_dir"])
    empty_dir(hls)
    Path(state.cfg["log_dir"]).mkdir(parents=True, exist_ok=True)
    state.playlist.maybe_reload(state.cfg["default_ua"], state.cfg["referer"])
    state.tokens.get()
    await state.origin.start()
    app["reaper"] = asyncio.create_task(state.reaper())
    log.info("escuchando %s:%s  %s canales", state.cfg["listen_host"], state.cfg["listen_port"], len(state.playlist.channels))


async def on_shutdown(app: web.Application) -> None:
    # Stop producers before aiohttp waits for infinite stream responses.
    state: AppState = app["state"]
    app["reaper"].cancel()
    await asyncio.gather(app["reaper"], return_exceptions=True)
    await asyncio.gather(*(sess.stop() for sess in list(state.sessions.values())))


async def on_cleanup(app: web.Application) -> None:
    state: AppState = app["state"]
    app["reaper"].cancel()
    for slug in list(state.sessions):
        await state._drop(slug)
    empty_dir(Path(state.cfg["hls_dir"]))
    await state.origin.close()


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    cfg = load_config()
    log_dir = Path(cfg["log_dir"])
    log_dir.mkdir(parents=True, exist_ok=True)
    file_handler = RotatingFileHandler(
        log_dir / "proxy-shaka.log",
        maxBytes=5 * 1024 * 1024,
        backupCount=3,
        encoding="utf-8",
    )
    file_handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    logging.getLogger().addHandler(file_handler)
    state = AppState(cfg)
    app = web.Application()
    app["state"] = state
    app.router.add_get("/playlist.m3u8", handle_playlist)
    app.router.add_get("/live/{slug}/stream.ts", handle_stream)
    app.router.add_get("/live/{slug}/index.m3u8", handle_live_index)
    app.router.add_get("/live/{slug}/{name}", handle_live_seg)
    app.router.add_get("/status", handle_status)
    app.router.add_get("/status.json", handle_status_json)
    app.router.add_get("/epg", handle_epg)
    app.on_startup.append(on_startup)
    app.on_shutdown.append(on_shutdown)
    app.on_cleanup.append(on_cleanup)
    web.run_app(app, host=cfg["listen_host"], port=int(cfg["listen_port"]), print=None, shutdown_timeout=3)


if __name__ == "__main__":
    main()
