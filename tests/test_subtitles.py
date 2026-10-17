"""Subtitles: TTML/WebVTT parsing, fragment cues and DVB encoding for MPEG-TS."""
import json
import shutil
import struct
import subprocess
import tempfile
import unittest
from pathlib import Path

import proxy
import smooth_streaming as smooth
import subtitles

FFPROBE = shutil.which('ffprobe') or '/usr/lib/jellyfin-ffmpeg/ffprobe'
HAVE_PROBE = Path(FFPROBE).exists()

TTML = b"""<?xml version="1.0" encoding="utf-8"?>
<tt xmlns="http://www.w3.org/ns/ttml" xmlns:ttp="http://www.w3.org/ns/ttml#parameter" ttp:tickRate="10000000" ttp:frameRate="25">
<body><div begin="100s">
<p begin="00:00:01.500" end="00:00:03.000">Hola <span>mundo</span><br/>  segunda   linea </p>
<p begin="40000000t" dur="10f">Ticks</p>
<p begin="5s" end="6s">   </p>
</div></body></tt>"""


def fragment(samples, tfdt=None):
    """moof+mdat with one trun of (duration, payload) samples."""
    trun_body = struct.pack('>I', len(samples)) + struct.pack('>i', 0) + b''.join(
        struct.pack('>II', dur, len(data)) for dur, data in samples)
    traf = [smooth.fbox(b'tfhd', 0x20000, struct.pack('>I', 1))]
    if tfdt is not None:
        traf.append(smooth.fbox(b'tfdt', 1 << 24, struct.pack('>Q', tfdt)))
    traf.append(smooth.fbox(b'trun', 0x301, trun_body))
    moof = smooth.box(b'moof', smooth.fbox(b'mfhd', 0, struct.pack('>I', 1)), smooth.box(b'traf', *traf))
    moof = bytearray(moof)
    trun_at = moof.index(b'trun') - 4
    struct.pack_into('>i', moof, trun_at + 16, len(moof) + 8)
    return bytes(moof) + smooth.box(b'mdat', *[data for _d, data in samples])


def ttml(*cues):
    body = ''.join(f'<p begin="{b}" end="{e}">{text}</p>' for b, e, text in cues)
    return f'<tt xmlns="http://www.w3.org/ns/ttml"><body><div>{body}</div></body></tt>'.encode()


class ParseTests(unittest.TestCase):
    def test_ttml_times_nesting_and_text(self):
        self.assertEqual(subtitles.parse_ttml(TTML), [
            (101.5, 103.0, 'Hola mundo\nsegunda linea'),
            (104.0, 104.4, 'Ticks'),
        ])

    def test_webvtt(self):
        text = 'WEBVTT\n\n1\n00:01.000 --> 00:02.500 align:middle\n<i>Hola</i> &amp; adios\n\n01:00:00.000 --> 01:00:01.000\nB\n'
        self.assertEqual(subtitles.parse_webvtt(text), [(1.0, 2.5, 'Hola & adios'), (3600.0, 3601.0, 'B')])

    def test_stpp_fragment_with_media_time(self):
        # Two 2 s samples at 1000 s; the TTML uses the media timeline.
        data = fragment([(2000, ttml(('1000.5s', '1001s', 'A'))), (2000, ttml(('1003s', '1009s', 'B')))], tfdt=1_000_000)
        cues = subtitles.segment_cues(data, 1000, 1_000_000, 4000)
        self.assertEqual([(c.start, c.end, c.text) for c in cues], [(1000.5, 1001.0, 'A'), (1003.0, 1004.0, 'B')])

    def test_smooth_fragment_with_sample_relative_time_and_no_tfdt(self):
        data = fragment([(20_000_000, ttml(('00:00:00.500', '00:00:01.000', 'Rel')))])
        cues = subtitles.segment_cues(data, 10_000_000, 17_600_000_000_000_000, 20_000_000)
        self.assertEqual([(c.start, c.end, c.text) for c in cues], [(1_760_000_000.5, 1_760_000_001.0, 'Rel')])

    def test_wvtt_fragment(self):
        vttc = smooth.box(b'vttc', smooth.box(b'payl', 'Línea <b>uno</b>'.encode()))
        data = fragment([(500, vttc), (1500, smooth.box(b'vtte'))], tfdt=3000)
        cues = subtitles.segment_cues(data, 1000, 3000, 2000)
        self.assertEqual([(c.start, c.end, c.text) for c in cues], [(3.0, 3.5, 'Línea uno')])

    def test_raw_ttml_segment_and_garbage(self):
        cues = subtitles.segment_cues(TTML, 1, 100, 10)
        self.assertEqual([c.text for c in cues], ['Hola mundo\nsegunda linea', 'Ticks'])
        self.assertEqual(subtitles.segment_cues(b'not xml', 1, 0, 2), [])

    def test_pieces_of_one_cue_are_merged(self):
        cues = [subtitles.Cue(2, 4, 'A'), subtitles.Cue(0, 2, 'A'), subtitles.Cue(5, 6, 'A')]
        self.assertEqual([(c.start, c.end) for c in subtitles.merge(cues)], [(0, 4), (5, 6)])


class TimelineTests(unittest.TestCase):
    def test_segment_starts_with_current_state_and_clears_at_cue_end(self):
        cues = [subtitles.Cue(9, 11, 'A'), subtitles.Cue(12, 13, 'B'), subtitles.Cue(12.5, 20, 'C')]
        got = [(at, text) for at, text, _until in subtitles.timeline(cues, 10, 14)]
        self.assertEqual(got, [(10, 'A'), (11, None), (12, 'B'), (12.5, 'B\nC'), (13, 'C')])

    def test_empty_segment_still_carries_a_clear(self):
        self.assertEqual(subtitles.timeline([], 10, 12), [(10, None, 11.0)])


class DvbTests(unittest.TestCase):
    def test_four_bit_runs_round_trip(self):
        row = bytes([0] * 300 + [1, 1, 1] + [2] * 8 + [0, 0] + [1] * 30 + [0] * 5 + [2])
        nibbles = subtitles._nibbles_4bit(row)
        self.assertTrue(all(0 <= n < 16 for n in nibbles))
        self.assertEqual(decode_4bit(nibbles), row)

    @unittest.skipUnless(HAVE_PROBE, 'ffprobe unavailable')
    def test_ffmpeg_decodes_the_display_sets(self):
        cues = [subtitles.Cue(100.5, 101.5, 'Hola, ¿qué tal?'), subtitles.Cue(101.5, 104, 'Dos\nlíneas')]
        data = subtitles.segment_ts(cues, 100, 102, 'spa')
        with tempfile.NamedTemporaryFile(suffix='.ts') as fh:
            fh.write(data)
            fh.flush()
            stream = subprocess.run([FFPROBE, '-v', 'error', '-show_entries', 'stream=codec_name:stream_tags=language',
                                     '-of', 'json', fh.name], capture_output=True, text=True, check=True)
            frames = subprocess.run([FFPROBE, '-v', 'error', '-show_entries', 'subtitle=pts,num_rects',
                                     '-of', 'csv=p=0', fh.name], capture_output=True, text=True, check=True)
        self.assertEqual([(s['codec_name'], s['tags']['language']) for s in json.loads(stream.stdout)['streams']],
                         [('dvb_subtitle', 'spa')])
        # 10 s base: clear at the segment start, then each cue (the second runs on).
        self.assertEqual(frames.stdout.split(), ['10000000,0', '10500000,1', '11500000,1'])


def decode_4bit(nibbles):
    out, i = bytearray(), 0
    while True:
        n = nibbles[i]
        i += 1
        if n:
            out.append(n)
            continue
        a = nibbles[i]
        i += 1
        if a == 0:
            return bytes(out)
        if a < 8:
            out += bytes(a + 2)
        elif a < 12:
            out += bytes([nibbles[i]]) * ((a & 3) + 4)
            i += 1
        elif a == 12:
            out += bytes(1)
        elif a == 13:
            out += bytes(2)
        elif a == 14:
            out += bytes([nibbles[i + 1]]) * (nibbles[i] + 9)
            i += 2
        else:
            out += bytes([nibbles[i + 2]]) * ((nibbles[i] << 4 | nibbles[i + 1]) + 25)
            i += 3


MPD = b"""<?xml version="1.0"?>
<MPD xmlns="urn:mpeg:dash:schema:mpd:2011" type="dynamic">
<Period>
<AdaptationSet contentType="video" mimeType="video/mp4"><SegmentTemplate timescale="1000" media="v-$Time$.m4s" initialization="v-init.mp4"><SegmentTimeline><S t="0" d="2000" r="2"/></SegmentTimeline></SegmentTemplate>
<Representation id="v" height="720" bandwidth="1" codecs="avc1.64001f"/></AdaptationSet>
<AdaptationSet contentType="audio" lang="en"><SegmentTemplate timescale="48000" media="a-$Time$.m4s" initialization="a-init.mp4"><SegmentTimeline><S t="0" d="96000" r="2"/></SegmentTimeline></SegmentTemplate>
<Representation id="a-en" bandwidth="1" codecs="ec-3"/></AdaptationSet>
<AdaptationSet mimeType="application/mp4" lang="en" codecs="stpp"><Representation id="t-en" bandwidth="1">
<SegmentTemplate timescale="1000" media="t-$Time$.m4s" initialization="t-init.mp4"><SegmentTimeline><S t="0" d="2000" r="2"/></SegmentTimeline></SegmentTemplate></Representation></AdaptationSet>
<AdaptationSet contentType="text" mimeType="application/ttml+xml" lang="es"><SegmentTemplate timescale="1000" media="s-$Time$.xml"><SegmentTimeline><S t="0" d="2000" r="2"/></SegmentTimeline></SegmentTemplate>
<Representation id="t-es" bandwidth="1"/></AdaptationSet>
</Period></MPD>"""


class SelectionTests(unittest.TestCase):
    def test_dash_text_sets_are_selected_spanish_first(self):
        texts = []
        video, audios = proxy.select_tracks(MPD, 0, None, texts)
        self.assertEqual([a.codecs for a in audios], ['ec-3'])
        self.assertEqual([(t.rep_id, t.lang, t.init_rel) for t in texts], [('t-es', 'es', ''), ('t-en', 'en', 't-init.mp4')])
        self.assertEqual(texts[1].entries, [(0, 2000), (2000, 2000), (4000, 2000)])
        self.assertEqual(proxy.select_tracks(MPD, 0), (video, audios))  # unchanged without a list

    def test_init_timescale(self):
        mdhd = smooth.fbox(b'mdhd', 0, struct.pack('>IIIIHH', 0, 0, 90000, 0, 0, 0))
        init = smooth.box(b'moov', smooth.box(b'trak', smooth.box(b'mdia', mdhd)))
        self.assertEqual(subtitles.init_timescale(init), 90000)
        self.assertIsNone(subtitles.init_timescale(b'junk'))


if __name__ == '__main__':
    unittest.main()
