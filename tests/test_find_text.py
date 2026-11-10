"""Tests for picking what to click on a screen. Run with: python -m unittest discover tests"""
import os
import sys
import types
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
try:
    import anti_afk
except ImportError:  # not on Windows: the input libraries aren't installed; they aren't needed here
    sys.modules.setdefault('pydirectinput', types.ModuleType('pydirectinput'))
    keyboard = types.ModuleType('pynput.keyboard')
    keyboard.Controller = keyboard.Listener = object
    keyboard.Key = types.SimpleNamespace(f8='f8', f9='f9')
    sys.modules.setdefault('pynput', types.ModuleType('pynput'))
    sys.modules.setdefault('pynput.keyboard', keyboard)
    import anti_afk


def read(*items):
    return {'items': [(text, pos, 0.9, height) for text, pos, height in items]}


class FindText(unittest.TestCase):
    def test_big_button_over_small_tag(self):
        # Main menu with Hardcore selected: the big play button and a small tag on a mode tile.
        screen = read(('HARDCORE', (900, 700), 14), ('HARDCORE', (300, 200), 42))
        self.assertEqual(anti_afk.find_text(screen, 'HARDCORE'), (300, 200))

    def test_big_button_even_if_small_tag_is_read_more_cleanly(self):
        screen = read(('HARDCORE', (900, 700), 14), ('HARDCORE >', (300, 200), 42))
        self.assertEqual(anti_afk.find_text(screen, 'HARDCORE'), (300, 200))

    def test_same_size_prefers_the_button_text(self):
        screen = read(('Press QUICK PLAY to start', (500, 900), 30), ('QUICK PLAY', (300, 200), 32))
        self.assertEqual(anti_afk.find_text(screen, 'QUICK PLAY'), (300, 200))

    def test_not_found(self):
        self.assertIsNone(anti_afk.find_text(read(('FEATURED', (1, 1), 20)), 'HARDCORE'))


if __name__ == '__main__':
    unittest.main()
