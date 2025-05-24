#!/usr/bin/env python3
import sounddevice as sd
from textwrap import shorten

def list_devices():
    """Print index, name and I/O options for every audio device."""
    for idx, dev in enumerate(sd.query_devices()):
        api = sd.query_hostapis(dev['hostapi'])['name']
        io  = ('I' if dev['max_input_channels']  else '-') +('O' if dev['max_output_channels'] else '-')
        print(f"{idx:2d} {io} {api:<10} {shorten(dev['name'], 60)}")


if __name__ == "__main__":
    list_devices()
