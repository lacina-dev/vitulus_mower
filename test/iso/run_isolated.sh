#!/bin/bash
# Izolovany test node_mower_prefs: soukromy roscore :11399, falesna deska, zadny zivy ROS.
# Spusteni: nice -n 19 bash test/iso/run_isolated.sh   (nic nepublikuje do ziveho ROS)
set -u
D=$(mktemp -d /tmp/mower_prefs_iso.XXXX)
WT=$(cd "$(dirname "$0")/../.." && pwd)
source /opt/ros/noetic/setup.bash
source /home/vitulus/catkin_ws/devel/setup.bash
export ROS_MASTER_URI=http://127.0.0.1:11399
export ROS_IP=127.0.0.1
unset ROS_HOSTNAME
export PYTHONPATH=$WT/src:$PYTHONPATH
export ROS_HOME=$D/rosHome
rm -rf $D/prefs $D/board.log $D/rosHome; mkdir -p $D/prefs $D/rosHome
P() { echo "== $*"; }
roscore -p 11399 >$D/roscore.log 2>&1 &
RC=$!
sleep 4
python3 $WT/test/iso/mock_board.py $D/board.log >$D/board.out 2>&1 &
MB=$!
python3 $WT/nodes/node_mower_prefs __name:=node_mower_prefs __ns:=/mower _path:=$D/prefs/preferences.json >$D/prefs1.out 2>&1 &
NP=$!
sleep 3
pubonce() { rostopic pub -1 "$1" "$2" "$3" >/dev/null 2>&1; }
P "1) UI nastavi preferenci pri vypnute sekacce: vyska 40, rpm 2600"
pubonce /mower/pref/set_cut_height std_msgs/Int16 40
pubonce /mower/pref/set_motor_rpm std_msgs/Int16 2600
sleep 1; cat $D/prefs/preferences.json; echo
timeout 5 rostopic echo -n1 /mower/preferences
P "2) program-like zdroj (smach callerid) posle vysku 30 + park max 65 -> neulozit"
rosrun rostopic rostopic pub -1 /mower/set_cut_height std_msgs/Int16 30 __name:=node_mower_smach >/dev/null 2>&1
rosrun rostopic rostopic pub -1 /mower/set_cut_height std_msgs/Int16 65 __name:=node_mower_smach >/dev/null 2>&1
sleep 1; python3 -c "import json;d=json.load(open('$D/prefs/preferences.json'));print('height',d['cut_height'],'rpm',d['motor_rpm'])"
P "3) zapnuti napajeni (reset desky) -> obnova po READY"
pubonce /mower/set_power std_msgs/Bool true
sleep 16
timeout 5 rostopic echo -n1 /mower/status | egrep "status|setpoint|current_height"
P "4) restart node (stack restart) se sekackou READY na 40 -> nic nehybat"
kill -INT $NP; wait $NP 2>/dev/null
python3 $WT/nodes/node_mower_prefs __name:=node_mower_prefs __ns:=/mower _path:=$D/prefs/preferences.json >$D/prefs2.out 2>&1 &
NP=$!
sleep 6
P "5) joystick (ds4 callerid) zmeni vysku na 50 -> ulozit"
rosrun rostopic rostopic pub -1 /mower/set_cut_height std_msgs/Int16 50 __name:=node_ds4 __ns:=/ds4 >/dev/null 2>&1
sleep 2; python3 -c "import json;d=json.load(open('$D/prefs/preferences.json'));print('height',d['cut_height'],d.get('cut_height_source'))"
P "6) vypnout + zapnout s busy=True (program) -> neobnovovat"
rostopic pub -l /mower_smach/busy std_msgs/Bool true >/dev/null 2>&1 &
BZ=$!
sleep 2
pubonce /mower/set_power std_msgs/Bool false
sleep 2
pubonce /mower/set_power std_msgs/Bool true
sleep 16
kill $BZ 2>/dev/null
timeout 5 rostopic echo -n1 /mower/status | egrep "status|setpoint|current_height"
P "prikazy, ktere deska dostala:"
cat $D/board.log
P "motor_on prikazy od node_mower_prefs:"
grep motor_on $D/board.log | grep -c mower_prefs
P "log node:"
grep mower_prefs $D/prefs1.out $D/prefs2.out | sed 's/^.*\[mower_prefs\]/  /'
kill $NP $MB 2>/dev/null; sleep 1; kill $RC; wait 2>/dev/null
