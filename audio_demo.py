#!/usr/bin/env python3
"""
Usage examples:
  python audio_demo.py --list
  python audio_demo.py --device 3          # by index
  python audio_demo.py --device usb        # by name, or falls back to PULSE_SINK
"""
import argparse, os, re, subprocess, sys, sounddevice as sd
from textwrap import shorten

def list_devices():
    for i, d in enumerate(sd.query_devices()):
        io  = ('I' if d['max_input_channels']  else '-') + \
              ('O' if d['max_output_channels'] else '-')
        api = sd.query_hostapis(d['hostapi'])['name']
        print(f"{i:2d} {io} {api:<10} {shorten(d['name'], 60)}")

def resolve(spec: str|None):
    if spec is None:
        return None                     # keep PortAudio’s default
    try:
        return int(spec)                # numeric index
    except ValueError:
        # try a substring match in PortAudio’s table
        for i, d in enumerate(sd.query_devices()):
            if spec.lower() in d['name'].lower():
                return i

        # last resort: tell Pulse to move *default* to the desired sink
        try:
            sinks = subprocess.check_output(
                    ['pactl', 'list', 'short', 'sinks'],
                    text=True).splitlines()
            for line in sinks:
                if re.search(spec, line, re.I):
                    os.environ['PULSE_SINK'] = line.split()[1]
                    return None          # continue to use PortAudio “default”
        except (FileNotFoundError, subprocess.CalledProcessError):
            pass
        sys.exit(f'No audio device or sink matches “{spec}”.')

if __name__ == '__main__':
    p = argparse.ArgumentParser()
    p.add_argument('--list', action='store_true')
    p.add_argument('--device', metavar='DEV')
    args = p.parse_args()

    if args.list:
        list_devices(); sys.exit()

    idx = resolve(args.device)
    if idx is not None:
        sd.default.device = (None, idx)     # (input, output)

    # demo: play a 440 Hz beep so you know where it lands
    import numpy as np
    fs = 48_000
    sd.play(0.1*np.sin(2*np.pi*440*np.arange(int(0.4*fs))/fs),
            fs, blocking=True)

