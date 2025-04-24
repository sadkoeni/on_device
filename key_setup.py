import hid
import time
import sys

# !!! IMPORTANT: Replace 0xXXXX with your Trinkey's actual Product ID !!!
TRINKEY_PID = 0x80ff # Use the PID you found

ADAFRUIT_VID = 0x239a
device_path = None

print(f"Searching for HID device VID={ADAFRUIT_VID:#06x}, PID={TRINKEY_PID:#06x}...")

try:
    devices = hid.enumerate(ADAFRUIT_VID, TRINKEY_PID)
    if not devices:
        print("Device not found. Check PID or connection.")
        sys.exit(1)
    device_path = devices[0]["path"]
    print(f"Found device at path: {device_path.decode('utf-8')}")
except ImportError:
    print("hidapi library not found or failed to import. Install it: pip install hidapi")
    sys.exit(1)
except Exception as e:
    print(f"Error enumerating HID devices: {e}")
    sys.exit(1)

print("Opening device...")
try:
    d = hid.device()
    d.open_path(device_path)
    d.set_nonblocking(1) # Use non-blocking reads
    print("Device opened. Press the Trinkey key... (Ctrl+C to exit)")
    print("Listening...")

    last_report = None
    while True:
        report = d.read(64) # Read up to 64 bytes
        if report:
            # Only print if it's a new report compared to the last one
            if bytes(report) != last_report:
                print(f"Report: {list(report)}")
                last_report = bytes(report) # Store as bytes
        # Small sleep to prevent pegging the CPU
        time.sleep(0.01)

except OSError as e:
    # This is the error often seen when the device disconnects during read
    print(f"\nOSError (likely device disconnect): {e}")
except KeyboardInterrupt:
    print("\nExiting.")
except Exception as e:
    print(f"\nUnexpected Error: {e}") # Catch other potential errors
finally:
    if 'd' in locals() and d:
        d.close()
        print("Device closed.")
