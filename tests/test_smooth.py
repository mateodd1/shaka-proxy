"""Smooth Streaming (ISM/ISML) support: manifest, init synthesis, PIFF decryption, remux."""
import asyncio
import base64
import json
import shutil
import struct
import subprocess
import tempfile
import unittest
import uuid
from fractions import Fraction
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from Crypto.Cipher import AES

import proxy
import smooth_streaming as smooth

FFMPEG = shutil.which('ffmpeg') or '/usr/lib/jellyfin-ffmpeg/ffmpeg'
FFPROBE = shutil.which('ffprobe') or '/usr/lib/jellyfin-ffmpeg/ffprobe'
HAVE_MEDIA = Path(FFMPEG).exists() and Path(FFPROBE).exists()
KEY = '00112233445566778899aabbccddeeff'
KID = '8899aabbccddeeff0011223344556677'

MANIFEST = b"""<?xml version="1.0" encoding="utf-8"?>
<SmoothStreamingMedia MajorVersion="2" MinorVersion="0" Duration="0" IsLive="TRUE" TimeScale="10000000">
<StreamIndex Type="video" Name="video" Url="QualityLevels({bitrate})/Fragments(video={start time})">
<QualityLevel Index="0" Bitrate="800000" FourCC="H264" MaxWidth="640" MaxHeight="360" CodecPrivateData="000000016764001eacd940a02ff97011000003000100000300320f162d960000000168ebe3cb22c0"/>
<QualityLevel Index="1" Bitrate="2000000" FourCC="H264" MaxWidth="1280" MaxHeight="720" CodecPrivateData="000000016764001eacd940a02ff97011000003000100000300320f162d960000000168ebe3cb22c0"/>
<c t="100" d="20" r="3"/><c d="25"/>
</StreamIndex>
<StreamIndex Type="audio" Name="audio_eng" Language="eng" Url="QualityLevels({bitrate})/Fragments(audio_eng={start time})">
<QualityLevel Index="0" Bitrate="128000" FourCC="AACL" SamplingRate="48000" Channels="2" CodecPrivateData="1190"/>
<c t="100" d="20"/></StreamIndex>
<StreamIndex Type="audio" Name="audio_spa" Language="spa" Url="QualityLevels({bitrate})/Fragments(audio_spa={start time})">
<QualityLevel Index="0" Bitrate="128000" FourCC="AACL" SamplingRate="48000" Channels="2" CodecPrivateData="1190"/>
<c t="100" d="20"/></StreamIndex>
<StreamIndex Type="text" Url="x"><QualityLevel Index="0" Bitrate="1" FourCC="TTML"/></StreamIndex>
</SmoothStreamingMedia>"""


def playready_header(kid_hex: str) -> str:
    kid_b64 = base64.b64encode(uuid.UUID(kid_hex).bytes_le).decode()
    xml = f'<WRMHEADER version="4.0.0.0"><DATA><KID>{kid_b64}</KID></DATA></WRMHEADER>'.encode('utf-16-le')
    record = struct.pack('<HH', 1, len(xml)) + xml
    return base64.b64encode(struct.pack('<IH', 6 + len(record), 1) + record).decode()


def encrypt_fragment(frag: bytes, key_hex: str) -> bytes:
    """Encrypt a clear fragment like PlayReady: PIFF uuid box with 6 clear bytes per sample."""
    moof_pos, moof_size = next((p, s) for k, p, s in smooth.boxes(frag) if k == b'moof')
    traf_pos, traf_size = next((p, s) for k, p, s in smooth.boxes(frag, moof_pos + 8, moof_pos + moof_size) if k == b'traf')
    buf = bytearray(frag)
    sizes, data_offset = smooth._samples(buf, traf_pos, traf_pos + traf_size)
    off, entries = moof_pos + data_offset, b''
    for i, size in enumerate(sizes):
        iv = struct.pack('>Q', i + 1)
        clear = min(6, size)
        cipher = AES.new(bytes.fromhex(key_hex), AES.MODE_CTR, nonce=b'', initial_value=iv.ljust(16, b'\0'))
        buf[off + clear:off + size] = cipher.encrypt(bytes(buf[off + clear:off + size]))
        entries += iv + struct.pack('>H', 1) + struct.pack('>HI', clear, size - clear)
        off += size
    senc = smooth.box(b'uuid', smooth.PIFF_UUID, struct.pack('>II', 2, len(sizes)), entries)
    out = bytearray(buf[:traf_pos + traf_size]) + senc + buf[traf_pos + traf_size:]
    struct.pack_into('>I', out, moof_pos, moof_size + len(senc))
    struct.pack_into('>I', out, traf_pos, traf_size + len(senc))
    for k, p, _s in smooth.boxes(out, traf_pos + 8, traf_pos + traf_size + len(senc)):
        if k == b'trun':
            struct.pack_into('>i', out, p + 16, struct.unpack_from('>i', out, p + 16)[0] + len(senc))
    return bytes(out)


class SmoothManifestTests(unittest.TestCase):
    def test_detects_smooth_urls(self):
        for url in ('https://h/live/a.ism/Manifest', 'https://h/a.isml/manifest?t=1', 'https://h/a.ism'):
            self.assertTrue(smooth.is_smooth_url(url), url)
        for url in ('https://h/a.mpd', 'https://h/ism/manifest', 'https://h/a.m3u8'):
            self.assertFalse(smooth.is_smooth_url(url), url)

    def test_mpd_url_inside_ism_stays_dash(self):
        with tempfile.NamedTemporaryFile('w', suffix='.m3u') as fh:
            fh.write('#EXTM3U\n#EXTINF:-1,A\nhttps://h/a.ism/.mpd\n#EXTINF:-1,B\nhttps://h/b.isml/manifest\n')
            fh.flush()
            playlist = proxy.Playlist(fh.name)
            playlist.maybe_reload('ua', '')
            channels = playlist.channels
        self.assertEqual([(c.is_dash, c.is_smooth) for c in channels], [(True, False), (False, True)])

    def test_parse_and_track_selection(self):
        video, audios = proxy.select_smooth_tracks(MANIFEST, 720, url='https://h/c.isml/manifest?token=abc')
        self.assertEqual((video.height, video.bandwidth, video.timescale), (360 * 2, 2000000, 10000000))
        self.assertEqual(video.entries, [(100, 20), (120, 20), (140, 20), (160, 25)])
        self.assertEqual(video.media_tmpl, 'QualityLevels(2000000)/Fragments(video=$Time$)?token=abc')
        self.assertEqual(proxy.fill_template(video.media_tmpl, video.rep_id, 120),
                         'QualityLevels(2000000)/Fragments(video=120)?token=abc')
        self.assertEqual([a.lang for a in audios], ['spa', 'eng'])  # Spanish first, text ignored
        self.assertTrue(video.smooth and audios[0].smooth)

    def test_height_cap(self):
        video, _ = proxy.select_smooth_tracks(MANIFEST, 360)
        self.assertEqual(video.bandwidth, 800000)

    def test_rejects_other_xml(self):
        with self.assertRaises(smooth.SmoothError):
            smooth.parse_manifest(b'<MPD/>')

    def test_audio_chunks_tile_the_video_timeline(self):
        audio = [(0, 19), (19, 20), (39, 20), (59, 21), (80, 20)]  # 1 tick = 1 s, drifting boundaries
        got = [smooth.covering_entries(audio, 1, Fraction(a), Fraction(b)) for a, b in ((0, 20), (20, 40), (40, 60), (60, 80))]
        self.assertEqual(sum(got, []), audio[:4])  # each audio chunk exactly once, none lost

    def test_playready_kid_is_unswapped(self):
        manifest = MANIFEST.replace(
            b'</SmoothStreamingMedia>',
            f'<Protection><ProtectionHeader SystemID="9A04F079-9840-4286-AB92-E65BE0885F95">{playready_header(KID)}</ProtectionHeader></Protection></SmoothStreamingMedia>'.encode())
        parsed = smooth.parse_manifest(manifest)
        self.assertTrue(parsed.protected)
        self.assertEqual(parsed.kid, KID)


@unittest.skipUnless(HAVE_MEDIA, 'ffmpeg unavailable')
class SmoothMediaTests(unittest.IsolatedAsyncioTestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory(prefix='smooth-test-')
        root = Path(cls.tmp.name)
        subprocess.run(
            [FFMPEG, '-hide_banner', '-v', 'error', '-f', 'lavfi', '-i', 'testsrc2=size=320x180:rate=25',
             '-f', 'lavfi', '-i', 'sine=frequency=440:sample_rate=48000', '-t', '8',
             '-c:v', 'libx264', '-preset', 'ultrafast', '-g', '50', '-keyint_min', '50', '-sc_threshold', '0',
             '-bf', '2', '-pix_fmt', 'yuv420p', '-c:a', 'aac', '-f', 'smoothstreaming',
             '-min_frag_duration', '2000000', '-ism_lookahead', '0', str(root / 'Stream.ism')], check=True)
        cls.ism = root / 'Stream.ism'
        # FFmpeg omits `t` on the first chunk although its files are named after it.
        kind, lines = None, []
        for line in (cls.ism / 'Manifest').read_text().splitlines():
            if '<StreamIndex' in line:
                kind = 'video' if 'Type="video"' in line else 'audio'
            if line.startswith('<c n="0" d='):
                first = min(int(p.name.split('=')[1].rstrip(')')) for p in cls.ism.glob(f'*/Fragments({kind}=*)'))
                line = line.replace('<c n="0"', f'<c n="0" t="{first}"')
            lines.append(line)
        cls.manifest = '\n'.join(lines).encode()

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def make_session(self, keys=None, encrypt=False, manifest=None):
        root = Path(self.tmp.name)
        ism = self.ism

        async def fetch(ch, relative, **kwargs):
            data = (ism / relative.split('?')[0]).read_bytes()
            return encrypt_fragment(data, KEY) if encrypt and 'Fragments' in relative else data
        origin = SimpleNamespace(work_sem=asyncio.Semaphore(1), fetch_rel=fetch)
        hls = tempfile.mkdtemp(dir=self.tmp.name)
        session = proxy.ChannelSession(
            SimpleNamespace(slug='smooth', keys=keys or {}, url='http://origin/Stream.ism/Manifest', is_smooth=True),
            {'hls_dir': hls, 'ffmpeg': FFMPEG}, origin)
        session.video, session.audios = proxy.select_smooth_tracks(manifest or self.manifest, 1080)
        session.refresh = AsyncMock()
        session._prune_cache = Mock()
        return session

    async def probe(self, session, count=2):
        continuity = proxy.TSContinuity()
        output = []
        for timestamp, _ in session.video.entries[:count]:
            path = await session.remux(timestamp)
            output.append(continuity.rewrite(path.read_bytes()))
        result = await asyncio.to_thread(
            subprocess.run,
            [FFPROBE, '-v', 'warning', '-show_packets', '-show_entries',
             'packet=stream_index,dts_time,duration_time:stream=index,codec_type',
             '-of', 'json', 'pipe:0'],
            input=b''.join(output), capture_output=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stderr.decode())
        self.assertFalse(result.stderr, result.stderr.decode())
        return json.loads(result.stdout)

    def check_continuous(self, data, frames):
        expected, counts = {}, {}
        for packet in data['packets']:
            index, ts = packet['stream_index'], float(packet['dts_time'])
            self.assertGreaterEqual(ts, 0)
            if index in expected:
                self.assertAlmostEqual(ts, expected[index], delta=.0002)  # no gaps or overlaps between segments
            expected[index] = ts + float(packet['duration_time'])
            counts[index] = counts.get(index, 0) + 1
        self.assertEqual(counts[0], frames)
        self.assertEqual(len(data['streams']), 2)
        self.assertGreater(counts[1], 80)

    async def test_clear_segments_remux_with_continuous_clock(self):
        session = self.make_session()
        self.assertEqual(session.video.codecs[:5], 'avc1.')
        self.check_continuous(await self.probe(session), frames=100)
        self.assertFalse(list(session.hls_dir.glob('.tmp-*')))

    async def test_encrypted_segments_decrypt_with_channel_key(self):
        session = self.make_session(keys={KID: KEY}, encrypt=True)
        self.check_continuous(await self.probe(session), frames=100)

    async def test_encrypted_segment_without_key_fails_cleanly(self):
        session = self.make_session(encrypt=True)
        with self.assertRaises(proxy.web.HTTPBadGateway) as ctx:
            await session.remux(session.video.entries[0][0])
        self.assertIn('no key', ctx.exception.text)

    async def test_refresh_polls_once_per_chunk_and_skips_unchanged_manifest(self):
        session = self.make_session()
        del session.refresh  # use the real method
        session.video = None
        session.origin.fetch_mpd = AsyncMock(return_value=self.manifest)
        with patch.object(proxy, 'select_smooth_tracks', wraps=proxy.select_smooth_tracks) as select:
            await session.refresh()
            self.assertAlmostEqual(session._refresh_interval(), 2.0, delta=0.1)
            await session.refresh()  # inside the interval: no request
            self.assertEqual(session.origin.fetch_mpd.await_count, 1)
            session.mpd_at = 0
            await session.refresh()  # same body: fetched but not parsed again
            self.assertEqual(session.origin.fetch_mpd.await_count, 2)
            self.assertEqual(select.call_count, 1)


if __name__ == '__main__':
    unittest.main()
