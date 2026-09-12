"""Optional browser regressions for current programmes and expandable clients."""
import importlib.util
import unittest

import proxy


@unittest.skipUnless(importlib.util.find_spec('playwright'), 'optional Playwright dependency')
class StatusBrowserTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from playwright.sync_api import sync_playwright
        cls.playwright = sync_playwright().start()
        cls.browser = cls.playwright.chromium.launch()

    @classmethod
    def tearDownClass(cls):
        cls.browser.close()
        cls.playwright.stop()

    def setUp(self):
        self.data = {
            'channels_total': 2, 'open': 1, 'token_ok': True, 'token_left_s': 3600,
            'live': [{
                'slug': 'test', 'name': 'Canal de prueba', 'logo': '', 'active_s': 90,
                'idle_s': 1, 'viewers': 1, 'quality': '1080p50', 'cached': 8,
                'clients': [{'ip': '192.0.2.10', 'ua': 'VLC', 'connected': '1m 30s'}],
                'programme': {'title': 'Clasificación de Fórmula 1', 'time': '15:00–16:00'},
            }],
        }
        self.errors = []
        self.documents = 0
        self.page = self.browser.new_page(viewport={'width': 1100, 'height': 750})
        self.addCleanup(self.page.close)
        self.page.clock.install()
        self.page.on('pageerror', lambda error: self.errors.append(str(error)))

        def serve(route):
            if route.request.url.endswith('/status.json'):
                route.fulfill(json=self.data)
            else:
                self.documents += 1
                route.fulfill(content_type='text/html', body=proxy.render_status_page(self.data))

        self.page.route('https://status.example.test/**', serve)
        self.page.goto('https://status.example.test/prefix/status')

    def tearDown(self):
        self.assertEqual(self.errors, [])

    def test_programme_is_below_channel_with_schedule_and_literal_text(self):
        from playwright.sync_api import expect
        info = self.page.locator('.channel-info')
        expect(info.locator('strong')).to_have_text('Canal de prueba')
        expect(info.locator('.programme-title')).to_have_text('Clasificación de Fórmula 1')
        expect(info.locator('.programme-time')).to_have_text('15:00–16:00')
        self.assertGreater(info.locator('.programme-title').bounding_box()['y'],
                           info.locator('strong').bounding_box()['y'])
        title = '</script><img src=x onerror="window.injected=true">'
        self.data['live'][0]['programme']['title'] = title
        self.page.clock.run_for(4100)
        expect(info.locator('.programme-title')).to_have_text(title)
        self.assertIsNone(self.page.evaluate('window.injected'))
        self.assertEqual(info.locator('img').count(), 0)

    def test_programme_updates_without_reload_or_collapsing_clients(self):
        from playwright.sync_api import expect
        summary = self.page.locator('.channel-summary')
        summary.click()
        expect(self.page.locator('.client-details')).to_be_visible()
        self.data['live'][0]['programme'] = {'title': 'Siguiente emisión', 'time': '16:00–17:00'}
        self.page.clock.run_for(4100)
        expect(self.page.locator('.programme-title')).to_have_text('Siguiente emisión')
        expect(summary).to_have_attribute('aria-expanded', 'true')
        expect(self.page.locator('.client-details')).to_be_visible()
        expect(self.page.locator('.client-count')).to_have_text('1')
        self.assertEqual(self.documents, 1)

    def test_missing_programme_keeps_channel_and_clients_visible(self):
        from playwright.sync_api import expect
        self.data['live'][0]['programme'] = None
        self.page.clock.run_for(4100)
        expect(self.page.locator('.programme-title')).to_have_count(0)
        expect(self.page.locator('.channel-info strong')).to_have_text('Canal de prueba')
        expect(self.page.locator('.client-count')).to_have_text('1')

    def test_mobile_long_title_fits_and_clients_remain_a_list(self):
        from playwright.sync_api import expect
        self.page.set_viewport_size({'width': 390, 'height': 800})
        title = 'Una clasificación de Fórmula 1 con un título de programa muy largo ' * 4
        self.data['live'][0]['programme']['title'] = title
        self.page.clock.run_for(4100)
        expect(self.page.locator('.programme-title')).to_have_text(title)
        self.assertLessEqual(self.page.evaluate('document.documentElement.scrollWidth'), 390)
        self.page.locator('.channel-summary').click()
        expect(self.page.locator('.client-details ul.client-list > li.client')).to_be_visible()


if __name__ == '__main__':
    unittest.main()
