"""Optional browser checks: install Playwright and its Chromium to run these."""
import importlib.util
import unittest
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import proxy


@unittest.skipUnless(importlib.util.find_spec('playwright'), 'optional Playwright dependency')
class EPGBrowserTests(unittest.TestCase):
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
        self.errors = []
        self.poster_requests = []
        now = datetime.now(timezone.utc)
        self.description = 'Primera línea de la sinopsis.\n' + 'Una descripción larga y legible. ' * 90
        first = {
            'start': now - timedelta(minutes=20), 'stop': now + timedelta(minutes=90),
            'title': 'Programa de prueba', 'subtitle': '2026 | 12',
            'description': self.description, 'category': 'Cine · Drama',
            'poster': 'https://images.example.test/poster.svg',
        }
        second = dict(first, start=first['stop'], stop=first['stop'] + timedelta(minutes=90),
                      title='Siguiente programa', description='Otra sinopsis.', poster='')
        channel = SimpleNamespace(logo='', name='Canal de prueba', tvg_id='test')
        data = {'error': '', 'rows': [
            {'channel': channel, 'programmes': [first, second], 'next': second}
        ]}
        self.html = proxy.render_epg_page(data)
        self.page = self.browser.new_page(viewport={'width': 1100, 'height': 750})
        self.addCleanup(self.page.close)
        self.page.on('pageerror', lambda error: self.errors.append(str(error)))
        self.mock_poster(self.page)
        self.page.set_content(self.html)

    def mock_poster(self, page):
        def serve(route):
            self.poster_requests.append(route.request.url)
            route.fulfill(content_type='image/svg+xml', body=(
                '<svg xmlns="http://www.w3.org/2000/svg" width="104" height="152">'
                '<rect width="104" height="152" fill="#37644f"/></svg>'
            ))
        page.route('https://images.example.test/**', serve)

    def tearDown(self):
        self.assertEqual(self.errors, [])

    def visible(self, page=None):
        from playwright.sync_api import expect
        info = (page or self.page).locator('#programme-info')
        expect(info).to_be_visible()
        return info

    def hidden(self, page=None):
        from playwright.sync_api import expect
        expect((page or self.page).locator('#programme-info')).to_be_hidden()

    def test_hover_content_remains_open_to_read_scroll_and_dismiss(self):
        self.page.locator('[data-programme]').first.hover()
        info = self.visible()
        self.assertEqual(info.locator('h2').inner_text(), 'Programa de prueba')
        self.assertEqual(info.locator('.info-description').inner_text(), self.description.strip())
        info.hover()
        self.page.wait_for_timeout(250)
        self.visible()
        info.evaluate('(el) => el.scrollTop = 120')
        self.page.wait_for_timeout(250)
        self.visible()
        self.assertGreater(info.evaluate('(el) => el.scrollTop'), 0)
        self.page.keyboard.press('Escape')
        self.hidden()
        self.assertIsNone(self.page.locator('[data-programme]').first.get_attribute('aria-describedby'))

    def assert_poster_layout(self, info):
        from playwright.sync_api import expect
        poster = info.locator('.info-poster')
        expect(poster).to_be_visible()
        image_box = poster.bounding_box()
        title_box = info.locator('h2').bounding_box()
        heading_box = info.locator('.info-heading').bounding_box()
        description_box = info.locator('.info-description').bounding_box()
        self.assertLess(image_box['x'] + image_box['width'], title_box['x'])
        self.assertGreaterEqual(description_box['y'], heading_box['y'] + heading_box['height'])
        self.assertEqual(description_box['x'], image_box['x'])
        self.assertGreater(description_box['width'], image_box['width'] + title_box['width'])

    def test_poster_loads_only_on_open_with_title_right_and_description_below(self):
        programme = self.page.locator('[data-programme]').first
        self.assertEqual(self.poster_requests, [])
        self.assertEqual(programme.evaluate('(el) => getComputedStyle(el).cursor'), 'default')
        programme.hover()
        info = self.visible()
        self.page.wait_for_function('document.querySelector(".info-poster").naturalWidth > 0')
        self.assertEqual(len(self.poster_requests), 1)
        self.assert_poster_layout(info)

    def test_missing_or_failed_poster_does_not_leave_a_broken_image_or_stale_poster(self):
        from playwright.sync_api import expect
        first, second = self.page.locator('[data-programme]').all()
        first.hover()
        info = self.visible()
        self.page.wait_for_function('document.querySelector(".info-poster").naturalWidth > 0')
        self.page.keyboard.press('Escape')
        second.hover()
        expect(info.locator('h2')).to_have_text('Siguiente programa')
        expect(info.locator('.info-poster')).to_be_hidden()
        self.assertIsNone(info.locator('.info-poster').get_attribute('src'))
        self.page.keyboard.press('Escape')
        self.page.route('https://images.example.test/**', lambda route: route.fulfill(status=404, body=''))
        first.evaluate('''el => {
            const details = JSON.parse(el.dataset.programme);
            details.poster = 'https://images.example.test/missing.svg';
            el.dataset.programme = JSON.stringify(details);
        }''')
        first.hover()
        self.visible()
        expect(info.locator('.info-poster')).to_be_hidden()
        expect(info.locator('h2')).to_have_text('Programa de prueba')

    def test_keyboard_focus_switches_content_and_filter_closes_card(self):
        self.page.locator('#filter').focus()
        self.page.keyboard.press('Tab')
        self.visible()
        self.page.keyboard.press('Tab')
        self.assertEqual(self.visible().locator('h2').inner_text(), 'Siguiente programa')
        self.page.locator('#filter').fill('no existe')
        self.hidden()
        self.assertFalse(self.page.locator('.channel').is_visible())

    def test_card_stays_in_viewport_and_scroll_dismisses_it(self):
        self.page.set_viewport_size({'width': 760, 'height': 320})
        self.page.locator('.timeline').evaluate('(el) => el.style.minWidth = "1300px"')
        self.page.locator('[data-programme]').nth(1).hover()
        info = self.visible()
        box = info.bounding_box()
        self.assertGreaterEqual(box['x'], 11)
        self.assertGreaterEqual(box['y'], 11)
        self.assertLessEqual(box['x'] + box['width'], 749)
        self.assertLessEqual(box['y'] + box['height'], 309)
        self.page.locator('.epg-scroll').evaluate('(el) => el.scrollLeft += 50')
        self.hidden()

    def test_touch_opens_card_and_outside_tap_closes_it(self):
        page = self.browser.new_page(viewport={'width': 390, 'height': 700},
                                     is_mobile=True, has_touch=True)
        self.addCleanup(page.close)
        page.on('pageerror', lambda error: self.errors.append(str(error)))
        self.mock_poster(page)
        page.set_content(self.html)
        page.locator('[data-programme]').first.tap()
        info = self.visible(page)
        page.wait_for_timeout(300)
        self.visible(page)
        self.assert_poster_layout(info)
        box = info.bounding_box()
        self.assertGreaterEqual(box['x'], 11)
        self.assertLessEqual(box['x'] + box['width'], 379)
        page.touchscreen.tap(5, 690)
        self.hidden(page)


if __name__ == '__main__':
    unittest.main()
