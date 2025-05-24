import sounddevice as sd

def list_devices():
    """Print index, name and I/O options for every audio device."""
    for idx, dev in enumerate(sd.query_devices()):
        io = []
        if dev['max_input_channels'] > 0:
            io.append('input')
        if dev['max_output_channels'] > 0:
            io.append('output')
        print(f"{idx:2d}: {dev['name']}  ({', '.join(io) or 'no I/O'})")

if __name__ == "__main__":
    list_devices()
