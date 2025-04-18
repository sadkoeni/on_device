#!/usr/bin/env python3
import hid
import time
import sys
import logging

# Basic logging setup
logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s:CheckNewKey:%(message)s')
logger = logging.getLogger("CheckNewKey")

# --- Configuration ---
# !!! IMPORTANT: Replace these with the VID and PID of the device you want to check !!!
#     Find these using 'lsusb' in the terminal.
DEVICE_VID = 0x239a # Example: Adafruit VID
DEVICE_PID = 0x80ff # Example: PID from your latest dmesg
# --- End Configuration ---

device_path = None
logger.info(f"Searching for HID device VID={DEVICE_VID:#06x}, PID={DEVICE_PID:#06x}...")

try:
    devices = hid.enumerate(DEVICE_VID, DEVICE_PID)
    if not devices:
        logger.error("Device not found. Check VID/PID or connection.")
        sys.exit(1)
    # Use the first device found - might need refinement if multiple interfaces exist
    device_path = devices[0]["path"]
    logger.info(f"Found device at path: {device_path.decode('utf-8') if device_path else 'N/A'}")
except ImportError:
    logger.error("hidapi library not found or failed to import. Install it: pip install hidapi")
    sys.exit(1)
except Exception as e:
    logger.error(f"Error enumerating HID devices: {e}")
    sys.exit(1)

logger.info("Opening device to check reports...")
device = None
try:
    device = hid.device()
    device.open_path(device_path)
    device.set_nonblocking(1) # Use non-blocking reads
    logger.info("Device opened. Press the key on the device... (Ctrl+C to exit)")
    print("Listening...")

    last_report = None
    while True: # Loop indefinitely until Ctrl+C
        report = device.read(64) # Read up to 64 bytes
        if report:
            # Only print if it's a new report compared to the last one
            if bytes(report) != last_report:
                print(f"Report: {list(report)}")
                last_report = bytes(report) # Store as bytes
        # Small sleep to prevent pegging the CPU
        time.sleep(0.01)

except OSError as e:
    # Often permission error (use sudo?) or device disconnect
    logger.error(f"OSError: {e} - Check permissions (try sudo?) or device connection.")
    sys.exit(1)
except KeyboardInterrupt:
    print("Exiting.")
except Exception as e:
    logger.error(f"Unexpected Error: {e}", exc_info=True)
    sys.exit(1)
finally:
    if device:
        device.close()
        logger.info("Device closed.") 