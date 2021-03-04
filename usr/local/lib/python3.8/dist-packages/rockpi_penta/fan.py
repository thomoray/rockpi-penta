#!/usr/bin/env python3
import time
import mraa  # pylint: disable=import-error
import misc

pin13 = mraa.Pwm(13)
pin13.period_us(40)
pin13.enable(True)


def read_temp():
    with open('/sys/class/thermal/thermal_zone0/temp') as f:
        t = int(f.read().strip()) / 1000.0
    return t


def get_dc(cache={}):
    if misc.conf['run'].value == 0:
        return 0.999

    if time.time() - cache.get('time', 0) > 60:
        cache['time'] = time.time()
        cache['dc'] = misc.fan_temp2dc(read_temp())

    return cache['dc']


def change_dc(dc, cache={}):
    if dc != cache.get('dc'):
        cache['dc'] = dc
        pin13.write(dc)


def running():
    while True:
        change_dc(get_dc())
        time.sleep(0.1)
