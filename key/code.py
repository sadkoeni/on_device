# Trinkey code.py to send F24 key on capacitive touch

import time
import board
import touchio
import usb_hid
from adafruit_hid.keyboard import Keyboard
from adafruit_hid.keycode import Keycode

# --- Configuration ---
KEYCODE_TO_SEND = Keycode.F24  # Send the F24 key
# -------------------

print("NeoKey Trinkey - HID F24 Sender")

# Set up capacitive touch input
touch_pad = board.TOUCH  # The touch pad pin is typically board.TOUCH
touch = touchio.TouchIn(touch_pad)

# Set up the keyboard object over USB HID
try:
    kbd = Keyboard(usb_hid.devices)
    print("Keyboard HID device ready.")
except Exception as e:
    print(f"Error setting up Keyboard HID: {e}")
    # Loop forever if HID setup fails, maybe blink LED?
    while True:
        time.sleep(1)

# Variables to track touch state
was_touched = False

# Main loop
while True:
    touched = touch.value # True if touched

    if touched and not was_touched:
        print("Touch detected - Sending F24 press")
        try:
            kbd.press(KEYCODE_TO_SEND)
        except Exception as e:
            print(f"Error sending key press: {e}")
        was_touched = True

    elif not touched and was_touched:
        print("Touch released - Sending F24 release")
        try:
            kbd.release(KEYCODE_TO_SEND)
        except Exception as e:
            print(f"Error sending key release: {e}")
        was_touched = False

    # Small delay to avoid excessive checking
    time.sleep(0.05)
