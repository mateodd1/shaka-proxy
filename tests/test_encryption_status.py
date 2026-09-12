"""Encryption shown in status must come from the selected DASH tracks."""
import unittest
from types import SimpleNamespace
from xml.etree import ElementTree as ET

import proxy


def manifest(scheme='', representation_level=False):
    root = ET.Element(proxy.qtag('MPD'))
    period = ET.SubElement(root, proxy.qtag('Period'))
    adaptation = ET.SubElement(period, proxy.qtag('AdaptationSet'),
                               contentType='video', mimeType='video/mp4')
    template = ET.SubElement(adaptation, proxy.qtag('SegmentTemplate'),
                             initialization='init-$RepresentationID$.mp4',
                             media='seg-$RepresentationID$-$Time$.m4s')
    timeline = ET.SubElement(template, proxy.qtag('SegmentTimeline'))
    ET.SubElement(timeline, proxy.qtag('S'), t='0', d='2', r='3')
    representation = ET.SubElement(adaptation, proxy.qtag('Representation'),
                                   id='video', height='1080', bandwidth='5000000')
    if scheme:
        parent = representation if representation_level else adaptation
        ET.SubElement(parent, proxy.qtag('ContentProtection'), {
            'schemeIdUri': 'urn:mpeg:dash:mp4protection:2011',
            'value': scheme,
            f'{{{proxy.CENC_NS}}}default_KID': '1' * 32,
        })
    return ET.tostring(root)


class EncryptionStatusTests(unittest.TestCase):
    def test_cenc_scheme_is_read_from_selected_adaptation(self):
        video, _ = proxy.select_tracks(manifest('cenc'), 1080, {'1' * 32: '2' * 32})
        self.assertEqual(video.protection_scheme, 'cenc')
        session = SimpleNamespace(video=video, audios=[], ch=SimpleNamespace(keys={'1': '2'}))
        self.assertEqual(proxy.session_encryption(session), 'CENC (AES-CTR) · ClearKey')

    def test_representation_level_cbcs_is_detected(self):
        video, _ = proxy.select_tracks(manifest('cbcs', representation_level=True), 1080)
        self.assertEqual(video.protection_scheme, 'cbcs')
        session = SimpleNamespace(video=video, audios=[], ch=SimpleNamespace(keys={}))
        self.assertEqual(proxy.session_encryption(session), 'CBCS (AES-CBC)')

    def test_unencrypted_or_not_yet_inspected_session_has_no_label(self):
        clear_video, _ = proxy.select_tracks(manifest(), 1080)
        for video in (clear_video, None):
            with self.subTest(video=video):
                session = SimpleNamespace(video=video, audios=[],
                                          ch=SimpleNamespace(keys={'configured': 'key'}))
                self.assertIsNone(proxy.session_encryption(session))

    def test_unknown_mp4_scheme_is_reported_without_guessing_cipher(self):
        video, _ = proxy.select_tracks(manifest('future-scheme'), 1080)
        session = SimpleNamespace(video=video, audios=[], ch=SimpleNamespace(keys={}))
        self.assertEqual(proxy.session_encryption(session), 'FUTURE-SCHEME')


if __name__ == '__main__':
    unittest.main()
