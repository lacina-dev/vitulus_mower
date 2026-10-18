# -*- coding: utf-8 -*-
"""Pamatovani uzivatelske vysky sekani a otacek (RPM) sekacky.

Deska sekacky (STM_MowerController, rosserial) si po kazdem zapnuti napajeni
sama udela reset (HAL_NVIC_SystemReset), vyjede nahoru (HOME = max_height) a
RPM setpoint vezme ze sveho EEPROM defaultu. Uzivatelem nastavena vyska/RPM
se tedy ztrati. Tento modul drzi:

* ``PrefsStore``   - atomicky JSON soubor ~/.vitulus/mower/preferences.json
* ``RestoreLogic`` - rozhodnuti, KDY po startu/reconnectu desky znovu poslat
                     vysku (jen polohuje nuz) a RPM (jen hodnota setpointu).

Bezpecnostni pravidla (zamerne bez rospy, aby slo testovat izolovane):

* motor noze se NIKDY nezapina - modul vraci jen akce 'height' a 'rpm'
  (set_motor_rpm meni ve firmwaru jen setpoint, motor nespousti);
* obnova jen jednou za "relaci" desky (reset/reconnect/OFF->ON), az kdyz deska
  stoji READY po HOME na max_height (= cerstve zaparkovana), motor stoji;
* nic se neobnovuje, kdyz bezi program sekani (/mower_smach/busy), kdyz
  posledni povel napajeni byl OFF (vypinani parkuje na max) nebo kdyz od
  zacatku relace uz nekdo poslal vlastni vysku/RPM (explicitni povel vyhrava);
* preference meni jen uzivatel (UI Settings > Mower pres /mower/pref/*,
  joystick, displej robota) - vyska/RPM z programu/zony a parkovani pri STOPu
  (safe_blade_shutdown -> max_height) jsou docasne a neukladaji se.
"""

import json
import os
import time

HEIGHT_MIN = 20          # UI i firmware: 20 .. max_height (fw min default 25)
HEIGHT_MAX_FALLBACK = 100
RPM_MIN = 1              # firmware rpm_callback: 0 < rpm < 6001
RPM_MAX = 6000

DEFAULT_PATH = os.path.expanduser('~/.vitulus/mower/preferences.json')


def valid_height(h, max_height=0):
    try:
        h = int(h)
    except (TypeError, ValueError):
        return False
    top = int(max_height) if max_height and int(max_height) > 0 else HEIGHT_MAX_FALLBACK
    return HEIGHT_MIN <= h <= top


def valid_rpm(r):
    try:
        r = int(r)
    except (TypeError, ValueError):
        return False
    return RPM_MIN <= r <= RPM_MAX


class PrefsStore(object):
    """Uzivatelska preference vysky/RPM v JSON souboru (zapis atomicky)."""

    def __init__(self, path=DEFAULT_PATH):
        self.path = path
        self.data = self._load()

    def _load(self):
        try:
            with open(self.path) as f:
                d = json.load(f)
        except Exception:  # noqa: BLE001 - chybejici/poskozeny soubor = bez preferenci
            return {}
        if not isinstance(d, dict):
            return {}
        out = {}
        if valid_height(d.get('cut_height')):
            out['cut_height'] = int(d['cut_height'])
        if valid_rpm(d.get('motor_rpm')):
            out['motor_rpm'] = int(d['motor_rpm'])
        for k in ('cut_height_source', 'motor_rpm_source', 'updated'):
            if isinstance(d.get(k), str):
                out[k] = d[k]
        return out

    @property
    def cut_height(self):
        return self.data.get('cut_height')

    @property
    def motor_rpm(self):
        return self.data.get('motor_rpm')

    def set(self, key, value, source):
        """Ulozi preferenci ('cut_height' | 'motor_rpm'). Vraci True pri zmene."""
        value = int(value)
        changed = self.data.get(key) != value
        self.data[key] = value
        self.data[key + '_source'] = str(source)
        self.data['updated'] = time.strftime('%Y-%m-%dT%H:%M:%S%z')
        self._write()
        return changed

    def _write(self):
        d = os.path.dirname(self.path)
        if d and not os.path.isdir(d):
            os.makedirs(d)
        tmp = '{}.tmp.{}'.format(self.path, os.getpid())
        with open(tmp, 'w') as f:
            json.dump(self.data, f, indent=1, sort_keys=True)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, self.path)


class RestoreLogic(object):
    """Stavovy automat obnovy po startu/reconnectu desky (bez rospy).

    Volajici krmi ``on_status`` kazdou zpravou /mower/status (cas t v s) a
    ``tick`` periodicky (vypadek zprav = reset/odpojeni desky). Vraci seznam
    akci ``[('height', mm), ('rpm', rpm)]`` - nikdy nic jineho.
    """

    GAP_S = 3.0      # status mlci dele -> deska se resetovala / odpojila
    SETTLE_S = 2.0   # READY musi trvat, nez se obnovi (doznela HOME, latch zpravy)

    def __init__(self, store):
        self.store = store
        self.busy = False            # /mower_smach/busy (program sekani)
        self.power_on_cmd = None     # posledni /mower/set_power (None = nevime)
        self.armed = True            # cekame na prvni READY nove relace
        self.height_cmd_seen = False
        self.rpm_cmd_seen = False
        self.last_status_t = None
        self.ready_since = None
        self.last_result = None      # popis posledniho rozhodnuti (pro UI/log)
        self.last_status = None      # posledni status desky (None = jeste nemluvila)

    # --- vstupy -----------------------------------------------------------
    def _new_session(self, why):
        self.armed = True
        self.height_cmd_seen = False
        self.rpm_cmd_seen = False
        self.ready_since = None
        self.last_result = 'armed: {}'.format(why)

    def on_command(self, kind):
        """Kdokoli (program, joystick, UI, my sami) poslal vysku/RPM desce.

        Povel do vypnute, mlcici nebo prave startujici desky (OFF/INIT/zadny
        status) se nepocita: deska ho bud nedostane, nebo ho prepise HOME po
        resetu. Tim se neblokuje obnova po pristim zapnuti (napr. kdyz uzivatel
        nastavi vysku pri vypnute sekacce) ani latched zpravami pri reconnectu.
        """
        if self.last_status in (None, 'OFF', 'INIT', 'UNK'):
            return
        if kind == 'height':
            self.height_cmd_seen = True
        elif kind == 'rpm':
            self.rpm_cmd_seen = True

    def on_power_cmd(self, on):
        self.power_on_cmd = bool(on)

    def tick(self, t):
        if self.last_status_t is not None and t - self.last_status_t > self.GAP_S:
            self.last_status = None  # deska mlci (reset / odpojena)
            if not self.armed:
                self._new_session('status silent {:.1f}s'.format(t - self.last_status_t))

    def on_status(self, t, status, current_height, max_height, setpoint_rpm, moto_rpm):
        if self.last_status_t is not None and t - self.last_status_t > self.GAP_S:
            self._new_session('status gap {:.1f}s'.format(t - self.last_status_t))
        self.last_status_t = t
        self.last_status = status

        if status in ('OFF', 'INIT', 'UNK'):
            if not self.armed or self.height_cmd_seen or self.rpm_cmd_seen:
                self._new_session('status {}'.format(status))
            self.ready_since = None
            return []
        if not self.armed:
            return []
        if status == 'HOME':
            self.ready_since = None
            return []
        if status != 'READY':
            # RUN / CHANGE_HEIGHT / ERR / BLOCKED / TEMP: nekdo s deskou pracuje
            # nebo je porucha -> tahle relace se neobnovuje
            self.armed = False
            self.ready_since = None
            self.last_result = 'skipped: status {}'.format(status)
            return []

        if self.ready_since is None:
            self.ready_since = t
            return []
        if t - self.ready_since < self.SETTLE_S:
            return []

        # READY ustalene -> jedno rozhodnuti za relaci
        self.armed = False
        self.ready_since = None
        if self.busy:
            self.last_result = 'skipped: mowing program running'
            return []
        if self.power_on_cmd is False:
            self.last_result = 'skipped: mower is being switched off'
            return []

        actions = []
        notes = []
        h = self.store.cut_height
        try:
            cur = int(current_height)
            mx = int(max_height)
        except (TypeError, ValueError):
            cur, mx = -1, 0
        if h is None:
            notes.append('no saved height')
        elif self.height_cmd_seen:
            notes.append('height: explicit command wins')
        elif mx <= 0 or cur < mx:
            notes.append('height: blade not parked ({}/{})'.format(cur, mx))
        elif not valid_height(h, mx):
            notes.append('height: saved {} outside 20..{}'.format(h, mx))
        elif h != cur:
            actions.append(('height', int(h)))
            notes.append('height -> {}'.format(h))

        r = self.store.motor_rpm
        try:
            spin = int(moto_rpm)
            sp = int(setpoint_rpm)
        except (TypeError, ValueError):
            spin, sp = 1, -1
        if r is None:
            notes.append('no saved rpm')
        elif self.rpm_cmd_seen:
            notes.append('rpm: explicit command wins')
        elif spin != 0:
            notes.append('rpm: motor spinning')
        elif r != sp:
            actions.append(('rpm', int(r)))
            notes.append('rpm setpoint -> {}'.format(r))
        self.last_result = 'restore: ' + '; '.join(notes)
        return actions
