"""Text subtitles (TTML/WebVTT) for the MPEG-TS output.

MPEG-TS players only understand bitmap subtitles: FFmpeg writes TTML or
WebVTT into a TS as opaque data that VLC, ExoPlayer or Kodi cannot show. This
module reads the cues out of DASH ``stpp``/``wvtt`` fragments, Smooth TTML
fragments or raw TTML/WebVTT segments, renders them with Pillow, and encodes
them as DVB subtitles (ETSI EN 300 743) inside a small TS that FFmpeg copies
into the output segment.
"""
from __future__ import annotations

import math
import re
import struct
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from typing import Iterable, Optional

import smooth_streaming as smooth

WIDTH, HEIGHT = 1920, 1080   # DVB display definition; players scale it to the video
SUB_PID, PMT_PID = 0x100, 0x1000
# Timestamps inside the subtitle TS are relative to the segment plus this base,
# so they never go negative and never wrap (the input gets -itsoffset).
TS_BASE = 10.0
MAX_LINES = 5
FONT_PATHS = (
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
    "/usr/share/fonts/dejavu/DejaVuSans-Bold.ttf",
    "/usr/share/fonts/TTF/DejaVuSans-Bold.ttf",
)


@dataclass
class Cue:
    start: float  # seconds on the media timeline
    end: float
    text: str


# ----------------------------------------------------------------------- TTML
def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _attr(elem: ET.Element, name: str) -> Optional[str]:
    for key, value in elem.attrib.items():
        if _local(key) == name:
            return value
    return None


def _ttml_time(value: Optional[str], rates: dict) -> Optional[float]:
    if not value:
        return None
    value = value.strip()
    m = re.fullmatch(r"(\d+):(\d{2}):(\d{2}(?:\.\d+)?)(?::(\d+(?:\.\d+)?))?", value)
    if m:
        h, mi, s, frames = m.groups()
        return int(h) * 3600 + int(mi) * 60 + float(s) + (float(frames) / rates["frame"] if frames else 0.0)
    m = re.fullmatch(r"(\d+(?:\.\d+)?)(h|ms|m|s|f|t)", value)
    if not m:
        return None
    n, unit = float(m.group(1)), m.group(2)
    return n * {"h": 3600, "m": 60, "s": 1, "ms": 0.001}.get(unit, 0) if unit not in ("f", "t") \
        else n / (rates["frame"] if unit == "f" else rates["tick"])


def _text(elem: ET.Element) -> str:
    parts = [elem.text or ""]
    for child in elem:
        parts.append("\n" if _local(child.tag) == "br" else _text(child))
        parts.append(child.tail or "")
    return "".join(parts)


def _clean(text: str) -> str:
    lines = (re.sub(r"[ \t\r\f\v]+", " ", line).strip() for line in text.split("\n"))
    return "\n".join(line for line in lines if line)


def parse_ttml(doc: bytes) -> list[tuple[float, Optional[float], str]]:
    """[(begin, end or None, text)] in document seconds."""
    root = ET.fromstring(doc)
    frame = float(_attr(root, "frameRate") or 30)
    multiplier = (_attr(root, "frameRateMultiplier") or "1 1").split()
    if len(multiplier) == 2 and float(multiplier[1]):
        frame *= float(multiplier[0]) / float(multiplier[1])
    rates = {"frame": frame or 30.0, "tick": float(_attr(root, "tickRate") or 0) or frame or 1.0}
    out: list[tuple[float, Optional[float], str]] = []

    def walk(elem: ET.Element, base: float, parent_end: Optional[float]) -> None:
        begin = _ttml_time(_attr(elem, "begin"), rates)
        start = base + (begin or 0.0)
        end = _ttml_time(_attr(elem, "end"), rates)
        dur = _ttml_time(_attr(elem, "dur"), rates)
        stop = base + end if end is not None else start + dur if dur is not None else parent_end
        if parent_end is not None and stop is not None:
            stop = min(stop, parent_end)
        if _local(elem.tag) == "p":
            text = _clean(_text(elem))
            if text:
                out.append((start, stop, text))
            return
        for child in elem:
            walk(child, start, stop)

    for child in root:
        if _local(child.tag) == "body":
            walk(child, 0.0, None)
    return out


# --------------------------------------------------------------------- WebVTT
_VTT_TIME = r"(?:(\d+):)?(\d{2}):(\d{2})\.(\d{3})"


def _vtt_seconds(m: tuple) -> float:
    h, mi, s, ms = m
    return int(h or 0) * 3600 + int(mi) * 60 + int(s) + int(ms) / 1000


def _vtt_text(text: str) -> str:
    text = re.sub(r"<[^>]*>", "", text)
    for entity, char in (("&lt;", "<"), ("&gt;", ">"), ("&nbsp;", " "), ("&amp;", "&")):
        text = text.replace(entity, char)
    return _clean(text)


def parse_webvtt(text: str) -> list[tuple[float, Optional[float], str]]:
    out = []
    for block in re.split(r"\n\s*\n", text.replace("\r\n", "\n")):
        lines = block.strip("\n").split("\n")
        for i, line in enumerate(lines):
            m = re.match(rf"\s*{_VTT_TIME}\s*-->\s*{_VTT_TIME}", line)
            if m:
                body = _vtt_text("\n".join(lines[i + 1:]))
                if body:
                    out.append((_vtt_seconds(m.groups()[:4]), _vtt_seconds(m.groups()[4:]), body))
                break
    return out


# ------------------------------------------------------------------ fragments
def init_timescale(init: bytes) -> Optional[int]:
    """mdhd timescale of the first track of an init segment."""
    def find(data: bytes, start: int, end: int, path: list[bytes]) -> Optional[int]:
        for kind, pos, size in smooth.boxes(data, start, end):
            if kind != path[0]:
                continue
            if len(path) == 1:
                version = data[pos + 8]
                return struct.unpack_from(">I", data, pos + (28 if version == 1 else 20))[0]
            return find(data, pos + 8, pos + size, path[1:])
        return None
    try:
        return find(init, 0, len(init), [b"moov", b"trak", b"mdia", b"mdhd"])
    except (smooth.SmoothError, struct.error, IndexError):
        return None


def _fragment_samples(data: bytes, timescale: int, t: int, d: int) -> list[tuple[float, float, bytes]]:
    """[(start, end, payload)] in seconds for each sample of a moof/mdat fragment."""
    out: list[tuple[float, float, bytes]] = []
    moof = None
    for kind, pos, size in smooth.boxes(data):
        if kind == b"moof":
            moof = (pos, size)
            continue
        if kind != b"mdat" or moof is None:
            continue
        traf = next(((p, s) for k, p, s in smooth.boxes(data, moof[0] + 8, sum(moof)) if k == b"traf"), None)
        if traf is None:
            continue
        base, default_dur, default_size, runs = t, 0, 0, []
        for k, p, s in smooth.boxes(data, traf[0] + 8, sum(traf)):
            flags = struct.unpack_from(">I", data, p + 8)[0] & 0xFFFFFF
            if k == b"tfdt":
                base = struct.unpack_from(">Q" if data[p + 8] == 1 else ">I", data, p + 12)[0]
            elif k == b"tfhd":
                off = p + 16 + (8 if flags & 1 else 0) + (4 if flags & 2 else 0)
                if flags & 8:
                    default_dur = struct.unpack_from(">I", data, off)[0]
                    off += 4
                if flags & 0x10:
                    default_size = struct.unpack_from(">I", data, off)[0]
            elif k == b"trun":
                count = struct.unpack_from(">I", data, p + 12)[0]
                off = p + 16
                data_offset = None
                if flags & 1:
                    data_offset = struct.unpack_from(">i", data, off)[0]
                    off += 4
                off += 4 if flags & 4 else 0
                samples = []
                for _ in range(count):
                    dur = size_ = None
                    if flags & 0x100:
                        dur = struct.unpack_from(">I", data, off)[0]
                        off += 4
                    if flags & 0x200:
                        size_ = struct.unpack_from(">I", data, off)[0]
                        off += 4
                    off += 4 * bool(flags & 0x400) + 4 * bool(flags & 0x800)
                    samples.append((dur, size_))
                runs.append((data_offset, samples))
        payload_at = pos + 8
        clock = base
        for data_offset, samples in runs:
            at = moof[0] + data_offset if data_offset is not None else payload_at
            for dur, size_ in samples:
                dur = dur if dur is not None else default_dur or (d if len(samples) == 1 else 0)
                size_ = size_ if size_ is not None else default_size or (pos + size - at)
                out.append((clock / timescale, (clock + dur) / timescale, data[at:at + size_]))
                at += size_
                clock += dur
            payload_at = at
        if not runs:  # no trun: the whole mdat is one sample
            out.append((base / timescale, (base + d) / timescale, data[pos + 8:pos + size]))
        moof = None
    return out


def _wvtt_cues(sample: bytes) -> list[str]:
    texts = []
    for kind, pos, size in smooth.boxes(sample):
        if kind == b"vttc":
            for k, p, s in smooth.boxes(sample, pos + 8, pos + size):
                if k == b"payl":
                    text = _vtt_text(sample[p + 8:p + s].decode("utf-8", "replace"))
                    if text:
                        texts.append(text)
    return texts


def _document_cues(doc: bytes, start: float, end: float) -> list[Cue]:
    """Cues of one TTML/WebVTT document that covers [start, end) on the media
    timeline. Documents may use media time or time relative to the sample."""
    head = doc.lstrip()[:64]
    if head.startswith(b"WEBVTT"):
        raw = parse_webvtt(doc.decode("utf-8", "replace"))
    else:
        raw = parse_ttml(doc)
    if not raw:
        return []
    span = end - start
    relative = start > 60 and all(b <= span + 1 for b, _e, _t in raw)
    shift = start if relative else 0.0
    cues = []
    for b, e, text in raw:
        b, e = b + shift, (e + shift if e is not None else end)
        b, e = max(b, start), min(e, end)
        if e > b:
            cues.append(Cue(b, e, text))
    return cues


def segment_cues(data: bytes, timescale: int, t: int, d: int) -> list[Cue]:
    """Cues of one subtitle segment that starts at t (duration d, in timescale)."""
    start, end = t / timescale, (t + d) / timescale
    try:
        if data[4:8] in (b"moof", b"styp", b"sidx", b"ftyp", b"moov", b"emsg"):
            cues = []
            for s0, s1, payload in _fragment_samples(data, timescale, t, d):
                if payload[4:8] in (b"vttc", b"vtte"):
                    cues += [Cue(s0, s1, text) for text in _wvtt_cues(payload)]
                elif payload.strip():
                    cues += _document_cues(payload, s0, s1)
            return cues
        return _document_cues(data, start, end)
    except (ET.ParseError, smooth.SmoothError, struct.error, ValueError, UnicodeError):
        return []


def merge(cues: Iterable[Cue]) -> list[Cue]:
    """Join pieces of the same cue split across samples or fragments."""
    out: list[Cue] = []
    for cue in sorted(cues, key=lambda c: (c.start, c.text)):
        same = next((c for c in out if c.text == cue.text and c.start <= cue.start <= c.end + 0.05), None)
        if same:
            same.end = max(same.end, cue.end)
        else:
            out.append(Cue(cue.start, cue.end, cue.text))
    return out


# ------------------------------------------------------------------ rendering
_font = None


def _load_font():
    global _font
    if _font is None:
        from PIL import ImageFont

        size = HEIGHT // 19
        for path in FONT_PATHS:
            try:
                _font = ImageFont.truetype(path, size)
                break
            except OSError:
                continue
        else:
            _font = ImageFont.load_default(size)
    return _font


def _wrap(text: str, font, width: int) -> list[str]:
    lines = []
    for paragraph in text.split("\n"):
        line = ""
        for word in paragraph.split(" "):
            candidate = f"{line} {word}".strip()
            if line and font.getlength(candidate) > width:
                lines.append(line)
                line = word
            else:
                line = candidate
        lines.append(line)
    return lines


def render(text: str) -> tuple[int, int, bytes]:
    """(width, height, pixels): 1 = white text, 2 = black outline, 0 = clear."""
    from PIL import Image, ImageDraw

    font = _load_font()
    stroke = max(2, HEIGHT // 300)
    lines = _wrap(text, font, int(WIDTH * 0.88))[-MAX_LINES:]
    ascent, descent = font.getmetrics()
    line_height = ascent + descent + HEIGHT // 120
    widths = [int(font.getlength(line)) for line in lines]
    width = min(WIDTH, max(widths) + 4 * stroke + 4)
    width += width % 2
    height = line_height * len(lines) + 2 * stroke
    height += height % 2  # two fields of equal height
    img = Image.new("L", (width, height), 0)
    draw = ImageDraw.Draw(img)
    draw.fontmode = "1"  # three exact colours, no anti-aliasing shades
    for i, (line, w) in enumerate(zip(lines, widths)):
        draw.text(((width - w) // 2, stroke + i * line_height), line, font=font,
                  fill=1, stroke_width=stroke, stroke_fill=2)
    return width, height, img.tobytes()


# ------------------------------------------------------------- DVB subtitles
def _nibbles_4bit(row: bytes) -> list[int]:
    """One line as a 4-bit/pixel code string (EN 300 743, 7.2.5.2)."""
    out: list[int] = []
    x, w = 0, len(row)
    while x < w:
        c = row[x]
        n = 1
        while x + n < w and row[x + n] == c and n < 280:
            n += 1
        if n == 8:
            n = 7
        x += n
        if c and n < 4:
            out += [c] * n
        elif not c and n == 1:
            out += [0, 0xC]
        elif not c and n == 2:
            out += [0, 0xD]
        elif not c and n <= 9:
            out += [0, n - 2]
        elif n <= 7:
            out += [0, 0x8 | (n - 4), c]
        elif n <= 24:
            out += [0, 0xE, n - 9, c]
        else:
            out += [0, 0xF, (n - 25) >> 4, (n - 25) & 15, c]
    out += [0, 0]  # end of string
    if len(out) % 2:
        out.append(0)
    return out


def _field(pixels: bytes, width: int, rows: range) -> bytes:
    out = bytearray()
    for y in rows:
        nib = _nibbles_4bit(pixels[y * width:(y + 1) * width])
        out.append(0x11)
        out += bytes((nib[i] << 4) | nib[i + 1] for i in range(0, len(nib), 2))
        out.append(0xF0)  # end of object line
    return bytes(out)


def _segment(kind: int, payload: bytes, page: int = 1) -> bytes:
    return struct.pack(">BBHH", 0x0F, kind, page, len(payload)) + payload


def display_set(text: Optional[str], timeout: int, version: int) -> bytes:
    """DVB subtitle PES payload that shows ``text`` (None clears the screen).
    Every set is a mode change, so a player can start from any of them."""
    v = version & 15
    segments = [_segment(0x14, struct.pack(">BHH", (v << 4) | 0x7, WIDTH - 1, HEIGHT - 1))]
    if text:
        width, height, pixels = render(text)
        x, y = (WIDTH - width) // 2, HEIGHT - height - HEIGHT // 14
        top, bottom = _field(pixels, width, range(0, height, 2)), _field(pixels, width, range(1, height, 2))
        segments += [
            _segment(0x10, struct.pack(">BBBBHH", timeout, (v << 4) | (2 << 2) | 3, 0, 0xFF, x, y)),
            _segment(0x11, struct.pack(">BBHHBBBB", 0, (v << 4) | 0xF, width, height,
                                       (2 << 5) | (2 << 2) | 3, 0, 0, 0x03)
                     + struct.pack(">HHH", 0, 0, 0xF000)),
            _segment(0x12, struct.pack(">BB", 0, (v << 4) | 0xF)
                     + bytes((1, 0x5F, 235, 128, 128, 0, 2, 0x5F, 16, 128, 128, 0))),
            _segment(0x13, struct.pack(">HBHH", 0, (v << 4) | 0x1, len(top), len(bottom)) + top + bottom),
        ]
    else:
        segments.append(_segment(0x10, struct.pack(">BB", timeout, (v << 4) | (2 << 2) | 3)))
    segments.append(_segment(0x80, b""))
    return b"\x20\x00" + b"".join(segments) + b"\xff"


def timeline(cues: list[Cue], start: float, end: float) -> list[tuple[float, Optional[str], float]]:
    """[(at, text or None, until)]: what is on screen from each change point in
    [start, end). The first entry is at ``start`` so every segment carries the
    stream and repeats the cue that is already showing."""
    points = {start} | {c.start for c in cues if start < c.start < end} | {c.end for c in cues if start < c.end < end}
    ends = sorted({c.start for c in cues} | {c.end for c in cues})
    out: list[tuple[float, Optional[str], float]] = []
    for at in sorted(points):
        active = sorted((c for c in cues if c.start <= at < c.end), key=lambda c: c.start)
        text = "\n".join(c.text for c in active) or None
        if out and out[-1][1] == text:
            continue
        until = next((p for p in ends if p > at), at + 1.0)
        out.append((at, text, until))
    return out


# ---------------------------------------------------------------- transport
def _crc32(data: bytes) -> int:
    crc = 0xFFFFFFFF
    for byte in data:
        crc ^= byte << 24
        for _ in range(8):
            crc = ((crc << 1) ^ 0x04C11DB7) & 0xFFFFFFFF if crc & 0x80000000 else (crc << 1) & 0xFFFFFFFF
    return crc


def _section(table_id: int, ext: int, body: bytes) -> bytes:
    head = struct.pack(">BHHBBB", table_id, 0xB000 | (len(body) + 9), ext, 0xC1, 0, 0)
    return head + body + struct.pack(">I", _crc32(head + body))


def _packets(pid: int, payload: bytes, counter: list[int]) -> bytes:
    out = bytearray()
    first = True
    while payload or first:
        room = 184
        chunk = payload[:room]
        payload = payload[len(chunk):]
        head = struct.pack(">BHB", 0x47, (0x4000 if first else 0) | pid, 0x10 | counter[0])
        if len(chunk) < room:  # stuff the last packet through an adaptation field
            stuffing = room - len(chunk) - 1
            head = struct.pack(">BHB", 0x47, (0x4000 if first else 0) | pid, 0x30 | counter[0])
            head += bytes([stuffing]) + (b"\x00" + b"\xff" * (stuffing - 1) if stuffing else b"")
        out += head + chunk
        counter[0] = (counter[0] + 1) & 15
        first = False
    return bytes(out)


def _pes(payload: bytes, seconds: float) -> bytes:
    pts = round(seconds * 90000) & ((1 << 33) - 1)
    stamp = bytes((0x21 | ((pts >> 29) & 0x0E), (pts >> 22) & 0xFF, 0x01 | ((pts >> 14) & 0xFE),
                   (pts >> 7) & 0xFF, 0x01 | ((pts << 1) & 0xFE)))
    body = bytes((0x84, 0x80, 5)) + stamp + payload
    return b"\x00\x00\x01\xbd" + struct.pack(">H", len(body)) + body


def transport(sets: list[tuple[float, bytes]], lang: str) -> bytes:
    """A minimal TS (PAT, PMT, one DVB subtitle PID) carrying the display sets."""
    code = (lang if re.fullmatch(r"[a-z]{3}", lang or "") else "und").encode()
    pat = _section(0x00, 1, struct.pack(">HH", 1, 0xE000 | PMT_PID))
    descriptor = bytes((0x59, 8)) + code + bytes((0x10,)) + struct.pack(">HH", 1, 1)
    pmt = _section(0x02, 1, struct.pack(">HH", 0xE000 | SUB_PID, 0xF000)
                   + struct.pack(">BHH", 0x06, 0xE000 | SUB_PID, 0xF000 | len(descriptor)) + descriptor)
    counters = {0: [0], PMT_PID: [0], SUB_PID: [0]}
    out = _packets(0, b"\x00" + pat, counters[0]) + _packets(PMT_PID, b"\x00" + pmt, counters[PMT_PID])
    for seconds, payload in sets:
        out += _packets(SUB_PID, _pes(payload, seconds), counters[SUB_PID])
    return out


def segment_ts(cues: list[Cue], start: float, end: float, lang: str) -> bytes:
    """Subtitle TS for the output segment [start, end) seconds. Its clock is
    relative: feed it to FFmpeg with ``-itsoffset (start - TS_BASE)``."""
    sets = []
    for i, (at, text, until) in enumerate(timeline(cues, start, end)):
        timeout = max(1, min(255, math.ceil(until - at) + 1))
        sets.append((at - start + TS_BASE, display_set(text, timeout, i)))
    return transport(sets, lang)
