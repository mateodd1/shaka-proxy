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
        now = datetime.now(timezone.utc)
        self.description = 'Primera línea de la sinopsis.\n' + 'Una descripción larga y legible. ' * 90
        first = {
            'start': now - timedelta(minutes=20), 'stop': now + timedelta(minutes=90),
            'title': 'Programa de prueba', 'subtitle': '2026 | 12',
            'description': self.description, 'category': 'Cine · Drama',
        }
        second = dict(first, start=first['stop'], stop=first['stop'] + timedelta(minutes=90),
                      title='Siguiente programa', description='Otra sinopsis.')
        channel = SimpleNamespace(logo='', name='Canal de prueba', tvg_id='test')
        data = {'error': '', 'rows': [
            {'channel': channel, 'programmes': [first, second], 'next': second}
        ]}
        self.html = proxy.render_epg_page(data)
        self.page = self.browser.new_page(viewport={'width': 1100, 'height': 750})
        self.addCleanup(self.page.close)
        self.page.on('pageerror', lambda error: self.errors.append(str(error)))
        self.page.set_content(self.html)

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
        page.set_content(self.html)
        page.locator('[data-programme]').first.tap()
        info = self.visible(page)
        page.wait_for_timeout(300)
        self.visible(page)
        box = info.bounding_box()
        self.assertGreaterEqual(box['x'], 11)
        self.assertLessEqual(box['x'] + box['width'], 379)
        page.touchscreen.tap(5, 690)
        self.hidden(page)


if __name__ == '__main__':
    unittest.main()
