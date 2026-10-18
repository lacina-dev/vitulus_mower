#!/usr/bin/env python3
"""Falesna deska sekacky pro soukromy roscore: chova se jako STM_MowerController
(status 2 Hz, set_power ON -> reset -> mlceni -> INIT -> HOME -> READY na max,
set_cut_height -> CHANGE_HEIGHT -> READY, set_motor_rpm meni jen setpoint,
set_motor_on -> RUN). Zaznamenava vsechny prijate povely do /tmp souboru."""
import json
import sys
import threading
import rospy
from std_msgs.msg import Bool, Int16
from vitulus_msgs.msg import Mower

LOG = sys.argv[1]
MAXH = 65
st = {'status': 'OFF', 'h': MAXH, 'sp': 2000, 'spin': 0, 'silent_until': 0.0,
      'target': None, 'home_until': 0.0, 'motor_on_cmds': 0}
lock = threading.Lock()


def log(kind, val, caller):
    with open(LOG, 'a') as f:
        f.write(json.dumps({'t': rospy.get_time(), 'kind': kind, 'val': val,
                            'caller': caller, 'status': st['status']}) + '\n')


def caller(m):
    return getattr(m, '_connection_header', {}).get('callerid', '')


def cb_h(m):
    with lock:
        log('height', m.data, caller(m))
        if st['status'] in ('READY', 'RUN', 'CHANGE_HEIGHT') and 20 <= m.data <= MAXH:
            st['target'] = m.data
            if st['status'] != 'RUN':
                st['status'] = 'CHANGE_HEIGHT'


def cb_r(m):
    with lock:
        log('rpm', m.data, caller(m))
        if 0 < m.data < 6001 and st['status'] not in ('OFF',):
            st['sp'] = m.data


def cb_on(m):
    with lock:
        log('motor_on', m.data, caller(m))
        if m.data:
            st['motor_on_cmds'] += 1
            st['status'] = 'RUN'
            st['spin'] = st['sp']


def cb_p(m):
    with lock:
        log('power', m.data, caller(m))
        now = rospy.get_time()
        if m.data and st['status'] == 'OFF':
            # reset desky: 5 s mlceni, pak INIT, HOME 3 s, READY na max, setpoint = EEPROM default
            st.update(status='INIT', silent_until=now + 5.0, home_until=now + 9.0,
                      sp=2000, spin=0, h=30, target=None)
        elif not m.data:
            st.update(status='OFF', spin=0, h=MAXH, target=None)


rospy.init_node('node_mower', anonymous=False)
pub = rospy.Publisher('/mower/status', Mower, queue_size=5)
rospy.Subscriber('/mower/set_cut_height', Int16, cb_h)
rospy.Subscriber('/mower/set_motor_rpm', Int16, cb_r)
rospy.Subscriber('/mower/set_motor_on', Bool, cb_on)
rospy.Subscriber('/mower/set_power', Bool, cb_p)
r = rospy.Rate(2)
while not rospy.is_shutdown():
    with lock:
        now = rospy.get_time()
        if now < st['silent_until']:
            r.sleep()
            continue
        if st['status'] == 'INIT' and st['home_until'] and now > st['home_until'] - 3.0:
            st['status'] = 'HOME'
        if st['status'] == 'HOME' and now > st['home_until']:
            st.update(status='READY', h=MAXH, home_until=0.0)
        if st['status'] == 'CHANGE_HEIGHT' and st['target'] is not None:
            st['h'] = st['target']
            st['target'] = None
            st['status'] = 'READY'
        m = Mower(status=st['status'], moto_dir='LEFT', moto_rpm=st['spin'],
                  setpoint_rpm=st['sp'], current_height=st['h'], max_height=MAXH, temp=25.0)
        m.header.stamp = rospy.Time.now()
    pub.publish(m)
    r.sleep()
