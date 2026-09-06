"""Ensure configuration reaches curl for MPD, init and media downloads."""
import asyncio
import os
import shutil
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import proxy


class OriginProxyTests(unittest.IsolatedAsyncioTestCase):
    def make_origin(self, **cfg):
        return proxy.DashOrigin(
            dict(default_ua='fixture', origin='', referer='', cdn_retries=1, **cfg),
            SimpleNamespace(get=lambda: ''))

    async def fake_curl(self, cmd, timeout):
        Path(cmd[cmd.index('-D') + 1]).write_bytes(b'HTTP/1.1 200 OK\r\n\r\n')
        Path(cmd[cmd.index('-o') + 1]).write_bytes(bytes(256))
        return 0, b'200', b''

    async def test_proxy_reaches_mpd_init_media_and_init_cache(self):
        for url in ('socks5://proxy.example:1080', 'socks5h://proxy.example:1080',
                    'http://proxy.example:8080'):
            with self.subTest(proxy=url):
                origin = self.make_origin(proxy_url=url)
                ch = proxy.Channel('fixture', 'Fixture', '', 'https://origin.example/live/Manifest')
                run = AsyncMock(side_effect=self.fake_curl)
                with patch.object(proxy, 'run_cmd_out', run):
                    await origin.fetch_mpd(ch)
                    await origin.fetch_rel(ch, 'init.mp4', use_init_cache=True)
                    await origin.fetch_rel(ch, 'segment.m4s')
                    await origin.fetch_rel(ch, 'init.mp4', use_init_cache=True)
                self.assertEqual(run.await_count, 3)
                for call in run.await_args_list:
                    cmd = call.args[0]
                    self.assertIn('--proxy', cmd)
                    self.assertEqual(cmd[cmd.index('--proxy') + 1], url)
                    self.assertEqual(cmd[cmd.index('--noproxy') + 1], '')
                    self.assertNotIn('--interface', cmd)

    async def test_wireguard_keeps_precedence_and_bound_dns(self):
        origin = self.make_origin(proxy_url='socks5://proxy.example:1080',
                                  egress_bind='192.0.2.10', egress_dns='192.0.2.53')
        run = AsyncMock(side_effect=self.fake_curl)
        with patch.object(proxy, 'run_cmd_out', run), patch.object(
                proxy, 'dns_query_a', return_value='192.0.2.20') as dns:
            await origin._curl_hop('https://origin.example/segment.m4s', {})
        cmd = run.await_args.args[0]
        self.assertNotIn('--proxy', cmd)
        self.assertEqual(cmd[cmd.index('--interface') + 1], '192.0.2.10')
        self.assertEqual(cmd[cmd.index('--resolve') + 1], 'origin.example:443:192.0.2.20')
        dns.assert_called_once_with('origin.example', '192.0.2.53', '192.0.2.10')

    async def test_unconfigured_route_stays_unchanged(self):
        origin = self.make_origin(proxy_url='')
        run = AsyncMock(side_effect=self.fake_curl)
        with patch.object(proxy, 'run_cmd_out', run):
            await origin._curl_hop('https://origin.example/segment.m4s', {})
        cmd = run.await_args.args[0]
        self.assertNotIn('--proxy', cmd)
        self.assertNotIn('--interface', cmd)

    async def test_proxy_failure_does_not_fall_back_to_direct_connection(self):
        origin = self.make_origin(proxy_url='socks5://proxy.example:1080')
        run = AsyncMock(return_value=(7, b'000', b'fixture connection refused'))
        with patch.object(proxy, 'run_cmd_out', run):
            with self.assertRaises(OSError):
                await origin._curl_hop('https://origin.example/segment.m4s', {})
        self.assertEqual(run.await_count, 1)
        self.assertIn('--proxy', run.await_args.args[0])

    @unittest.skipUnless(shutil.which('curl'), 'curl unavailable')
    async def test_real_curl_uses_local_http_and_socks_proxies_despite_no_proxy(self):
        for scheme in ('http', 'socks5', 'socks5h'):
            with self.subTest(scheme=scheme):
                requests, destinations, tasks = [], [], []
                host = '127.0.0.1' if scheme == 'socks5' else 'fixture.invalid'

                async def handle(reader, writer):
                    try:
                        async with asyncio.timeout(5):
                            if scheme != 'http':
                                greeting = await reader.readexactly(2)
                                self.assertEqual(greeting[0], 5)
                                methods = await reader.readexactly(greeting[1])
                                self.assertIn(0, methods)
                                writer.write(b'\x05\x00')
                                await writer.drain()
                                command = await reader.readexactly(4)
                                self.assertEqual(command[:3], b'\x05\x01\x00')
                                if command[3] == 3:
                                    length = (await reader.readexactly(1))[0]
                                    address = (await reader.readexactly(length)).decode()
                                else:
                                    self.assertEqual(command[3], 1)
                                    address = '.'.join(str(x) for x in await reader.readexactly(4))
                                destinations.append(address)
                                await reader.readexactly(2)
                                writer.write(b'\x05\x00\x00\x01\x7f\x00\x00\x01\x00\x50')
                                await writer.drain()
                            request = await reader.readuntil(b'\r\n\r\n')
                            target = request.split(b' ', 2)[1].decode()
                            requests.append(target)
                            if '/live/Manifest?' in target:
                                writer.write(b'HTTP/1.1 302 Found\r\n'
                                             b'Location: ../cdn/Manifest?token=fixture\r\n'
                                             b'Content-Length: 0\r\nConnection: close\r\n\r\n')
                            else:
                                writer.write(b'HTTP/1.1 200 OK\r\nContent-Length: 256\r\n'
                                             b'Connection: close\r\n\r\n' + bytes(256))
                            await writer.drain()
                    finally:
                        writer.close()
                        await writer.wait_closed()

                def connected(reader, writer):
                    tasks.append(asyncio.create_task(handle(reader, writer)))

                server = await asyncio.start_server(connected, '127.0.0.1', 0)
                port = server.sockets[0].getsockname()[1]
                origin = self.make_origin(proxy_url=f'{scheme}://127.0.0.1:{port}')
                ch = proxy.Channel('fixture', 'Fixture', '', f'http://{host}/live/Manifest')
                try:
                    # Explicit proxy configuration must not be bypassed by the environment.
                    with patch.dict(os.environ, {'no_proxy': '*', 'NO_PROXY': '*'}):
                        self.assertEqual(await origin.fetch_mpd(ch), bytes(256))
                        await origin.fetch_rel(ch, 'init.mp4', use_init_cache=True)
                        await origin.fetch_rel(ch, '../media/segment.m4s')
                        await origin.fetch_rel(ch, 'init.mp4', use_init_cache=True)
                    prefix = f'http://{host}' if scheme == 'http' else ''
                    self.assertEqual(len(requests), 4)
                    self.assertTrue(requests[0].startswith(prefix + '/live/Manifest?_='))
                    self.assertEqual(requests[1:], [prefix + '/cdn/Manifest?token=fixture',
                                                   prefix + '/cdn/init.mp4',
                                                   prefix + '/media/segment.m4s'])
                    if scheme != 'http':
                        self.assertEqual(destinations, [host] * 4)
                finally:
                    server.close()
                    await server.wait_closed()
                    await origin.close()
                    for task in tasks:
                        if not task.done():
                            task.cancel()
                    results = await asyncio.gather(*tasks, return_exceptions=True)
                    for result in results:
                        if isinstance(result, Exception):
                            raise result


if __name__ == '__main__':
    unittest.main()
