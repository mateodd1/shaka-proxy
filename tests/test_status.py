"""Status-page regressions for client counts and expandable client lists."""
import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import proxy


class StatusPageTests(unittest.TestCase):
    def make_state(self):
        now = proxy.time.monotonic()
        directory = tempfile.TemporaryDirectory(prefix='shaka-status-')
        self.addCleanup(directory.cleanup)
        session = SimpleNamespace(
            started_mono=now - 90,
            last_access=now - 2,
            clients={
                'one': {'ip': '192.0.2.10', 'ua': 'VLC iOS', 'since_mono': now - 65},
                'two': {'ip': '192.0.2.11', 'ua': 'TiviMate', 'since_mono': now - 5},
            },
            viewers=2,
            ch=SimpleNamespace(name='DAZN F1', logo='https://example.test/logo.png', keys={}),
            hls_dir=Path(directory.name),
            task=SimpleNamespace(done=lambda: False),
            video=SimpleNamespace(height=1080, fps=50, protection_scheme='cenc'),
            audios=[],
            started_at=proxy.time.time() - 90,
        )
        return SimpleNamespace(
            sessions={'dazn-f1': session},
            playlist=SimpleNamespace(channels=[session.ch]),
            epg=SimpleNamespace(programmes={}),
            tokens=SimpleNamespace(get=Mock(return_value='token'), exp=proxy.time.time() + 3600),
        )

    def test_snapshot_contains_exact_connected_clients(self):
        snapshot = proxy.session_snapshot(self.make_state())
        channel = snapshot['live'][0]
        self.assertEqual(channel['viewers'], 2)
        self.assertEqual(len(channel['clients']), 2)
        self.assertEqual(channel['encryption'], 'DRM')
        self.assertEqual(
            [(client['ip'], client['ua'], client['connected']) for client in channel['clients']],
            [('192.0.2.10', 'VLC iOS', '1m 05s'), ('192.0.2.11', 'TiviMate', '5s')],
        )

    def test_page_builds_numeric_accessible_expandable_client_list(self):
        page = proxy.render_status_page(proxy.session_snapshot(self.make_state()))
        self.assertIn("<title>Shaka-proxy · Estado</title>", page)
        self.assertIn("<header><h1>Shaka-proxy</h1></header>", page)
        self.assertIn("String(clients.length)", page)
        self.assertIn("aria-expanded", page)
        self.assertIn("aria-controls", page)
        self.assertIn("client-details", page)
        self.assertIn("expanded.has(ch.slug)", page)
        self.assertIn("client.ip", page)
        self.assertIn("element('ul', 'client-list')", page)
        self.assertIn("element('li', 'client')", page)
        self.assertIn("name.append(element('div', 'encryption', ch.encryption))", page)
        self.assertNotIn("grid-template-columns: repeat(auto-fit", page)
        initial = page.split('<script id="initial-data" type="application/json">', 1)[1].split('</script>', 1)[0]
        data = json.loads(initial)
        self.assertEqual(len(data['live'][0]['clients']), 2)

    def test_zero_clients_is_numeric_and_not_expandable(self):
        state = self.make_state()
        session = state.sessions['dazn-f1']
        session.clients.clear()
        session.viewers = 0
        page = proxy.render_status_page(proxy.session_snapshot(state))
        self.assertIn("const expandable = clients.length > 0", page)
        self.assertIn("summary.tabIndex = expandable ? 0 : -1", page)
        self.assertIn("String(clients.length)", page)

    def test_current_programme_contains_only_title_and_local_schedule(self):
        state = self.make_state()
        now = datetime.now(timezone.utc)
        current = {'start': now - timedelta(minutes=20), 'stop': now + timedelta(minutes=40),
                   'title': 'Clasificación de Fórmula 1', 'description': 'Not needed by status'}
        state.epg.programmes['dazn-f1'] = [
            dict(current, title='Anterior', start=now - timedelta(hours=2), stop=current['start']),
            current,
            dict(current, title='Siguiente', start=current['stop'], stop=now + timedelta(hours=2)),
        ]
        programme = proxy.session_snapshot(state)['live'][0]['programme']
        self.assertEqual(programme, {
            'title': current['title'],
            'time': f"{proxy.fmt_epg_time(current['start'])}–{proxy.fmt_epg_time(current['stop'])}",
        })
        page = proxy.render_status_page(proxy.session_snapshot(state))
        self.assertIn("element('div', 'programme-title', ch.programme.title)", page)
        self.assertIn("element('div', 'programme-time', ch.programme.time)", page)

    def test_missing_expired_or_future_epg_does_not_invent_current_programme(self):
        state = self.make_state()
        now = datetime.now(timezone.utc)
        for programmes in ([], [
            {'start': now - timedelta(hours=2), 'stop': now - timedelta(hours=1), 'title': 'Anterior'},
            {'start': now + timedelta(hours=1), 'stop': now + timedelta(hours=2), 'title': 'Después'},
        ]):
            with self.subTest(programmes=programmes):
                state.epg.programmes['dazn-f1'] = programmes
                self.assertIsNone(proxy.session_snapshot(state)['live'][0]['programme'])

    def test_programme_switches_at_its_end_without_reloading_epg(self):
        from unittest.mock import patch
        state = self.make_state()
        boundary = datetime(2026, 9, 12, 14, 0, tzinfo=timezone.utc)
        state.epg.programmes['dazn-f1'] = [
            {'start': boundary - timedelta(hours=1), 'stop': boundary, 'title': 'Anterior'},
            {'start': boundary, 'stop': boundary + timedelta(hours=1), 'title': 'Nuevo'},
        ]
        with patch.object(proxy, 'datetime', wraps=datetime) as clock:
            clock.now.return_value = boundary - timedelta(seconds=1)
            self.assertEqual(proxy.session_snapshot(state)['live'][0]['programme']['title'], 'Anterior')
            clock.now.return_value = boundary
            self.assertEqual(proxy.session_snapshot(state)['live'][0]['programme']['title'], 'Nuevo')

    def test_programme_title_is_embedded_as_data_not_executable_html(self):
        state = self.make_state()
        now = datetime.now(timezone.utc)
        title = '</script><script>window.injected=true</script>'
        state.epg.programmes['dazn-f1'] = [
            {'start': now - timedelta(hours=1), 'stop': now + timedelta(hours=1), 'title': title},
        ]
        page = proxy.render_status_page(proxy.session_snapshot(state))
        self.assertNotIn(title, page)
        initial = page.split('<script id="initial-data" type="application/json">', 1)[1].split('</script>', 1)[0]
        self.assertEqual(json.loads(initial)['live'][0]['programme']['title'], title)


if __name__ == '__main__':
    unittest.main()
