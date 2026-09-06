"""Status-page regressions for client counts and expandable client lists."""
import json
import tempfile
import unittest
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
            ch=SimpleNamespace(name='DAZN F1', logo='https://example.test/logo.png'),
            hls_dir=Path(directory.name),
            task=SimpleNamespace(done=lambda: False),
            video=SimpleNamespace(height=1080, fps=50),
            started_at=proxy.time.time() - 90,
        )
        return SimpleNamespace(
            sessions={'dazn-f1': session},
            playlist=SimpleNamespace(channels=[session.ch]),
            tokens=SimpleNamespace(get=Mock(return_value='token'), exp=proxy.time.time() + 3600),
        )

    def test_snapshot_contains_exact_connected_clients(self):
        snapshot = proxy.session_snapshot(self.make_state())
        channel = snapshot['live'][0]
        self.assertEqual(channel['viewers'], 2)
        self.assertEqual(len(channel['clients']), 2)
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


if __name__ == '__main__':
    unittest.main()
