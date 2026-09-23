"""DASH BaseURL resolution for initialization and media segments."""
import unittest
from xml.etree import ElementTree as ET

import proxy


class DashBaseURLTests(unittest.TestCase):
    def test_root_base_url_is_applied_to_segment_templates(self):
        root = ET.Element(proxy.qtag("MPD"))
        ET.SubElement(root, proxy.qtag("BaseURL")).text = "cpix/"
        period = ET.SubElement(root, proxy.qtag("Period"))
        adaptation = ET.SubElement(period, proxy.qtag("AdaptationSet"), contentType="video")
        template = ET.SubElement(
            adaptation,
            proxy.qtag("SegmentTemplate"),
            initialization="init-$RepresentationID$.mp4",
            media="seg-$RepresentationID$-$Time$.m4s",
            timescale="25",
        )
        timeline = ET.SubElement(template, proxy.qtag("SegmentTimeline"))
        ET.SubElement(timeline, proxy.qtag("S"), t="100", d="50", r="2")
        ET.SubElement(
            adaptation,
            proxy.qtag("Representation"),
            id="video-1",
            height="1080",
            bandwidth="6000000",
        )

        video, _audios = proxy.select_tracks(ET.tostring(root), 1080)

        self.assertIsNotNone(video)
        self.assertEqual(video.init_rel, "cpix/init-video-1.mp4")
        self.assertEqual(video.media_tmpl, "cpix/seg-$RepresentationID$-$Time$.m4s")

    def test_hierarchical_base_urls_are_resolved_for_each_representation(self):
        root = ET.Element(proxy.qtag("MPD"))
        ET.SubElement(root, proxy.qtag("BaseURL")).text = "https://cdn.example.test/root/"
        period = ET.SubElement(root, proxy.qtag("Period"))
        ET.SubElement(period, proxy.qtag("BaseURL")).text = "live/"
        adaptation = ET.SubElement(period, proxy.qtag("AdaptationSet"), contentType="video")
        ET.SubElement(adaptation, proxy.qtag("BaseURL")).text = "video/"
        template = ET.SubElement(
            adaptation,
            proxy.qtag("SegmentTemplate"),
            initialization="init-$RepresentationID$.mp4",
            media="seg-$Time$.m4s",
            timescale="25",
        )
        timeline = ET.SubElement(template, proxy.qtag("SegmentTimeline"))
        ET.SubElement(timeline, proxy.qtag("S"), t="100", d="50", r="2")
        representation = ET.SubElement(
            adaptation,
            proxy.qtag("Representation"),
            id="video-1",
            height="1080",
            bandwidth="6000000",
        )
        ET.SubElement(representation, proxy.qtag("BaseURL")).text = "1080/"

        video, _audios = proxy.select_tracks(ET.tostring(root), 1080)

        self.assertIsNotNone(video)
        expected = "https://cdn.example.test/root/live/video/1080/"
        self.assertEqual(video.init_rel, expected + "init-video-1.mp4")
        self.assertEqual(video.media_tmpl, expected + "seg-$Time$.m4s")


if __name__ == "__main__":
    unittest.main()
