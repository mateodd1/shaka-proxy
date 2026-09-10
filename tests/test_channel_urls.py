"""Opaque URLs must resolve to the same shared channel as legacy URLs."""
import asyncio
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock
from urllib.parse import urlsplit

from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

import proxy


PLAYLIST = '''#EXTM3U url-tvg="https://example.test/epg.xml"
#EXTINF:-1 tvg-id="one",La 1
https://example.test/one/manifest.mpd|x-tcdn-token=TOKEN
#EXTINF:-1 tvg-id="two",La 1
https://example.test/two/manifest.mpd|x-tcdn-token=TOKEN
#EXTINF:-1,Directo
https://example.test/direct.ts
'''


class ChannelURLTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory(prefix="shaka-channel-urls-")
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.source = self.root / "channels.m3u"
        self.source.write_text(PLAYLIST.replace("TOKEN", "first-token"))
        self.cfg = {
            "source_m3u": str(self.source),
            "token_file": str(self.root / "token.json"),
            "hls_dir": str(self.root / "hls"),
            "public_base": "https://proxy.example.test/prefix/",
            "default_ua": "test",
            "referer": "https://example.test/",
        }
        self.playlist = proxy.Playlist(str(self.source))
        self.playlist.maybe_reload("test", self.cfg["referer"])

    def test_export_preserves_metadata_and_uses_distinct_opaque_ids(self):
        output = proxy.render_playlist(self.playlist, self.cfg)
        urls = [line for line in output.splitlines() if line.startswith("https://")]
        ids = [urlsplit(url).path.split("/")[-2] for url in urls[:2]]
        for channel, public_id, url in zip(self.playlist.channels, ids, urls):
            self.assertRegex(public_id, r"^[0-9a-f]{24}$")
            self.assertIs(self.playlist.by_public_id[public_id], channel)
            self.assertNotIn(channel.slug, url)
        self.assertNotEqual(ids[0], ids[1])
        self.assertIn('#EXTINF:-1 tvg-id="one",La 1', output)
        self.assertIn('url-tvg="https://example.test/epg.xml"', output)
        self.assertEqual(urls[-1], "https://example.test/direct.ts")
        self.assertNotIn("first-token", output)

    def test_ids_survive_token_refresh_playlist_reload_and_new_instance(self):
        original = proxy.render_playlist(self.playlist, self.cfg)
        self.source.write_text(PLAYLIST.replace("TOKEN", "second-token"))
        self.playlist.mtime = 0
        self.playlist.maybe_reload("test", self.cfg["referer"])
        self.assertEqual(proxy.render_playlist(self.playlist, self.cfg), original)
        restarted = proxy.Playlist(str(self.source))
        restarted.maybe_reload("test", self.cfg["referer"])
        self.assertEqual(proxy.render_playlist(restarted, self.cfg), original)
        other_cfg = dict(self.cfg, public_base="https://proxy.example.test/other")
        self.assertEqual(
            set(self.playlist.by_public_id), set(restarted.by_public_id)
        )
        self.assertEqual(
            proxy.render_playlist(restarted, other_cfg),
            original.replace("/prefix/", "/other/"),
        )

    def test_url_without_public_base_keeps_relative_form(self):
        url = proxy.channel_url({}, "la-1")
        self.assertRegex(url, r"^live/[0-9a-f]{24}/stream\.ts$")

    async def test_concurrent_opaque_and_legacy_requests_share_one_session(self):
        state = proxy.AppState(self.cfg)
        public_id = urlsplit(proxy.channel_url(self.cfg, "la-1")).path.split("/")[-2]
        sessions = await asyncio.gather(
            state.get_session(public_id), state.get_session("la-1"),
            state.get_session(public_id),
        )
        self.assertTrue(all(session is sessions[0] for session in sessions))
        self.assertEqual(list(state.sessions), ["la-1"])
        self.assertEqual(sessions[0].hls_dir, self.root / "hls" / "la-1")

    async def test_unknown_and_passthrough_ids_do_not_create_sessions(self):
        state = proxy.AppState(self.cfg)
        for value in ("missing", "f" * 24, proxy.public_channel_id("directo")):
            with self.assertRaises(web.HTTPNotFound):
                await state.get_session(value)
        self.assertFalse(state.sessions)

    async def test_hls_index_and_segments_work_via_both_routes_and_publish_only_ids(self):
        # Exercise the HTTP handlers with completed media, without contacting a CDN.
        state = proxy.AppState(self.cfg)
        session = await state.get_session("la-1")
        session.video = SimpleNamespace(timescale=1000)
        session.refresh = AsyncMock()
        session.start_producer = Mock()
        payload = bytes([0x47, 0, 0, 0x10]) + bytes(184)
        payload *= 220
        for timestamp in (1000, 2000, 3000):
            session._published[timestamp] = 1000
            (session.hls_dir / f"seg_{timestamp}.ts").write_bytes(payload)
        app = web.Application()
        app["state"] = state
        app.router.add_get("/live/{slug}/index.m3u8", proxy.handle_live_index)
        app.router.add_get("/live/{slug}/{name}", proxy.handle_live_seg)
        public_id = proxy.public_channel_id("la-1")
        async with TestClient(TestServer(app)) as client:
            for route in (public_id, "la-1"):
                response = await client.get(f"/live/{route}/index.m3u8")
                self.assertEqual(response.status, 200)
                body = await response.text()
                self.assertNotIn("la-1", body)
                urls = [line for line in body.splitlines() if not line.startswith("#")]
                self.assertEqual(len(urls), 3)
                for url in urls:
                    self.assertIn(f"/live/{public_id}/seg_", url)
                    path = urlsplit(url).path.removeprefix("/prefix")
                    segment = await client.get(path)
                    self.assertEqual(segment.status, 200)
                    self.assertEqual(await segment.read(), payload)
            missing = await client.get("/live/missing/index.m3u8")
            self.assertEqual(missing.status, 404)
        self.assertEqual(list(state.sessions), ["la-1"])


if __name__ == "__main__":
    unittest.main()
