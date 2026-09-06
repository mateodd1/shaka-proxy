"""Exercise actual Shaka/FFmpeg using locally generated, unencrypted media."""
import asyncio
import json
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock
from xml.etree import ElementTree as ET

import proxy

ROOT = Path(__file__).resolve().parents[1]
FFMPEG = shutil.which('ffmpeg') or '/usr/lib/jellyfin-ffmpeg/ffmpeg'
FFPROBE = shutil.which('ffprobe') or '/usr/lib/jellyfin-ffmpeg/ffprobe'
PACKAGER = ROOT / 'bin/packager'


@unittest.skipUnless(Path(FFMPEG).exists() and Path(FFPROBE).exists() and PACKAGER.exists(), 'media binaries unavailable')
class MuxTimestampTests(unittest.IsolatedAsyncioTestCase):
    async def test_segments_preserve_common_epoch_clock_and_audio_boundaries(self):
        await self.check_audio_tracks(['spa'])

    async def test_multiple_audios_survive_remux_in_spanish_first_order(self):
        await self.check_audio_tracks(['eng', 'es-ES', 'cat'])

    async def check_audio_tracks(self, languages):
        with tempfile.TemporaryDirectory(prefix='shaka-media-test-') as directory:
            root = Path(directory)
            command = [FFMPEG, '-hide_banner', '-v', 'error',
                       '-f', 'lavfi', '-i', 'testsrc2=size=320x180:rate=25',
                       '-f', 'lavfi', '-i', 'sine=frequency=440:sample_rate=48000',
                       '-map', '0:v:0']
            for i, lang in enumerate(languages):
                command += ['-map', '1:a:0', f'-metadata:s:a:{i}', f'language={lang}']
            command += ['-t', '6', '-c:v', 'libx264', '-preset', 'ultrafast',
                       '-b:v', '1500k', '-g', '50', '-bf', '2', '-c:a', 'aac',
                       '-output_ts_offset', '1700000000', '-f', 'dash', '-seg_duration', '2',
                       '-use_timeline', '1', '-use_template', '1',
                       '-init_seg_name', 'init-$RepresentationID$.mp4',
                       '-media_seg_name', 'seg-$RepresentationID$-$Time$.m4s', str(root / 'test.mpd')]
            result = await asyncio.to_thread(subprocess.run, command, capture_output=True, timeout=20)
            self.assertEqual(result.returncode, 0, result.stderr.decode())
            manifest = ET.parse(root / 'test.mpd').getroot()
            # Fixture adaptation only: production MPDs place SegmentTemplate
            # on AdaptationSet, whereas FFmpeg places it on Representation.
            for adaptation in manifest.iter(proxy.qtag('AdaptationSet')):
                representation = adaptation.find(proxy.qtag('Representation'))
                template = representation.find(proxy.qtag('SegmentTemplate'))
                representation.remove(template)
                adaptation.append(template)
            video, audio = proxy.select_tracks(ET.tostring(manifest), 1080)
            self.assertEqual(len(audio), len(languages))
            async def fetch(ch, relative, **kwargs):
                return (root / relative).read_bytes()
            origin = SimpleNamespace(work_sem=asyncio.Semaphore(1), fetch_rel=fetch)
            session = proxy.ChannelSession(
                SimpleNamespace(slug='fixture', keys={}),
                {'hls_dir': str(root/'hls'), 'ffmpeg': FFMPEG, 'packager': str(PACKAGER)}, origin)
            session.video, session.audios = video, audio
            session.refresh = AsyncMock()
            session._prune_cache = Mock()
            continuity = proxy.TSContinuity()
            output = []
            for timestamp, _ in video.entries[:2]:
                path = await session.remux(timestamp)
                output.append(continuity.rewrite(path.read_bytes()))
            probe = await asyncio.to_thread(subprocess.run,
                [FFPROBE, '-v', 'warning', '-show_packets', '-show_entries',
                 'packet=stream_index,dts_time,duration_time:stream=index,codec_type:stream_tags=language',
                 '-of', 'json', 'pipe:0'],
                input=b''.join(output), capture_output=True, timeout=20)
            self.assertEqual(probe.returncode, 0, probe.stderr.decode())
            self.assertFalse(probe.stderr, probe.stderr.decode())
            expected = {}
            counts = {}
            for packet in json.loads(probe.stdout)['packets']:
                index, timestamp = packet['stream_index'], float(packet['dts_time'])
                self.assertGreaterEqual(timestamp, 0)
                self.assertLess(timestamp, 6)
                if index in expected:
                    self.assertAlmostEqual(timestamp, expected[index], delta=.00005)
                expected[index] = timestamp + float(packet['duration_time'])
                counts[index] = counts.get(index, 0) + 1
            streams = json.loads(probe.stdout)['streams']
            audio_streams = [s for s in streams if s['codec_type'] == 'audio']
            expected_languages = sorted(
                [proxy.iso639(lang) for lang in languages], key=lambda lang: lang != 'spa')
            self.assertEqual([s['tags']['language'] for s in audio_streams], expected_languages)
            self.assertEqual(set(counts), set(range(len(languages) + 1)))
            self.assertEqual(counts[0], 100)
            for index in range(1, len(languages) + 1):
                self.assertGreater(counts[index], 100)
            self.assertFalse(list(session.hls_dir.glob('.tmp-*')))


if __name__ == '__main__':
    unittest.main()
