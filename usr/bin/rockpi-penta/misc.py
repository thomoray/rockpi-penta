#!/usr/bin/env python3
import re
import datetime
import sys
import time
import mraa  # pylint: disable=import-error
import shutil
import subprocess
import multiprocessing as mp
from configparser import ConfigParser
from collections import defaultdict, OrderedDict

cmds = {
    'blk': "lsblk | awk '{print $1}'",
    'up': "echo Uptime: `uptime | sed 's/.*up \\([^,]*\\), .*/\\1/'`",
    'temp': "cat /sys/class/thermal/thermal_zone0/temp",
    'ip': "hostname -I | awk '{printf \"IP %s\", $1}'",
    'cpu': "uptime | awk '{printf \"CPU Load: %.2f\", $(NF-2)}'",
    'men': "free -m | awk 'NR==2{printf \"Mem: %s/%sMB\", $3,$2}'",
    'disk': "df -h | awk '$NF==\"/\"{printf \"Disk: %d/%dGB %s\", $3,$2,$5}'"
}

lv2dc = OrderedDict({'lv3': 0, 'lv2': 0.25, 'lv1': 0.5, 'lv0': 0.75})


def set_mode(pin, mode=1):
    try:
        pin = mraa.Gpio(pin)
        pin.dir(mraa.DIR_OUT)
        pin.write(mode)
    except Exception as ex:
        print(ex)


def check_output(cmd):
    return subprocess.check_output(cmd, shell=True).decode().strip()


def check_call(cmd):
    return subprocess.check_call(cmd, shell=True)


def get_blk():
    conf['disk'] = [x for x in check_output(cmds['blk']).strip().split('\n') if x.startswith('sd')]


def get_info(s):
    return check_output(cmds[s])


def get_cpu_temp():
    t = float(get_info('temp')) / 1000
    if conf['oled']['f-temp']:
        temp = "CPU Temp: {:.0f}°F".format(t * 1.8 + 32)
    else:
        temp = "CPU Temp: {:.1f}°C".format(t)
    return temp


def read_conf():
    conf = defaultdict(dict)

    try:
        cfg = ConfigParser()
        cfg.read('/etc/rockpi-penta.conf')
        # fan
        conf['fan']['lv0'] = cfg.getfloat('fan', 'lv0')
        conf['fan']['lv1'] = cfg.getfloat('fan', 'lv1')
        conf['fan']['lv2'] = cfg.getfloat('fan', 'lv2')
        conf['fan']['lv3'] = cfg.getfloat('fan', 'lv3')
        conf['fan']['linear'] = cfg.getboolean('fan', 'linear', fallback=False)
        conf['fan']['silentmaxlv'] = float(cfg.get('fan', 'silentmaxlv', fallback='0.4').replace('%', 'e-2'))
        conf['fan']['silentstart'] = datetime.datetime.strptime(cfg.get('fan', 'silentstart', fallback='22:00'), '%H:%M').time()
        conf['fan']['silentend'] = datetime.datetime.strptime(cfg.get('fan', 'silentend', fallback='10:00'), '%H:%M').time()
        # key
        conf['key']['click'] = cfg.get('key', 'click')
        conf['key']['twice'] = cfg.get('key', 'twice')
        conf['key']['press'] = cfg.get('key', 'press')
        # time
        conf['time']['twice'] = cfg.getfloat('time', 'twice')
        conf['time']['press'] = cfg.getfloat('time', 'press')
        # other
        conf['slider']['auto'] = cfg.getboolean('slider', 'auto')
        conf['slider']['time'] = cfg.getfloat('slider', 'time')
        conf['oled']['rotate'] = cfg.getboolean('oled', 'rotate')
        conf['oled']['f-temp'] = cfg.getboolean('oled', 'f-temp')
    except Exception:
        # fan
        conf['fan']['lv0'] = 35
        conf['fan']['lv1'] = 40
        conf['fan']['lv2'] = 45
        conf['fan']['lv3'] = 50
        # key
        conf['key']['click'] = 'slider'
        conf['key']['twice'] = 'switch'
        conf['key']['press'] = 'none'
        # time
        conf['time']['twice'] = 0.7  # second
        conf['time']['press'] = 1.8
        # other
        conf['slider']['auto'] = True
        conf['slider']['time'] = 10  # second
        conf['oled']['rotate'] = False
        conf['oled']['f-temp'] = False

    return conf


def read_key(pattern, size):
    s = ''
    pin11 = mraa.Gpio(11)
    pin11.dir(mraa.DIR_IN)

    while True:
        s = s[-size:] + str(pin11.read())
        for t, p in pattern.items():
            if p.match(s):
                return t
        time.sleep(0.1)


def watch_key(q=None):
    size = int(conf['time']['press'] * 10)
    wait = int(conf['time']['twice'] * 10)
    pattern = {
        'click': re.compile(r'1+0+1{%d,}' % wait),
        'twice': re.compile(r'1+0+1+0+1{3,}'),
        'press': re.compile(r'1+0{%d,}' % size),
    }

    while True:
        q.put(read_key(pattern, size))


def get_disk_info(cache={}):
    if not cache.get('time') or time.time() - cache['time'] > 30:
        info = {}
        cmd = "df -h | awk '$NF==\"/\"{printf \"%s\", $5}'"
        info['root'] = check_output(cmd)
        for x in conf['disk']:
            cmd = "df -Bg | awk '$1==\"/dev/{}\" {{printf \"%s\", $5}}'".format(x)
            info[x] = check_output(cmd)
        cache['info'] = list(zip(*info.items()))
        cache['time'] = time.time()

    return cache['info']


def slider_next(pages):
    conf['idx'].value += 1
    return pages[conf['idx'].value % len(pages)]


def slider_sleep():
    time.sleep(conf['slider']['time'])


def fan_temp2dc(t):
    base_temp = conf['fan']['lv0']
    if t <= base_temp:
        return 0.999

    if time_in_range(
        conf['fan']['silentstart'],
        conf['fan']['silentend'],
        datetime.datetime.now().time(),
    ):
        min_dc = max(min(1 - conf['fan']['silentmaxlv'], 0.999), 0)
    else:
        min_dc = 0

    if conf['fan']['linear']:
        lv0 = lv2dc['lv0']
        lv3 = lv2dc['lv3']
        denominator = conf['fan']['lv3'] - base_temp
        slope = (lv3 - lv0) / denominator if denominator > 0 else -0.05
        dc = min(lv0, max(slope * (t - base_temp) + lv0, lv3))  # bound the speed
    else:
        for lv, dc in lv2dc.items():
            if t >= conf['fan'][lv]:
                break

    return min(max(dc, min_dc), 0.999)

def time_in_range(start, end, x):
    """Return true if x is in the range [start, end]"""
    # from https://stackoverflow.com/a/10748024
    if start <= end:
        return start <= x <= end
    else:
        return start <= x or x <= end

def fan_switch():
    conf['run'].value = not(conf['run'].value)


def get_func(key):
    return conf['key'].get(key, 'none')


def open_pwm_i2c():
    def replace(filename, raw_str, new_str):
        with open(filename, 'r') as f:
            content = f.read()

        if raw_str in content:
            shutil.move(filename, filename + '.bak')
            content = content.replace(raw_str, new_str)

            with open(filename, 'w') as f:
                f.write(content)

    replace('/boot/hw_intfc.conf', 'intfc:pwm0=off', 'intfc:pwm0=on')
    replace('/boot/hw_intfc.conf', 'intfc:pwm1=off', 'intfc:pwm1=on')
    replace('/boot/hw_intfc.conf', 'intfc:i2c7=off', 'intfc:i2c7=on')


conf = {'disk': [], 'idx': mp.Value('d', -1), 'run': mp.Value('d', 1)}
conf.update(read_conf())


if __name__ == '__main__':
    if sys.argv[-1] == 'open_pwm_i2c':
        open_pwm_i2c()
