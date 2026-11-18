"""Tests for picking what to click on a screen. Run with: python -m unittest discover tests"""
import os
import sys
import tempfile
import types
import unittest
from unittest.mock import Mock, patch

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


class GameProfiles(unittest.TestCase):
    def test_screen_reader_rejects_a_read_from_before_focus_returned(self):
        reader = anti_afk.ScreenReader()
        reader.latest = {'time': 98}
        with (patch.object(anti_afk, 'focus_changed_at', 99),
              patch.object(anti_afk.time, 'monotonic', return_value=100)):
            self.assertIsNone(reader.fresh())
            reader.latest = {'time': 99.5}
            self.assertEqual(reader.fresh(), reader.latest)

    def test_immediate_menu_click_can_use_a_newer_frame_already_in_flight(self):
        screen = {'name': 'loadouts_custom', 'click': 'DEFAULT', 'press': None, 'click_delay': 0}
        read = {'time': 11, 'items': [('DEFAULT', (20, 30), 0.99, 10)]}
        reader = Mock(max_age=Mock(return_value=5), fresh=Mock(return_value=read))
        with (patch.object(anti_afk, 'reader', reader),
              patch.object(anti_afk, 'wait', return_value=True),
              patch.object(anti_afk, 'running', Mock(is_set=lambda: True)),
              patch.object(anti_afk, 'focused', Mock(is_set=lambda: True)),
              patch.object(anti_afk, 'menu_reason', return_value='menu'),
              patch.object(anti_afk, 'classify', return_value=(screen, None)),
              patch.object(anti_afk, 'stats', {'clicks': 0}),
              patch.object(anti_afk, 'DRY_RUN', True),
              patch.object(anti_afk.time, 'monotonic', return_value=12)):
            self.assertTrue(anti_afk.click_screen(screen, 10))
        reader.fresh.assert_called_once_with(newer_than=10)

    def test_failed_menu_click_retries_on_a_new_read_without_waiting_for_cooldown(self):
        screen = {'name': 'multiplayer_lobby', 'click': 'FIND A MATCH'}
        reads = [{'time': 1}, {'time': 1}, {'time': 2}, {'time': 3}]
        reader = Mock(fresh=Mock(side_effect=reads))
        stop = Mock(is_set=Mock(side_effect=[False, False, False, False, True]))
        with (patch.object(anti_afk, 'stop_event', stop),
              patch.object(anti_afk, 'started', Mock(is_set=lambda: True)),
              patch.object(anti_afk, 'running', Mock(is_set=lambda: True)),
              patch.object(anti_afk, 'focused', Mock(is_set=lambda: True)),
              patch.object(anti_afk, 'menu_reason', return_value='menu'),
              patch.object(anti_afk, 'reader', reader),
              patch.object(anti_afk, 'detect', return_value=(anti_afk.SCREEN, 'lobby', screen)),
              patch.object(anti_afk, 'enter_state'),
              patch.object(anti_afk.time, 'monotonic', return_value=100),
              patch.object(anti_afk, 'click_screen', side_effect=[False, True]) as click):
            anti_afk.run_state_machine()
        self.assertEqual([call.args for call in click.call_args_list], [(screen, 1), (screen, 2)])

    def test_black_ops_6_rechecks_small_compass_labels_in_the_game_window(self):
        rules = anti_afk.load_rules(anti_afk.profile_path('cod24-cod.exe'))
        frame = Mock()
        full = [('170', (165, 48), 0.99, 18)]
        compass = [*full, ('SE', (86, 73), 0.98, 18)]
        with (patch.object(anti_afk, 'rules', rules),
              patch.object(anti_afk, 'ocr', side_effect=[full, compass])):
            read = anti_afk.read_image(frame, (1547, 55, 2051, 1173), 0)
        self.assertTrue(anti_afk.has_gameplay_hud(read))
        self.assertEqual(read['text'], '170')
        frame.crop.assert_called_once_with((0, 0, 390, 164))

    def test_legacy_history_migrates_to_mw4_and_new_matches_keep_their_game(self):
        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, 'matches.csv')
            with open(path, 'w', encoding='utf-8') as f:
                f.write('date,duration_s,result\n2026-11-18 12:00,60,victory\n')
            with (patch.object(anti_afk, 'HISTORY_FILE', path),
                  patch.object(anti_afk, 'active_game', 'cod24-cod.exe'),
                  patch.object(anti_afk, 'match', {'start': 10, 'over_at': None}),
                  patch.object(anti_afk, 'stats', {'matches': 0, 'match_time': 0}),
                  patch.object(anti_afk, 'DRY_RUN', False),
                  patch.object(anti_afk.time, 'time', return_value=20)):
                anti_afk.migrate_history()
                anti_afk.migrate_history()
                anti_afk.end_match('defeat')
                rows = anti_afk.load_history()
            self.assertEqual([(row['game'], row['duration'], row['result']) for row in rows],
                             [('cod.exe', 60, 'victory'), ('cod24-cod.exe', 10, 'defeat')])
            with open(path, encoding='utf-8') as f:
                self.assertEqual(f.readline().strip(), 'date,duration_s,result,game')

    def test_black_ops_6_post_match_screens(self):
        rules = anti_afk.load_rules(anti_afk.profile_path('cod24-cod.exe'))
        for banner, name, result, action in (
            ('DEFEAT | TIME LIMIT REACHED', 'defeat', 'defeat', None),
            ('VICTORY | SCORE LIMIT REACHED', 'victory', 'victory', None),
            ('TIE | TIME LIMIT REACHED', 'tie', 'tie', None),
            ('BEST PLAY', 'best_play', None, None),
            ("WINNER'S CIRCLE | DEFEAT - ROGUE BLACK OPS", 'winners_circle_defeat', 'defeat', None),
            ("WINNER'S CIRCLE | VICTORY", 'winners_circle_victory', 'victory', None),
            ("WINNER'S CIRCLE | TIE", 'winners_circle_tie', 'tie', None),
            ("WINNER'S CIRCLE", 'winners_circle', 'over', None),
            ('PROGRESSION | ESC DISMISS | SEARCHING FOR A MATCH', 'progression', 'over', 'press ESC'),
            ('AFTER ACTION REPORT | SEARCHING FOR A MATCH', 'after_action_report', 'over', None),
            ('AFTER ACTION REPORT | SEARCHING FOR A MATCH | RETURNING TO LOBBY IN: 7 | DEFEAT',
             'after_action_report', 'defeat', None),
            ('AFTER ACTION REPORT | WAITING. LOADING LEVEL | OPEN AAR SUMMARY', 'match_lobby', None, None),
        ):
            with self.subTest(screen=name):
                read = {'text': banner, 'items': []}
                if name == 'tie':
                    read.update(bounds=(0, 0, 2000, 1000), items=[('TIE', (1000, 110), 0.99, 40)])
                screen, danger = anti_afk.classify(read, in_match=name != 'progression', screen_rules=rules)
                self.assertIsNone(danger)
                self.assertEqual(screen['name'], name)
                self.assertEqual(screen.get('match_result'), result)
                self.assertEqual(anti_afk.screen_action(screen), action)

    def test_tie_counts_once_and_starts_the_post_match_wait(self):
        with (patch.object(anti_afk, 'match', {'start': 10, 'over_at': None}),
              patch.object(anti_afk, 'stats', {'matches': 0, 'match_time': 0}),
              patch.object(anti_afk, 'DRY_RUN', True),
              patch.object(anti_afk.time, 'time', return_value=20)):
            anti_afk.end_match('tie')
            anti_afk.end_match('over')
            self.assertEqual(anti_afk.stats['matches'], 1)
            self.assertEqual(anti_afk.stats['last_match'], ('tie', 10))
            self.assertIsNotNone(anti_afk.match['over_at'])

    def test_black_ops_6_loadout_flow_selects_default_then_enforcer_row(self):
        rules = anti_afk.load_rules(anti_afk.profile_path('cod24-cod.exe'))
        custom = {'text': 'LOADOUTS | CUSTOM | DEFAULT | EDITLOADOUT',
                  'items': [('DEFAULT', (700, 600), 0.99, 100),
                            ('CUSTOM', (1222, 97), 0.99, 40),
                            ('DEFAULT', (1444, 97), 0.99, 40)]}
        presets = {'text': 'LOADOUTS | CUSTOM | DEFAULT | ENFORCER | RECON | STRATEGIST',
                   'items': [('ENFORCER', (1865, 1567), 0.99, 60),
                             ('Enforcer', (260, 250), 0.99, 40)]}
        for read, expected, target in (
            (custom, 'loadouts_custom', (1444, 97)),
            (presets, 'loadouts_default', (260, 250)),
        ):
            with self.subTest(screen=expected):
                screen, danger = anti_afk.classify(read, screen_rules=rules)
                self.assertIsNone(danger)
                self.assertEqual(screen['name'], expected)
                self.assertEqual(anti_afk.screen_click_target(read, screen), target)

    def test_detects_profiles_by_executable(self):
        with patch.object(anti_afk, 'foreground_window', return_value=('COD24-COD.EXE', 'Game')):
            path = anti_afk.foreground_profile_path()
            self.assertEqual(os.path.basename(path), 'cod24-cod.exe.json')
            self.assertIsNone(anti_afk.foreground_profile_path('cod.exe'))

    def test_black_ops_6_title_is_used_only_when_the_executable_is_unavailable(self):
        title = 'Call of Duty®: Black Ops 6'
        with patch.object(anti_afk, 'foreground_window', return_value=('', title)):
            self.assertEqual(anti_afk.foreground_profile_path(), anti_afk.profile_path('cod24-cod.exe'))
            self.assertTrue(anti_afk.game_is_focused('cod24-cod.exe'))
            self.assertFalse(anti_afk.game_is_focused('cod.exe'))
        self.assertFalse(anti_afk.game_is_focused('cod24-cod.exe', ('other.exe', title)))
        self.assertFalse(anti_afk.game_is_focused('cod.exe', ('', 'Terminal: --game cod.exe')))

    def test_empty_profile_keeps_input_paused(self):
        with (patch.object(anti_afk, 'foreground_window', return_value=('cod24-cod.exe', 'Game')),
              patch.object(anti_afk, '_user32', return_value=Mock(GetForegroundWindow=lambda: 42)),
              patch.object(anti_afk, 'load_rules', return_value={'screens': []}),
              patch.object(anti_afk, 'focused') as focused,
              patch.object(anti_afk, 'stop_event') as stop):
            stop.is_set.side_effect = [False, True]
            focused.is_set.return_value = False
            anti_afk.watch_focus('')
            focused.set.assert_not_called()

    def test_black_ops_6_lobby_recognizes_find_a_match_with_store_tab_visible(self):
        rules = anti_afk.load_rules(anti_afk.profile_path('cod24-cod.exe'))
        read = {'text': 'BLACKOPS6 | MULTIPLAYER | LOBBY | STORE | FIND A MATCH',
                'items': [('FIND A MATCH', (497, 1847), 0.96, 40)]}
        screen, danger = anti_afk.classify(read, screen_rules=rules)
        self.assertIsNone(danger)
        self.assertEqual(screen['name'], 'multiplayer_lobby')
        self.assertEqual(anti_afk.find_text(read, screen['click']), (497, 1847))
        screen, _ = anti_afk.classify({**read, 'text': read['text'].replace('FIND A MATCH', ''),
                                        'items': []}, screen_rules=rules)
        self.assertIsNone(screen)

    def test_legacy_rules_keep_match_metadata(self):
        screens = {rule['name']: rule for rule in anti_afk.load_rules()['screens']}
        self.assertEqual(screens['victory']['match_result'], 'victory')
        self.assertTrue(screens['main_menu']['pre_match'])

    def test_black_ops_6_quick_play_targets_the_find_a_match_tile(self):
        rules = anti_afk.load_rules(anti_afk.profile_path('cod24-cod.exe'))
        read = {
            'text': 'MULTIPLAYER | Quick Play | Private Match | FIND A MATCH | Core Game Modes [2/12]',
            'items': [('Private Match', (3100, 404), 0.99, 100),
                      ('FIND A MATCH', (314, 425), 0.98, 35)],
        }
        screen, danger = anti_afk.classify(read, screen_rules=rules)
        self.assertIsNone(danger)
        self.assertEqual(screen['name'], 'quick_play')
        self.assertEqual(anti_afk.find_text(read, screen['click']), (314, 425))
        read['text'] = read['text'].replace('Quick Play', '')
        self.assertEqual(anti_afk.classify(read, screen_rules=rules), (None, None))

    def test_black_ops_6_quick_play_prefers_face_off_when_both_tiles_are_visible(self):
        rules = anti_afk.load_rules(anti_afk.profile_path('cod24-cod.exe'))
        read = {
            'text': 'MULTIPLAYER | Quick Play | FIND A MATCH | Core Game Modes | Face Off Moshpit',
            'items': [('FIND A MATCH', (314, 425), 0.98, 35),
                      ('Face Off Moshpit', (341, 914), 0.99, 36)],
        }
        screen, danger = anti_afk.classify(read, screen_rules=rules)
        self.assertIsNone(danger)
        self.assertEqual(screen['name'], 'quick_play_face_off')
        self.assertEqual(anti_afk.find_text(read, screen['click']), (341, 914))
        read['text'] = read['text'].replace('Face Off Moshpit', 'Face Of Moshpit')
        read['items'][1] = ('Face Of Moshpit', (341, 914), 0.98, 36)
        self.assertEqual(anti_afk.classify(read, screen_rules=rules)[0]['name'], 'quick_play')

    def test_black_ops_6_search_queue_takes_priority_over_match_buttons(self):
        rules = anti_afk.load_rules(anti_afk.profile_path('cod24-cod.exe'))
        read = {'text': 'SEARCHING FOR A MATCH <14MS PING | BLACKOPS6 | MULTIPLAYER | LOBBY | '
                        'QUICK PLAY | FACE OFF MOSHPIT | FIND A MATCH', 'items': []}
        screen, danger = anti_afk.classify(read, screen_rules=rules)
        self.assertIsNone(danger)
        self.assertEqual(screen['name'], 'searching')
        self.assertIsNone(anti_afk.screen_action(screen))

    def test_black_ops_6_waits_for_lobby_loading_and_killcam_with_cursor_hidden(self):
        rules = anti_afk.load_rules(anti_afk.profile_path('cod24-cod.exe'))
        with (patch.object(anti_afk, 'rules', rules),
              patch.object(anti_afk, 'menu_reason', return_value=None),
              patch.object(anti_afk, 'started', Mock(is_set=lambda: True)),
              patch.object(anti_afk, 'running', Mock(is_set=lambda: True)),
              patch.object(anti_afk, 'focused', Mock(is_set=lambda: True)),
              patch.object(anti_afk, 'stop_event', Mock(is_set=lambda: False))):
            self.assertEqual(anti_afk.detect()[0], anti_afk.MENU)
            state, _, screen = anti_afk.detect(
                {'text': 'BLACKOPS6 | MULTIPLAYER | LOBBY | FIND A MATCH', 'items': []})
            self.assertEqual(state, anti_afk.SCREEN)
            self.assertEqual(anti_afk.screen_action(screen), 'click FIND A MATCH')
            for text, expected in (
                ('VOTING IN PROGRESS: 25.5', 'match_lobby'),
                ('WAITING. LOADING LEVEL', 'match_lobby'),
                ('MATCH BEGINNING IN 3', 'match_lobby'),
                ('LOADING MAP | EXCHANGE', 'loading_map'),
                ('KILLCAM | KILLEDBY: PLAYER', 'killcam'),
            ):
                with self.subTest(text=text):
                    state, _, screen = anti_afk.detect({'text': text, 'items': []})
                    self.assertEqual(state, anti_afk.SCREEN)
                    self.assertEqual(screen['name'], expected)
                    self.assertIsNone(anti_afk.screen_action(screen))
            self.assertEqual(anti_afk.detect({'text': 'EXCHANGE', 'items': []})[0], anti_afk.MENU)
            for items in (
                [('TIE', (164, 332), 0.99, 19), ('7:49', (165, 350), 0.97, 23)],
                [('276', (164, 54), 0.99, 19), ('NW', (245, 84), 0.96, 24)],
                [('156', (168, 57), 0.99, 19), ('SE', (118, 68), 0.96, 18),
                 ('WINNING', (167, 335), 0.97, 18)],
                [('S', (168, 57), 0.99, 19), ('SW', (246, 68), 0.96, 18)],
                [('NW', (118, 68), 0.96, 18), ('N', (168, 57), 0.99, 19)],
                [('SE', (168, 57), 0.99, 19), ('WINNING', (167, 335), 0.97, 18)],
                [('LOSING', (164, 332), 0.99, 19), ('5:33', (165, 350), 0.97, 23)],
                [('WINNING', (164, 332), 0.99, 19), ('3:12', (165, 350), 0.97, 23)],
            ):
                hud = {'text': ' | '.join(item[0] for item in items), 'items': items,
                       'bounds': (0, 0, 2070, 1182)}
                self.assertEqual(anti_afk.detect(hud)[0], anti_afk.PLAYING)

    def test_black_ops_6_kick_notice_takes_priority_over_lobby(self):
        rules = anti_afk.load_rules(anti_afk.profile_path('cod24-cod.exe'))
        read = {'text': 'BLACKOPS6 | MULTIPLAYER | LOBBY | FIND A MATCH | NOTICE | Kicked for inactivity. | EXIT',
                'items': [('EXIT', (1236, 1240), 0.99, 30)]}
        screen, danger = anti_afk.classify(read, screen_rules=rules)
        self.assertIsNone(danger)
        self.assertEqual(screen['match_result'], 'kicked')
        self.assertEqual(anti_afk.find_text(read, screen['click']), (1236, 1240))

    def test_classification_uses_the_selected_profiles_order(self):
        read = {'text': 'SCOREBOARD | CONTINUE', 'items': []}
        specific = {'if_seen': ['SCOREBOARD', 'CONTINUE'], 'name': 'post_game'}
        general = {'if_seen': ['SCOREBOARD'], 'name': 'scoreboard'}
        rules = {'screens': [specific, general], 'never_click_if_seen': []}
        screen, danger = anti_afk.classify(read, screen_rules=rules)
        self.assertIs(screen, specific)
        self.assertIsNone(danger)


if __name__ == '__main__':
    unittest.main()
