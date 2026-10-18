"""Microsoft Smooth Streaming (ISM/ISML) support for shaka-proxy.

Smooth manifests describe fragmented MP4 (moof+mdat) streams that have no
initialization segment and no ``tfdt``. This module parses the manifest,
synthesizes the missing ``ftyp+moov``, stamps each fragment with its absolute
decode time, and decrypts PlayReady/PIFF (AES-CTR) samples with a key supplied
by the user, so the result can be remuxed by FFmpeg like any DASH fragment.
"""
from __future__ import annotations

import base64
import re
import struct
import uuid
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from fractions import Fraction
from typing import Iterator, Optional
from urllib.parse import urlsplit

VIDEO_FOURCC = {"H264", "AVC1", "X264", "DAVC"}
AUDIO_FOURCC = {"AACL", "AACH", "MP4A", "EC-3", "AC-3"}
TEXT_FOURCC = {"TTML", "DFXP"}
DOLBY_ENTRY = {"EC-3": b"ec-3", "AC-3": b"ac-3"}
PIFF_UUID = bytes.fromhex("a2394f525a9b4f14a2446c427c648df4")
MATRIX = struct.pack(">9I", 0x10000, 0, 0, 0, 0x10000, 0, 0, 0, 0x40000000)
AAC_RATES = [96000, 88200, 64000, 48000, 44100, 32000, 24000, 22050,
             16000, 12000, 11025, 8000, 7350]


class SmoothError(Exception):
    pass


def is_smooth_url(url: str) -> bool:
    """True for ``.../name.ism/Manifest`` and ``.../name.isml/manifest`` URLs."""
    return re.search(r"\.isml?(/|$)", urlsplit(url).path, re.I) is not None


# ------------------------------------------------------------------- manifest
@dataclass
class QualityLevel:
    bitrate: int
    fourcc: str = ""
    width: int = 0
    height: int = 0
    codec_private: bytes = b""
    channels: int = 2
    sampling_rate: int = 48000
    nal_length: int = 4


@dataclass
class Stream:
    kind: str
    name: str
    url: str
    timescale: int
    lang: str
    levels: list[QualityLevel]
    chunks: list[tuple[int, int]] = field(default_factory=list)  # (t, d) in timescale

    def supported(self) -> bool:
        allowed = {"video": VIDEO_FOURCC, "audio": AUDIO_FOURCC, "text": TEXT_FOURCC}.get(self.kind, set())
        return bool(self.levels) and self.levels[0].fourcc in allowed

    def template(self, level: QualityLevel, query: str = "") -> str:
        """Relative fragment URL with a ``$Time$`` placeholder, like a DASH template."""
        path = re.sub(r"\{bitrate\}", str(level.bitrate), self.url, flags=re.I)
        path = re.sub(r"\{start[ _]time\}", "$Time$", path, flags=re.I)
        return f"{path}?{query}" if query and "?" not in path else path


@dataclass
class Manifest:
    live: bool
    protected: bool
    kid: Optional[str]
    streams: list[Stream]


def _int(value: Optional[str], default: int = 0) -> int:
    try:
        return int(value) if value not in (None, "") else default
    except ValueError:
        return default


def _hex(value: Optional[str]) -> bytes:
    try:
        return bytes.fromhex(value or "")
    except ValueError:
        return b""


def protection_kid(header_b64: str) -> Optional[str]:
    """Content KID (hex) from a PlayReady ProtectionHeader, or None."""
    try:
        blob = base64.b64decode(header_b64)
        count = struct.unpack_from("<H", blob, 4)[0]
        pos = 6
        for _ in range(count):
            rtype, rlen = struct.unpack_from("<HH", blob, pos)
            pos += 4
            if rtype == 1:
                xml = blob[pos:pos + rlen].decode("utf-16-le", "ignore")
                m = re.search(r"<KID>([^<]+)</KID>", xml) or re.search(r'<KID[^>]*VALUE="([^"]+)"', xml)
                if m:
                    return uuid.UUID(bytes_le=base64.b64decode(m.group(1))).hex  # GUID is little-endian
            pos += rlen
    except Exception:
        pass
    return None


def parse_manifest(data: bytes, max_chunks: Optional[int] = None) -> Manifest:
    if max_chunks is not None and max_chunks < 1:
        raise ValueError("max_chunks must be positive")
    root = ET.fromstring(data)
    if root.tag != "SmoothStreamingMedia":
        raise SmoothError(f"not a Smooth Streaming manifest: <{root.tag}>")
    movie_ts = _int(root.get("TimeScale"), 10_000_000)
    streams: list[Stream] = []
    for si in root.findall("StreamIndex"):
        levels = [
            QualityLevel(
                bitrate=_int(q.get("Bitrate")),
                fourcc=(q.get("FourCC") or "").upper(),
                width=_int(q.get("MaxWidth") or q.get("Width")),
                height=_int(q.get("MaxHeight") or q.get("Height")),
                codec_private=_hex(q.get("CodecPrivateData")),
                channels=_int(q.get("Channels"), 2),
                sampling_rate=_int(q.get("SamplingRate"), 48000),
                nal_length=_int(q.get("NALUnitLengthField"), 4),
            )
            for q in si.findall("QualityLevel")
        ]
        chunks: list[tuple[int, int]] = []
        t = 0
        nodes = si.findall("c")
        tail_start = 0
        if max_chunks is not None:
            needed = max_chunks
            for index in range(len(nodes) - 1, -1, -1):
                if _int(nodes[index].get("d")) > 0:
                    needed -= max(1, _int(nodes[index].get("r"), 1))
                if needed <= 0:
                    tail_start = index
                    break
        for index, c in enumerate(nodes):
            t = _int(c.get("t"), t)
            d = _int(c.get("d"))
            if d <= 0:
                continue
            count = max(1, _int(c.get("r"), 1))
            if index >= tail_start:
                first = max(0, count - max_chunks) if max_chunks is not None else 0
                chunks.extend((t + offset * d, d) for offset in range(first, count))
            t += d * count
        if max_chunks is not None:
            chunks = chunks[-max_chunks:]
        streams.append(Stream(
            kind=(si.get("Type") or "").lower(),
            name=si.get("Name") or si.get("Type") or "",
            url=si.get("Url") or "",
            timescale=_int(si.get("TimeScale"), movie_ts),
            lang=(si.get("Language") or "").lower(),
            levels=levels,
            chunks=chunks,
        ))
    header = root.find("Protection/ProtectionHeader")
    return Manifest(
        live=(root.get("IsLive") or "").upper() == "TRUE",
        protected=root.find("Protection") is not None,
        kid=protection_kid((header.text or "").strip()) if header is not None else None,
        streams=streams,
    )


def covering_entries(entries: list[tuple[int, int]], timescale: int,
                     start: Fraction, end: Fraction) -> list[tuple[int, int]]:
    """Chunks that start in [start, end). Smooth audio and video chunks do not
    share boundaries, but assigning each audio chunk to the video segment in
    which it starts covers the timeline exactly once, without gaps or overlap."""
    return [(t, d) for t, d in entries if start <= Fraction(t, timescale) < end]


# ----------------------------------------------------------------------- MP4
def box(kind: bytes, *payload: bytes) -> bytes:
    data = b"".join(payload)
    return struct.pack(">I4s", 8 + len(data), kind) + data


def fbox(kind: bytes, flags: int, *payload: bytes) -> bytes:
    return box(kind, struct.pack(">I", flags), *payload)


def boxes(data: bytes | bytearray, start: int = 0, end: Optional[int] = None) -> Iterator[tuple[bytes, int, int]]:
    """Yield (type, offset, size) for the boxes between start and end."""
    end = len(data) if end is None else end
    while start + 8 <= end:
        size, kind = struct.unpack_from(">I4s", data, start)
        if size < 8 or start + size > end:
            raise SmoothError("corrupt MP4 box")
        yield kind, start, size
        start += size


def _desc(tag: int, payload: bytes) -> bytes:
    n, out = len(payload), [len(payload) & 0x7F]
    n >>= 7
    while n:
        out.append(0x80 | (n & 0x7F))
        n >>= 7
    return bytes([tag, *reversed(out)]) + payload


def _avcc(level: QualityLevel) -> bytes:
    nals = [n.rstrip(b"\0") for n in re.split(b"\0\0\x01", level.codec_private)[1:] if n]
    sps = [n for n in nals if n[0] & 0x1F == 7]
    pps = [n for n in nals if n[0] & 0x1F == 8]
    if not sps or not pps:
        raise SmoothError("CodecPrivateData has no SPS/PPS")
    body = bytes([1, sps[0][1], sps[0][2], sps[0][3], 0xFC | (level.nal_length - 1), 0xE1])
    body += struct.pack(">H", len(sps[0])) + sps[0] + bytes([len(pps)])
    body += b"".join(struct.pack(">H", len(n)) + n for n in pps)
    return box(b"avcC", body)


def avc_codec_string(level: QualityLevel) -> str:
    """``avc1.PPCCLL`` from the SPS in CodecPrivateData (used for display and bsf choice)."""
    try:
        avcc = _avcc(level)
        return "avc1." + avcc[9:12].hex()
    except SmoothError:
        return "avc1"


def audio_codec_string(level: QualityLevel) -> str:
    return {"EC-3": "ec-3", "AC-3": "ac-3", "AACH": "mp4a.40.5"}.get(level.fourcc, "mp4a.40.2")


def build_init(stream: Stream, level: QualityLevel) -> bytes:
    """``ftyp+moov`` for a single track (track_ID 1) synthesized from the manifest."""
    ts = stream.timescale
    if stream.kind == "video":
        entry = box(
            b"avc1", bytes(6), struct.pack(">H", 1), bytes(16),
            struct.pack(">HHIIIH", level.width, level.height, 0x480000, 0x480000, 0, 1),
            bytes(32), struct.pack(">Hh", 0x18, -1), _avcc(level))
        handler, media_header = b"vide", fbox(b"vmhd", 1, bytes(8))
        volume, size = 0, struct.pack(">II", level.width << 16, level.height << 16)
    elif level.fourcc in DOLBY_ENTRY:
        # No dac3/dec3: FFmpeg reads the stream parameters from the syncframes.
        entry = box(DOLBY_ENTRY[level.fourcc], bytes(6), struct.pack(">H", 1), bytes(8),
                    struct.pack(">HHHHI", 2, 16, 0, 0, (level.sampling_rate & 0xFFFF) << 16))
        handler, media_header = b"soun", fbox(b"smhd", 0, bytes(4))
        volume, size = 0x100, bytes(8)
    else:
        idx = AAC_RATES.index(level.sampling_rate) if level.sampling_rate in AAC_RATES else 3
        asc = level.codec_private or struct.pack(">H", (2 << 11) | (idx << 7) | (level.channels << 3))
        decoder = _desc(4, bytes([0x40, 0x15]) + bytes(3)
                        + struct.pack(">II", level.bitrate, level.bitrate) + _desc(5, asc))
        esds = fbox(b"esds", 0, _desc(3, bytes(3) + decoder + _desc(6, b"\2")))
        entry = box(b"mp4a", bytes(6), struct.pack(">H", 1), bytes(8),
                    struct.pack(">HHHHI", level.channels, 16, 0, 0, (level.sampling_rate & 0xFFFF) << 16), esds)
        handler, media_header = b"soun", fbox(b"smhd", 0, bytes(4))
        volume, size = 0x100, bytes(8)
    lang = stream.lang if re.fullmatch(r"[a-z]{3}", stream.lang) else "und"
    packed = sum((ord(c) - 0x60) << s for c, s in zip(lang, (10, 5, 0)))
    stbl = box(b"stbl", fbox(b"stsd", 0, struct.pack(">I", 1), entry), fbox(b"stts", 0, bytes(4)),
               fbox(b"stsc", 0, bytes(4)), fbox(b"stsz", 0, bytes(8)), fbox(b"stco", 0, bytes(4)))
    dinf = box(b"dinf", fbox(b"dref", 0, struct.pack(">I", 1), fbox(b"url ", 1)))
    mdia = box(b"mdia", fbox(b"mdhd", 0, struct.pack(">IIIIHH", 0, 0, ts, 0, packed, 0)),
               fbox(b"hdlr", 0, bytes(4), handler, bytes(12), b"smooth\0"),
               box(b"minf", media_header, dinf, stbl))
    trak = box(b"trak", fbox(b"tkhd", 7, struct.pack(">IIIII", 0, 0, 1, 0, 0), bytes(8),
                             struct.pack(">HHHH", 0, 0, volume, 0), MATRIX, size), mdia)
    moov = box(b"moov", fbox(b"mvhd", 0, struct.pack(">IIIII", 0, 0, ts, 0, 0x10000),
                             struct.pack(">H", 0x100), bytes(10), MATRIX, bytes(24), struct.pack(">I", 2)),
               trak, box(b"mvex", fbox(b"trex", 0, struct.pack(">IIIII", 1, 1, 0, 0, 0))))
    return box(b"ftyp", b"isom", struct.pack(">I", 512), b"isomiso2avc1mp41") + moov


def add_tfdt(frag: bytes, tfdt: int) -> bytes:
    """Set track_ID=1 and put ``tfdt`` in the moof (Smooth fragments carry none)."""
    out = bytearray()
    for kind, pos, size in boxes(frag):
        if kind != b"moof":
            out += frag[pos:pos + size]
            continue
        kids = list(boxes(frag, pos + 8, pos + size))
        traf = next((c for c in kids if c[0] == b"traf"), None)
        if not traf:
            out += frag[pos:pos + size]
            continue
        new = fbox(b"tfdt", 1 << 24, struct.pack(">Q", tfdt))
        inner = list(boxes(frag, traf[1] + 8, traf[1] + traf[2]))
        delta = len(new) - sum(s for k, _, s in inner if k == b"tfdt")
        parts = []
        for k, p, s in inner:
            part = bytearray(frag[p:p + s])
            if k == b"tfdt":
                continue
            if k == b"tfhd":
                if part[11] & 1:
                    raise SmoothError("tfhd with base_data_offset is not supported")
                struct.pack_into(">I", part, 12, 1)
                parts += [bytes(part), new]
                continue
            if k == b"trun" and part[11] & 1:  # data_offset: the moof grows by `delta`
                struct.pack_into(">i", part, 16, struct.unpack_from(">i", part, 16)[0] + delta)
            parts.append(bytes(part))
        out += box(b"moof", *[frag[p:p + s] for k, p, s in kids if k != b"traf"], box(b"traf", *parts))
    return bytes(out)


# ---------------------------------------------------------------- decryption
def _samples(frag: bytearray, traf: int, traf_end: int) -> tuple[list[int], Optional[int]]:
    """Sample sizes of a traf and the data_offset of its first trun."""
    default_size, sizes, first_offset = 0, [], None
    for kind, pos, size in boxes(frag, traf + 8, traf_end):
        if kind == b"tfhd":
            flags = struct.unpack_from(">I", frag, pos + 8)[0] & 0xFFFFFF
            off = pos + 16
            off += 8 if flags & 0x1 else 0
            off += 4 if flags & 0x2 else 0
            off += 4 if flags & 0x8 else 0
            if flags & 0x10:
                default_size = struct.unpack_from(">I", frag, off)[0]
        elif kind == b"trun":
            vf, count = struct.unpack_from(">II", frag, pos + 8)
            flags = vf & 0xFFFFFF
            off = pos + 16
            if flags & 0x1:
                if first_offset is None:
                    first_offset = struct.unpack_from(">i", frag, off)[0]
                off += 4
            off += 4 if flags & 0x4 else 0
            step = 4 * sum(bool(flags & b) for b in (0x100, 0x200, 0x400, 0x800))
            for i in range(count):
                if flags & 0x200:
                    skip = 4 if flags & 0x100 else 0
                    sizes.append(struct.unpack_from(">I", frag, off + i * step + skip)[0])
                else:
                    sizes.append(default_size)
    return sizes, first_offset


def _parse_senc(payload: bytes) -> list[tuple[bytes, list[tuple[int, int]]]]:
    """[(iv, [(clear, encrypted), ...])] from a SampleEncryptionBox (senc or PIFF uuid)."""
    flags = struct.unpack_from(">I", payload, 0)[0] & 0xFFFFFF
    pos, iv_override = 4, None
    if flags & 0x1:
        iv_override = payload[pos + 3]
        pos += 20  # AlgorithmID(3) + IV_size(1) + KID(16)
    count = struct.unpack_from(">I", payload, pos)[0]
    pos += 4
    for iv_size in ([iv_override] if iv_override else [8, 16]):  # the box does not say; try both
        try:
            p, out = pos, []
            for _ in range(count):
                iv = payload[p:p + iv_size]
                p += iv_size
                subs = []
                if flags & 0x2:
                    n = struct.unpack_from(">H", payload, p)[0]
                    p += 2
                    for _ in range(n):
                        subs.append(struct.unpack_from(">HI", payload, p))
                        p += 6
                out.append((iv, subs))
            if p == len(payload) and len(iv) == iv_size:
                return out
        except struct.error:
            continue
    raise SmoothError("unreadable SampleEncryptionBox")


def decrypt_fragment(frag: bytes, key: Optional[bytes]) -> bytes:
    """Decrypt AES-CTR samples in place; clear fragments are returned unchanged."""
    buf = bytearray(frag)
    moof = next(((p, s) for k, p, s in boxes(buf) if k == b"moof"), None)
    mdat = next(((p, s) for k, p, s in boxes(buf) if k == b"mdat"), None)
    traf = moof and next(((p, s) for k, p, s in boxes(buf, moof[0] + 8, moof[0] + moof[1])
                          if k == b"traf"), None)
    if not (moof and mdat and traf):
        return frag
    senc = None
    for kind, pos, size in boxes(buf, traf[0] + 8, traf[0] + traf[1]):
        if kind == b"senc":
            senc = bytes(buf[pos + 8:pos + size])
        elif kind == b"uuid" and bytes(buf[pos + 8:pos + 24]) == PIFF_UUID:
            senc = bytes(buf[pos + 24:pos + size])
    if senc is None:
        return frag
    if key is None:
        raise SmoothError("encrypted fragment and no key for the channel")
    from Crypto.Cipher import AES

    sizes, data_offset = _samples(buf, traf[0], traf[0] + traf[1])
    off = moof[0] + data_offset if data_offset is not None else mdat[0] + 8
    for size, (iv, subs) in zip(sizes, _parse_senc(senc)):
        cipher = AES.new(key, AES.MODE_CTR, nonce=b"", initial_value=iv.ljust(16, b"\x00"))
        if not subs:
            buf[off:off + size] = cipher.decrypt(bytes(buf[off:off + size]))
        else:
            ranges, pos = [], off
            for clear, enc in subs:
                pos += clear
                ranges.append((pos, enc))
                pos += enc
            plain = cipher.decrypt(b"".join(bytes(buf[p:p + n]) for p, n in ranges))
            cut = 0
            for p, n in ranges:
                buf[p:p + n] = plain[cut:cut + n]
                cut += n
        off += size
    return bytes(buf)


def prepare_fragment(frag: bytes, tfdt: int, key: Optional[bytes]) -> bytes:
    """Decrypt (when needed) and stamp a fragment so FFmpeg can read it."""
    return add_tfdt(decrypt_fragment(frag, key), tfdt)
