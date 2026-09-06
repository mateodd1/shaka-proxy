"""Multi-audio selection: preserve tracks, prefer Spanish, keep key filtering."""
import unittest
from xml.etree import ElementTree as ET

import proxy


def manifest(languages, encrypted=()):
    root = ET.Element(proxy.qtag('MPD'))
    period = ET.SubElement(root, proxy.qtag('Period'))
    for i, language in enumerate(languages):
        adaptation = ET.SubElement(period, proxy.qtag('AdaptationSet'),
                                   contentType='audio', lang=language)
        if i in encrypted:
            ET.SubElement(adaptation, proxy.qtag('ContentProtection'),
                          {f'{{{proxy.CENC_NS}}}default_KID': f'{i:032x}'})
        template = ET.SubElement(adaptation, proxy.qtag('SegmentTemplate'),
                                 initialization='init-$RepresentationID$.mp4',
                                 media='seg-$RepresentationID$-$Time$.m4s',
                                 timescale='48000')
        timeline = ET.SubElement(template, proxy.qtag('SegmentTimeline'))
        ET.SubElement(timeline, proxy.qtag('S'), t='0', d='96000', r='2')
        for quality, bandwidth in [('low', '64000'), ('high', '128000')]:
            ET.SubElement(adaptation, proxy.qtag('Representation'),
                          id=f'a{i}-{quality}', bandwidth=bandwidth, codecs='mp4a.40.2')
    return ET.tostring(root)


class AudioSelectionTests(unittest.TestCase):
    def test_all_languages_and_distinct_spanish_tracks_are_retained(self):
        _, audios = proxy.select_tracks(manifest(['eng', 'spa', 'cat', 'spa']), 1080)
        self.assertEqual([a.rep_id for a in audios],
                         ['a1-high', 'a3-high', 'a0-high', 'a2-high'])

    def test_spanish_aliases_are_first_and_labelled_spa(self):
        for language in ['es', 'es-ES', 'es-MX', 'SPA', 'spa', 'es_ES']:
            with self.subTest(language=language):
                _, audios = proxy.select_tracks(manifest(['eng', language]), 1080)
                self.assertEqual([a.rep_id for a in audios], ['a1-high', 'a0-high'])
                self.assertEqual(proxy.iso639(audios[0].lang), 'spa')

    def test_no_spanish_preserves_all_available_tracks_in_order(self):
        _, audios = proxy.select_tracks(manifest(['eng', 'cat', '']), 1080)
        self.assertEqual([a.rep_id for a in audios], ['a0-high', 'a1-high', 'a2-high'])

    def test_missing_audio_key_does_not_prevent_other_tracks(self):
        _, audios = proxy.select_tracks(
            manifest(['eng', 'spa', 'cat'], encrypted=(0, 1)),
            1080, keys={f'{1:032x}': 'a' * 32})
        self.assertEqual([a.rep_id for a in audios], ['a1-high', 'a2-high'])

    def test_empty_and_single_track_remain_supported(self):
        for languages in [[], ['spa']]:
            _, audios = proxy.select_tracks(manifest(languages), 1080)
            self.assertEqual(len(audios), len(languages))
