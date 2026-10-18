#!/usr/bin/env python3
"""Isolated load exercise: generated encrypted Smooth media, real remux and TS clients.

Uses loopback only and temporary files; never opens channels on the real CDN.
Run with the project's venv after building its compiled modules.
"""
import argparse
import asyncio
import json
import logging
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from xml.etree import ElementTree as ET

import aiohttp
from aiohttp import web

import proxy

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'tests'))
from test_smooth import KEY, KID, encrypt_fragment


async def listen(app):
    runner = web.AppRunner(app, shutdown_timeout=3, access_log=None)
    await runner.setup()
    site = web.TCPSite(runner, '127.0.0.1', 0)
    await site.start()
    return runner, f'http://127.0.0.1:{site._server.sockets[0].getsockname()[1]}'


def fixture(root, ffmpeg, bitrate):
    ism = root / 'Stream.ism'
    subprocess.run([
        ffmpeg, '-hide_banner', '-v', 'error', '-f', 'lavfi', '-i',
        'testsrc2=size=1920x1080:rate=25', '-f', 'lavfi', '-i',
        'sine=frequency=440:sample_rate=48000', '-t', '6',
        '-c:v', 'libx264', '-threads', '2', '-preset', 'ultrafast',
        '-b:v', str(bitrate), '-minrate', str(bitrate), '-maxrate', str(bitrate),
        '-bufsize', str(bitrate * 2), '-x264-params', 'nal-hrd=cbr',
        '-g', '50', '-keyint_min', '50', '-sc_threshold', '0', '-bf', '2',
        '-pix_fmt', 'yuv420p', '-c:a', 'aac', '-b:a', '128k',
        '-f', 'smoothstreaming', '-min_frag_duration', '2000000',
        '-ism_lookahead', '0', str(ism)], check=True)
    manifest = ET.fromstring((ism / 'Manifest').read_bytes())
    fragments = {}
    for stream in manifest.findall('StreamIndex'):
        kind = stream.get('Type')
        paths = sorted(ism.glob(f'*/Fragments({kind}=*)'),
                       key=lambda p: int(p.name.split('=')[1].rstrip(')')))
        path = paths[1]  # full fragment after the encoder's initial rate burst
        fragments[kind] = encrypt_fragment(path.read_bytes(), KEY)
        for chunk in stream.findall('c'):
            stream.remove(chunk)
    manifest.set('IsLive', 'TRUE')
    manifest.set('Duration', '0')
    return manifest, fragments


async def exercise(args, root):
    ffmpeg = shutil.which('ffmpeg') or '/usr/lib/jellyfin-ffmpeg/ffmpeg'
    manifest, fragments = await asyncio.to_thread(fixture, root, ffmpeg, args.bitrate)
    started = time.monotonic()
    byte_counts = [0] * args.channels
    first_bytes = [None] * args.channels
    last_bytes = [None] * args.channels
    errors = []
    warnings = {'expired': 0, 'gap': 0, 'remux': 0, 'stalled': 0}

    class Counts(logging.Handler):
        def emit(self, record):
            message = record.getMessage()
            for key, pattern in [('expired', 'segments expired'), ('gap', 'stream gap'),
                                 ('remux', 'producer remux'), ('stalled', 'stream stalled')]:
                warnings[key] += int(pattern in message)

    logger = logging.getLogger('proxy-shaka')
    handler = Counts()
    logger.addHandler(handler)
    manifest_edge, manifest_body = None, b''

    async def source(request):
        nonlocal manifest_edge, manifest_body
        if args.origin_delay:
            await asyncio.sleep(args.origin_delay)
        if request.path.endswith('/Manifest'):
            # Match the real CDN's two-hour manifest, not just its live tail.
            edge = 1_000_000_000_000 + int((time.monotonic() - started) / 2) * 20_000_000
            if edge != manifest_edge:
                for stream in manifest.findall('StreamIndex'):
                    for chunk in stream.findall('c'):
                        stream.remove(chunk)
                    ET.SubElement(stream, 'c', t=str(edge - (args.manifest_chunks - 1) * 20_000_000), d='20000000')
                    for _ in range(args.manifest_chunks - 1):
                        ET.SubElement(stream, 'c', d='20000000')
                manifest_body = ET.tostring(manifest)
                manifest_edge = edge
            return web.Response(body=manifest_body, content_type='application/xml')
        kind = 'video' if 'Fragments(video=' in request.path else 'audio'
        return web.Response(body=fragments[kind])

    origin_app = web.Application()
    origin_app.router.add_get('/{path:.*}', source)
    origin_runner, origin_url = await listen(origin_app)
    cfg = dict(source_m3u=str(root / 'missing.m3u'), token_file=str(root / 'missing.json'),
               hls_dir=str(root / 'hls'), log_dir=str(root), ffmpeg=ffmpeg,
               default_ua='shaka-local-load', origin='', referer='', public_base='',
               max_channels=0, origin_concurrency=256, origin_connections=512,
               origin_transport='aiohttp', media_process_concurrency=args.processes,
               cpu_workers=args.cpu_workers,
               mux_backend=args.mux_backend,
               hls_window=6, cdn_retries=2, subtitles=False, max_height=1080)
    state = proxy.AppState(cfg)
    for i in range(args.channels):
        slug = f'load-{i}'
        ch = proxy.Channel(slug, slug, '', f'{origin_url}/{slug}/Stream.ism/Manifest',
                           keys={KID: KEY}, is_smooth=True)
        state.playlist.channels.append(ch)
        state.playlist.by_slug[slug] = ch
    app = web.Application()
    app['state'] = state
    app.router.add_get('/live/{slug}/stream.ts', proxy.handle_stream)
    proxy_runner, proxy_url = await listen(app)
    await state.origin.start()
    cpu_start = time.process_time()
    wall_start = time.monotonic()

    async def client(i, session):
        await asyncio.sleep(args.ramp * i / args.channels)
        try:
            async with session.get(f'{proxy_url}/live/load-{i}/stream.ts') as resp:
                resp.raise_for_status()
                async for chunk in resp.content.iter_chunked(256 * 1024):
                    now = time.monotonic()
                    if first_bytes[i] is None:
                        first_bytes[i] = now
                    last_bytes[i] = now
                    byte_counts[i] += len(chunk)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            errors.append(f'{i}: {type(exc).__name__}: {exc}')

    clients = []
    baseline, measured_at = {}, None
    connector = aiohttp.TCPConnector(limit=args.channels + 8)
    try:
        async with aiohttp.ClientSession(connector=connector, timeout=aiohttp.ClientTimeout(total=None)) as session:
            clients = [asyncio.create_task(client(i, session)) for i in range(args.channels)]
            for elapsed in range(10, args.duration + args.ramp + 1, 10):
                await asyncio.sleep(10)
                now = time.monotonic()
                active = sum(last is not None and now - last < 6 for last in last_bytes)
                if measured_at is None and elapsed >= args.ramp + 10:
                    baseline = {slug: s._next_t for slug, s in state.sessions.items() if s._next_t is not None}
                    measured_at = now
                print(json.dumps(dict(elapsed_s=elapsed, receiving=active, sessions=len(state.sessions),
                                      download_s=round(max((s.download_s for s in state.sessions.values()), default=0), 2),
                                      processing_s=round(max((s.processing_s for s in state.sessions.values()), default=0), 2),
                                      warnings=warnings.copy())), flush=True)
            now = time.monotonic()
            receiving = sum(last is not None and now - last < 6 for last in last_bytes)
            ratios = [(s._next_t - baseline[slug]) / s.video.timescale / (now - measured_at)
                      for slug, s in state.sessions.items()
                      if slug in baseline and s._next_t is not None and now - measured_at > 1]
            min_ratio = min(ratios, default=0)
            report = dict(channels=args.channels, receiving=receiving,
                          mux_backend=args.mux_backend, cpu_workers=args.cpu_workers,
                          media_process_concurrency=args.processes,
                          nominal_video_mbps=args.bitrate / 1e6,
                          fragment_video_mbps=round(len(fragments['video']) * 8 / 2 / 1e6, 2),
                          elapsed_s=round(now - wall_start, 2),
                          delivered_mbytes=round(sum(byte_counts) / 1e6, 1),
                          cpu_seconds=round(time.process_time() - cpu_start, 2),
                          max_segment_age_s=round(max((now - s._last_mux for s in state.sessions.values()), default=0), 2),
                          min_production_ratio=round(min_ratio, 3),
                          warnings=warnings.copy(), errors=errors,
                          passed=receiving == args.channels and not errors and not any(warnings.values())
                                 and len(ratios) == args.channels and min_ratio >= .9)
            print(json.dumps(report, indent=2), flush=True)
            if args.report:
                Path(args.report).write_text(json.dumps(report, indent=2) + '\n')
            return report['passed']
    finally:
        for task in clients:
            task.cancel()
        await asyncio.gather(*clients, return_exceptions=True)
        await asyncio.gather(*(s.stop() for s in state.sessions.values()))
        await proxy_runner.cleanup()
        await state.origin.close()
        await origin_runner.cleanup()
        logger.removeHandler(handler)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--channels', type=int, default=200)
    parser.add_argument('--duration', type=int, default=60)
    parser.add_argument('--ramp', type=int, default=20)
    parser.add_argument('--bitrate', type=int, default=6_000_000)
    parser.add_argument('--processes', type=int, default=16)
    parser.add_argument('--cpu-workers', type=int, default=4)
    parser.add_argument('--mux-backend', choices=['ffmpeg', 'pyav'], default='pyav')
    parser.add_argument('--manifest-chunks', type=int, default=3600)
    parser.add_argument('--origin-delay', type=float, default=.03)
    parser.add_argument('--tmp-dir', default='/dev/shm')
    parser.add_argument('--report')
    args = parser.parse_args()
    if args.channels < 1 or args.duration < 20 or args.ramp < 0:
        parser.error('channels >= 1, duration >= 20 and ramp >= 0 required')
    with tempfile.TemporaryDirectory(prefix='shaka-load-', dir=args.tmp_dir) as tmp:
        passed = asyncio.run(exercise(args, Path(tmp)))
    raise SystemExit(0 if passed else 1)


if __name__ == '__main__':
    main()
