"""Pooled downloads under load, route preservation and channel failure isolation."""
import asyncio
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from aiohttp import web

import proxy


class PooledOriginTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.connections = set()
        self.paths = []

        async def handle(request):
            self.connections.add(request.transport)
            self.paths.append(request.raw_path)
            if request.path == '/redirect':
                return web.Response(status=302, headers={'Location': '/cdn/Manifest?token=a%2Fb'})
            await asyncio.sleep(.005)
            return web.Response(body=bytes(256))

        app = web.Application()
        app.router.add_get('/{path:.*}', handle)
        self.runner = web.AppRunner(app)
        await self.runner.setup()
        site = web.TCPSite(self.runner, '127.0.0.1', 0)
        await site.start()
        self.base = f'http://127.0.0.1:{site._server.sockets[0].getsockname()[1]}'
        self.origin = proxy.DashOrigin(
            dict(default_ua='fixture', origin='', referer='', cdn_retries=1,
                 origin_transport='aiohttp', origin_connections=8),
            SimpleNamespace(get=lambda: ''))

    async def asyncTearDown(self):
        await self.origin.close()
        await self.runner.cleanup()

    async def test_200_channels_share_bounded_persistent_connections(self):
        async def channel(index):
            ch = proxy.Channel(str(index), str(index), '', self.base + '/Manifest')
            for _ in range(2):
                self.assertEqual(await self.origin.fetch_mpd(ch), bytes(256))
            self.assertEqual(await self.origin.fetch_rel(ch, 'video.m4s'), bytes(256))

        with patch.object(proxy, 'run_cmd_out', side_effect=AssertionError('unexpected subprocess')):
            await asyncio.gather(*(channel(i) for i in range(200)))
        self.assertEqual(len(self.paths), 600)
        self.assertLessEqual(len(self.connections), 8)

    async def test_redirect_base_and_signed_paths_are_preserved(self):
        ch = proxy.Channel('c', 'C', '', self.base + '/redirect')
        await self.origin.fetch_mpd(ch)
        await self.origin.fetch_rel(ch, 'Fragments(video=1)?token=a%2Fb&x=%2f')
        self.assertEqual(self.paths[-1], '/cdn/Fragments(video=1)?token=a%2Fb&x=%2f')
        self.assertEqual(self.origin.cdn_base['c'], self.base + '/cdn/Manifest?token=a%2Fb')

    async def test_one_channel_failure_keeps_other_channels_pool_alive(self):
        await self.origin._http_hop(self.base + '/Manifest', {})
        session = self.origin.session
        with patch.object(self.origin, 'recycle', new=AsyncMock()) as recycle:
            await self.origin._note_fail()
            await self.origin._note_fail()
            recycle.assert_not_awaited()
        self.assertIs(self.origin.session, session)
        self.assertFalse(session.closed)

    async def test_bound_dns_is_cached_and_wireguard_precedes_proxy(self):
        self.origin.cfg.update(egress_bind='127.0.0.1', egress_dns='192.0.2.53',
                               proxy_url='socks5://unreachable.invalid:1080')
        url = self.base.replace('127.0.0.1', 'origin.invalid') + '/Manifest'
        with patch.object(proxy, 'dns_query_a', return_value='127.0.0.1') as dns:
            await self.origin._http_hop(url, {})
            await self.origin._http_hop(url, {})
        dns.assert_called_once_with('origin.invalid', '192.0.2.53', '127.0.0.1')
