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
import test_subtitles as subtitles_test

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
        self.assertEqual([a.lang for a in audios], ['spa', 'eng'])  # Spanish first, text apart
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
        cls.ism, cls.manifest = cls.encode('Stream.ism', 'testsrc2=size=320x180:rate=25',
                                           'sine=frequency=440:sample_rate=48000')
        # A still image with silence, like a holding card: tiny fragments.
        cls.still_ism, cls.still_manifest = cls.encode('Still.ism', 'color=c=black:size=320x180:rate=25',
                                                       'anullsrc=r=48000:cl=stereo')

    @classmethod
    def encode(cls, name, video, audio):
        ism = Path(cls.tmp.name) / name
        subprocess.run(
            [FFMPEG, '-hide_banner', '-v', 'error', '-f', 'lavfi', '-i', video,
             '-f', 'lavfi', '-i', audio, '-t', '8',
             '-c:v', 'libx264', '-preset', 'ultrafast', '-g', '50', '-keyint_min', '50', '-sc_threshold', '0',
             '-bf', '2', '-pix_fmt', 'yuv420p', '-c:a', 'aac', '-f', 'smoothstreaming',
             '-min_frag_duration', '2000000', '-ism_lookahead', '0', str(ism)], check=True)
        # FFmpeg omits `t` on the first chunk although its files are named after it.
        kind, lines = None, []
        for line in (ism / 'Manifest').read_text().splitlines():
            if '<StreamIndex' in line:
                kind = 'video' if 'Type="video"' in line else 'audio'
            if line.startswith('<c n="0" d='):
                first = min(int(p.name.split('=')[1].rstrip(')')) for p in ism.glob(f'*/Fragments({kind}=*)'))
                line = line.replace('<c n="0"', f'<c n="0" t="{first}"')
            lines.append(line)
        return ism, '\n'.join(lines).encode()

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def make_session(self, keys=None, encrypt=False, manifest=None, ism=None, text=None):
        ism = ism or self.ism

        async def fetch(ch, relative, **kwargs):
            if text and 'Fragments(text=' in relative:
                return text(int(relative.split('=')[1].split(')')[0]))
            data = (ism / relative.split('?')[0]).read_bytes()
            return encrypt_fragment(data, KEY) if encrypt and 'Fragments' in relative else data
        origin = SimpleNamespace(work_sem=asyncio.Semaphore(1), fetch_rel=fetch)
        hls = tempfile.mkdtemp(dir=self.tmp.name)
        session = proxy.ChannelSession(
            SimpleNamespace(slug='smooth', keys=keys or {}, url='http://origin/Stream.ism/Manifest', is_smooth=True),
            {'hls_dir': hls, 'ffmpeg': FFMPEG}, origin)
        session.video, session.audios = proxy.select_smooth_tracks(manifest or self.manifest, 1080, texts=session.texts)
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

    async def test_still_image_segments_are_kept_not_skipped(self):
        session = self.make_session(keys={KID: KEY}, encrypt=True, manifest=self.still_manifest, ism=self.still_ism)
        frags = list(self.still_ism.glob('QualityLevels(*)/Fragments(video=*)'))
        self.assertLess(max(p.stat().st_size for p in frags), 20000)  # below the old floor
        self.check_continuous(await self.probe(session), frames=100)

    async def test_ttml_stream_becomes_dvb_subtitles_in_the_ts(self):
        video_chunks = [line for line in self.manifest.decode().split('<StreamIndex')[1].splitlines() if line.startswith('<c ')]
        text_index = ('<StreamIndex Type="text" Subtype="SUBT" Name="text" Language="spa" '
                      'Url="QualityLevels({bitrate})/Fragments(text={start time})">'
                      '<QualityLevel Index="0" Bitrate="1000" FourCC="TTML"/>' + ''.join(video_chunks) + '</StreamIndex>')
        manifest = self.manifest.replace(b'</SmoothStreamingMedia>', text_index.encode() + b'</SmoothStreamingMedia>')

        def text(t):  # one cue 0.5-1.5 s into each chunk, on the media timeline
            start = t / 10_000_000
            doc = subtitles_test.ttml((f'{start + .5:.3f}s', f'{start + 1.5:.3f}s', f'Cue {t}'))
            return subtitles_test.fragment([(20_000_000, doc)])
        session = self.make_session(manifest=manifest, text=text)
        self.assertEqual([(s.kind, s.lang, s.codecs) for s in session.texts], [('text', 'spa', 'ttml')])
        output = b''.join([(await session.remux(t)).read_bytes() for t, _d in session.video.entries[:2]])
        result = await asyncio.to_thread(
            subprocess.run, [FFPROBE, '-v', 'error', '-show_entries', 'stream=codec_type,codec_name:stream_tags=language'
                             ':subtitle=pts,num_rects', '-of', 'json', 'pipe:0'],
            input=output, capture_output=True, timeout=30)
        data = json.loads(result.stdout)
        self.assertEqual([(s['codec_type'], s.get('tags', {}).get('language')) for s in data['streams']],
                         [('video', None), ('audio', 'und'), ('subtitle', 'spa')])
        shown = [(round(int(f['pts']) / 1e6, 2), f['num_rects']) for f in data['frames'] if 'num_rects' in f]
        # The TS clock starts at 1 s: clear, cue, clear for each 2 s segment.
        self.assertEqual(shown, [(1.0, 0), (1.5, 1), (2.5, 0), (3.0, 0), (3.5, 1), (4.5, 0)])

    async def test_eac3_audio_remuxes_without_a_dec3_box(self):
        work = Path(self.tmp.name)
        subprocess.run([FFMPEG, '-v', 'error', '-y', '-f', 'lavfi', '-i', 'sine=r=48000,aformat=channel_layouts=5.1',
                        '-t', '3', '-c:a', 'eac3', str(work / 'raw.eac3')], check=True)
        # The MP4 muxer needs parsed E-AC-3 packets before it writes a fragment,
        # and the first one goes into the moov: use the second.
        subprocess.run([FFMPEG, '-v', 'error', '-y', '-i', str(work / 'raw.eac3'), '-c', 'copy', '-f', 'mp4',
                        '-movflags', 'frag_keyframe+default_base_moof', '-frag_duration', '1000000',
                        str(work / 'ec3.mp4')], check=True)
        data = (work / 'ec3.mp4').read_bytes()
        moof = next((p, s) for k, p, s in smooth.boxes(data) if k == b'moof')
        mdat = next((p, s) for k, p, s in smooth.boxes(data) if k == b'mdat' and p > moof[0])
        stream = smooth.Stream('audio', 'dd', 'u', 48000, 'eng', [smooth.QualityLevel(384000, 'EC-3', channels=6)])
        self.assertTrue(stream.supported())
        self.assertEqual(smooth.audio_codec_string(stream.levels[0]), 'ec-3')
        frag = data[moof[0]:moof[0] + moof[1]] + data[mdat[0]:mdat[0] + mdat[1]]
        (work / 'smooth-ec3.mp4').write_bytes(smooth.build_init(stream, stream.levels[0]) + smooth.prepare_fragment(frag, 0, None))
        probe = subprocess.run([FFPROBE, '-v', 'error', '-show_entries', 'stream=codec_name,channels', '-of', 'csv=p=0',
                                str(work / 'smooth-ec3.mp4')], capture_output=True, text=True, check=True)
        self.assertEqual(probe.stdout.strip(), 'eac3,6')

    async def test_encrypted_segment_without_key_fails_cleanly(self):
        session = self.make_session(encrypt=True)
        with self.assertRaises(proxy.web.HTTPBadGateway) as ctx:
            await session.remux(session.video.entries[0][0])
        self.assertIn('no key', ctx.exception.text)

    async def test_refresh_polls_twice_per_chunk_and_skips_unchanged_manifest(self):
        session = self.make_session()
        del session.refresh  # use the real method
        session.video = None
        session.origin.fetch_mpd = AsyncMock(return_value=self.manifest)
        with patch.object(proxy, 'select_smooth_tracks', wraps=proxy.select_smooth_tracks) as select:
            await session.refresh()
            self.assertAlmostEqual(session._refresh_interval(), 1.0, delta=0.1)
            await session.refresh()  # inside the interval: no request
            self.assertEqual(session.origin.fetch_mpd.await_count, 1)
            session.mpd_at = 0
            await session.refresh()  # same body: fetched but not parsed again
            self.assertEqual(session.origin.fetch_mpd.await_count, 2)
            self.assertEqual(select.call_count, 1)


if __name__ == '__main__':
    unittest.main()
