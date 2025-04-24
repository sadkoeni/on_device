# /home/stephankoenigstorfer/lb/bluetooth_wifi_config.py

import socket
import subprocess
import sys
import threading
import time
from bluetooth import (
    BluetoothSocket,
    RFCOMM,
    PORT_ANY,
    SERIAL_PORT_PROFILE,
    advertise_service,
    stop_advertising,
)

# Configuration
SERVICE_NAME = "PiWiFiConfig"
SERVICE_UUID = "94f39d29-7d6d-437d-973b-fba39e49d4ee" # Generate a new UUID if preferred
COUNTRY_CODE = "GB"  # <<< IMPORTANT: Change this to your 2-letter ISO country code!
RESTART_DELAY_SECS = 10 # Delay before restarting the listener after an error/disconnect

def log(message):
    """Simple logger to print messages."""
    print(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {message}", flush=True)

def run_command(command_list):
    """Runs a shell command and returns True on success, False otherwise."""
    log(f"Running command: {' '.join(command_list)}")
    try:
        # Using check=True will raise CalledProcessError on non-zero exit codes
        # Capture output to prevent it from interfering with Bluetooth communication
        result = subprocess.run(
            command_list,
            check=True,
            capture_output=True,
            text=True,
            timeout=60 # Add a timeout
        )
        log(f"Command successful. stdout:\n{result.stdout}")
        if result.stderr:
             log(f"Command stderr:\n{result.stderr}")
        return True
    except subprocess.CalledProcessError as e:
        log(f"Command failed: {e}")
        log(f"Stderr: {e.stderr}")
        log(f"Stdout: {e.stdout}")
        return False
    except subprocess.TimeoutExpired:
        log(f"Command timed out: {' '.join(command_list)}")
        return False
    except Exception as e:
        log(f"Error running command: {e}")
        return False

def set_wifi_country(country_code):
    """Sets the WiFi regulatory domain."""
    return run_command(["sudo", "raspi-config", "nonint", "do_wifi_country", country_code])

def set_wifi_credentials(ssid, psk):
    """Sets the WiFi SSID and password."""
    # Attempt to bring interface up first, might help on some boots
    run_command(["sudo", "ifconfig", "wlan0", "up"])
    time.sleep(2) # Short delay after bringing interface up
    success = run_command(["sudo", "raspi-config", "nonint", "do_wifi_ssid_passphrase", ssid, psk])
    # Optionally, force a reconfigure or restart networking - might not always be needed
    # run_command(["sudo", "wpa_cli", "-i", "wlan0", "reconfigure"])
    return success

def handle_client(client_sock, client_info):
    """Handles communication with a connected Bluetooth client."""
    log(f"Accepted connection from {client_info}")
    try:
        client_sock.send("Welcome to Pi WiFi Configurator!\n".encode('utf-8'))

        # Set country code first (only needs doing once, but harmless to repeat)
        if not set_wifi_country(COUNTRY_CODE):
             client_sock.send(f"Failed to set WiFi country code to {COUNTRY_CODE}. Please check logs.\n".encode('utf-8'))
             # Proceed anyway, might still work depending on the issue

        # Get SSID
        client_sock.send("Enter WiFi SSID: ".encode('utf-8'))
        ssid_bytes = client_sock.recv(1024)
        if not ssid_bytes:
            log("Client disconnected before sending SSID.")
            return
        ssid = ssid_bytes.decode('utf-8').strip()
        log(f"Received SSID: {ssid}")

        # Get Password
        client_sock.send("Enter WiFi Password: ".encode('utf-8'))
        psk_bytes = client_sock.recv(1024)
        if not psk_bytes:
            log("Client disconnected before sending password.")
            return
        psk = psk_bytes.decode('utf-8').strip()
        # Avoid logging the password itself
        log(f"Received password (length: {len(psk)})")

        if not ssid:
            client_sock.send("Error: SSID cannot be empty.\n".encode('utf-8'))
            return

        client_sock.send(f"Attempting to connect to SSID: {ssid}...\n".encode('utf-8'))
        if set_wifi_credentials(ssid, psk):
            log("WiFi configuration updated successfully.")
            client_sock.send("Success! WiFi configured. The Pi should connect shortly.\n".encode('utf-8'))
            # Optionally trigger a reboot or network restart here if needed
            # run_command(["sudo", "reboot"])
        else:
            log("Failed to update WiFi configuration.")
            client_sock.send("Error: Failed to configure WiFi. Please check logs on the Pi.\n".encode('utf-8'))

    except OSError as e:
        log(f"Bluetooth communication error: {e}")
    except Exception as e:
        log(f"Unexpected error handling client: {e}")
    finally:
        log(f"Closing connection to {client_info}")
        client_sock.close()

def start_server():
    """Starts the Bluetooth RFCOMM server."""
    server_sock = None
    while True: # Keep trying to restart the server if it fails
        try:
            server_sock = BluetoothSocket(RFCOMM)
            server_sock.bind(("", PORT_ANY))
            server_sock.listen(1) # Listen for one connection at a time

            port = server_sock.getsockname()[1]

            log(f"Starting Bluetooth RFCOMM server on port {port}")

            # Advertise the service (simplified call)
            log("Attempting to advertise service...")
            advertise_service(server_sock, SERVICE_NAME,
                              service_id=SERVICE_UUID)
            log("Service advertising call completed.")

            log(f"Waiting for connection on RFCOMM channel {port} (UUID: {SERVICE_UUID})")

            # Accept connections indefinitely (or until script stops)
            # For simplicity, handle one client then wait for the next
            # A threaded approach could handle multiple simultaneous attempts, but is complex.
            while True:
                client_sock, client_info = server_sock.accept()
                # Handle client in a separate thread to allow server restart logic
                # but for simplicity here, handle sequentially. If handle_client blocks
                # indefinitely (it shouldn't), the server won't accept new connections.
                handle_client(client_sock, client_info)
                log("Client handled, waiting for next connection.")


        except OSError as e:
            log(f"Bluetooth server error: {e}. Check if bluetoothd is running and hci0 is up.")
        except Exception as e:
            log(f"Unexpected server error: {e}")
        finally:
            log("Stopping advertising and closing server socket.")
            if server_sock:
                try:
                    stop_advertising(server_sock)
                except Exception as ad_err:
                    log(f"Error stopping advertising: {ad_err}") # Might fail if already down
                try:
                    server_sock.close()
                except Exception as sock_err:
                    log(f"Error closing server socket: {sock_err}")
            log(f"Restarting server in {RESTART_DELAY_SECS} seconds...")
            time.sleep(RESTART_DELAY_SECS)


if __name__ == "__main__":
    # Ensure script runs with sudo privileges if needed for raspi-config,
    # but it's better to configure sudoers for the specific commands.
    # For now, assume the service running this will handle permissions,
    # or the user runs it with sudo.
    # We explicitly use 'sudo' in run_command now.

    # Make sure Bluetooth is usable
    log("Ensuring Bluetooth adapter is up...")
    run_command(["sudo", "hciconfig", "hci0", "up", "piscan"]) # Make discoverable and connectable

    start_server() 