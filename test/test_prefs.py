# -*- coding: utf-8 -*-
"""Izolovane testy pamatovani vysky/RPM sekacky (bez ROS).

    python3 -m pytest -q src/vitulus/vitulus_mower/test/test_prefs.py
"""
import json
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'src'))

from node_mower.prefs import PrefsStore, RestoreLogic  # noqa: E402

MAXH = 65


class Board(object):
    """Krmi RestoreLogic zpravami /mower/status v 2 Hz jako deska."""

    def __init__(self, logic):
        self.logic = logic
        self.t = 100.0
        self.actions = []

    def status(self, status, h=MAXH, sp=2000, spin=0, n=1, dt=0.5):
        for _ in range(n):
            self.t += dt
            self.actions += self.logic.on_status(self.t, status, h, MAXH, sp, spin)

    def silence(self, s):
        end = self.t + s
        while self.t < end:
            self.t += 1.0
            self.logic.tick(self.t)


class Base(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.path = os.path.join(self.dir, 'mower', 'preferences.json')
        self.store = PrefsStore(self.path)

    def fresh(self):
        self.logic = RestoreLogic(PrefsStore(self.path))
        self.board = Board(self.logic)
        return self.board


class TestStore(Base):
    def test_roundtrip_atomic(self):
        self.store.set('cut_height', 42, 'ui')
        self.store.set('motor_rpm', 2600, 'ui')
        self.assertEqual(sorted(f for f in os.listdir(os.path.dirname(self.path))),
                         ['preferences.json'])  # zadny .tmp nezustal
        s2 = PrefsStore(self.path)
        self.assertEqual((s2.cut_height, s2.motor_rpm), (42, 2600))

    def test_corrupt_file_is_empty(self):
        os.makedirs(os.path.dirname(self.path))
        with open(self.path, 'w') as f:
            f.write('{nope')
        s = PrefsStore(self.path)
        self.assertIsNone(s.cut_height)
        self.assertIsNone(s.motor_rpm)

    def test_invalid_values_dropped(self):
        os.makedirs(os.path.dirname(self.path))
        with open(self.path, 'w') as f:
            json.dump({'cut_height': 5, 'motor_rpm': 0}, f)
        s = PrefsStore(self.path)
        self.assertIsNone(s.cut_height)
        self.assertIsNone(s.motor_rpm)


class TestRestore(Base):
    def setUp(self):
        Base.setUp(self)
        self.store.set('cut_height', 40, 'ui')
        self.store.set('motor_rpm', 2600, 'ui')

    def power_on(self, b):
        b.status('OFF', n=4)
        self.logic.on_power_cmd(True)
        b.status('INIT', n=2)
        b.silence(5)              # HAL reset desky, rosserial resync
        b.status('INIT', n=3)
        b.status('HOME', h=30, n=6)

    def test_power_on_restores_height_and_rpm_once(self):
        b = self.fresh()
        self.power_on(b)
        b.status('READY', n=8)
        self.assertEqual(b.actions, [('height', 40), ('rpm', 2600)])
        # dalsi READY uz nic neposila (jedna obnova za relaci)
        b.status('CHANGE_HEIGHT', h=50, n=3)
        b.status('READY', h=40, sp=2600, n=10)
        self.assertEqual(len(b.actions), 2)

    def test_never_motor_on(self):
        b = self.fresh()
        self.power_on(b)
        b.status('READY', n=8)
        self.assertTrue(all(k in ('height', 'rpm') for k, _ in b.actions))

    def test_settle_needed(self):
        b = self.fresh()
        self.power_on(b)
        b.status('READY', n=4)    # 1.5 s od prvniho READY
        self.assertEqual(b.actions, [])
        b.status('READY', n=2)
        self.assertEqual(len(b.actions), 2)

    def test_busy_program_skips(self):
        b = self.fresh()
        self.logic.busy = True
        self.power_on(b)
        b.status('READY', n=8)
        self.assertEqual(b.actions, [])
        self.assertIn('program', self.logic.last_result)

    def test_explicit_command_wins(self):
        b = self.fresh()
        self.power_on(b)
        b.status('READY', n=1)
        self.logic.on_command('height')   # mise / uzivatel poslal vlastni vysku
        b.status('READY', n=8)
        self.assertEqual(b.actions, [('rpm', 2600)])

    def test_command_while_off_does_not_block_next_restore(self):
        b = self.fresh()
        b.status('OFF', n=4)
        self.logic.on_command('height')   # uzivatel nastavil pri vypnute sekacce
        self.logic.on_command('rpm')
        self.power_on(b)
        b.status('READY', n=8)
        self.assertEqual(b.actions, [('height', 40), ('rpm', 2600)])

    def test_switching_off_skips(self):
        # node nastartoval, kdyz deska prave parkuje pri vypinani
        b = self.fresh()
        self.logic.on_power_cmd(False)
        b.status('HOME', h=30, n=4)
        b.status('READY', n=8)
        self.assertEqual(b.actions, [])

    def test_blade_not_parked_height_kept(self):
        # restart stacku, deska bezi dal s rucne nastavenou vyskou -> vysku nemenit
        b = self.fresh()
        b.status('READY', h=35, sp=2600, n=8)
        self.assertEqual(b.actions, [])
        self.assertIn('not parked', self.logic.last_result)

    def test_motor_spinning_rpm_untouched(self):
        b = self.fresh()
        b.status('READY', h=MAXH, sp=1800, spin=1800, n=8)
        self.assertEqual(b.actions, [('height', 40)])

    def test_run_status_skips_session(self):
        b = self.fresh()
        self.power_on(b)
        b.status('RUN', spin=2000, n=3)
        b.status('READY', n=8)
        self.assertEqual(b.actions, [])

    def test_reconnect_without_power_cycle(self):
        # "Unable to sync" -> znovu pripojeno: deska po resetu HOME -> READY
        b = self.fresh()
        b.status('READY', h=40, sp=2600, n=8)      # bezna relace, nic k obnove
        self.assertEqual(b.actions, [])
        b.silence(10)
        b.status('INIT', n=2)
        b.status('HOME', h=20, n=4)
        b.status('READY', n=8)
        self.assertEqual(b.actions, [('height', 40), ('rpm', 2600)])

    def test_park_after_mission_not_restored(self):
        # mise: obnova preskocena (busy), safe_blade_shutdown zaparkuje na max
        b = self.fresh()
        self.logic.busy = True
        self.power_on(b)
        b.status('READY', n=8)
        self.logic.on_command('height')
        b.status('CHANGE_HEIGHT', h=30, n=3)
        b.status('RUN', h=30, spin=2400, n=3)
        self.logic.busy = False
        b.status('CHANGE_HEIGHT', h=50, n=3)
        b.status('READY', n=8)                      # zaparkovano na max
        self.assertEqual(b.actions, [])

    def test_no_prefs_nothing(self):
        os.remove(self.path)
        b = self.fresh()
        self.power_on(b)
        b.status('READY', n=8)
        self.assertEqual(b.actions, [])


if __name__ == '__main__':
    unittest.main()
