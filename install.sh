echo "Updating package list..."
sudo apt-get update || { echo "apt-get update failed. Exiting."; exit 1; }

sudo apt-get install -y vim || { echo "Failed to install vim"; exit 1; }
echo "Installing Portaudio (dependency for pyaudio and sounddevice)"
sudo apt-get install -y portaudio19-dev || { echo "Failed to install portaudio19-dev. Exiting."; exit 1; }
sudo apt-get install -y build-essential python3-dev || { echo "Failed to install python3-dev. Exiting."; exit 1; }
echo "Installed successfully"
echo "creating virtual environment"
python -m venv venv
echo "installing python dependencies"
./venv/bin/python -m pip install -r requirements.txt
echo "Downloading openWakeWord models..."
./venv/bin/python -c "import openwakeword; openwakeword.utils.download_models()" || { echo "Failed to download openWakeWord models. Exiting."; exit 1; }
./venv/bin/python setup.py build_ext --inplace || { echo "Failed to build c_resampler. Exiting."; exit 1; }

echo "renaming directory"
#check if current directory is called lb, otherwise rename
if [ "$(basename "$(pwd)")" != "lb" ]; then
    echo "Renaming directory to lb"
    mv "$(pwd)" "$(dirname "$(pwd)")/lb" || { echo "Failed to rename directory. Exiting."; exit 1; }
fi


echo "installing startup services"
sudo cp lightberry.service /etc/systemd/system/ || { echo "Failed to copy lightberry.service. Exiting."; exit 1; }
sudo cp btwifiset.service /etc/systemd/system/ || { echo "Failed to copy bt-wifi-config.service. Exiting."; exit 1; }
sudo cp gitpull.service /etc/systemd/system/ || { echo "Failed to copy gitpull.service. Exiting."; exit 1; }

echo "installing bt wifi setup"
sudo apt install -y python3 python3-pip bluetooth bluez python3-dbus || { echo "Failed to install btwifi setup dependencies. Exiting."; exit 1; }

echo "downloading bt-wifi setup"
curl  -L https://raw.githubusercontent.com/nksan/Rpi-SetWiFi-viaBluetooth/main/btwifisetInstall.sh | bash


echo "enabling services so they start on system boot"

sudo systemctl daemon-reload

sudo systemctl enable lightberry || { echo "Failed to enable lightberry. Exiting."; exit 1; }
sudo systemctl enable btwifiset.service|| { echo "Failed to enable bt-wifi-config. Exiting."; exit 1; }
sudo systemctl enable gitpull || { echo "Failed to enable gitpull. Exiting."; exit 1; }
sudo systemctl start btwifiset.service

curl -fsSL https://tailscale.com/install.sh | sh
sudo tailscale up
amixer -D pulse sset Master unmute 100%
amixer sset Master unmute 100%

###############################################################################
# >>>  AUDIO + SERVICE AUTO-CONFIG FOR lightberrydev  <<<                    #
###############################################################################

echo "----------  Enabling always-on audio for user lightberrydev ----------"

# 1. Install PipeWire (or PulseAudio fallback on very old images)
sudo apt-get install -y pipewire-audio wireplumber dbus-user-session || {
    echo "Failed to install PipeWire packages. Exiting."
    exit 1
}

# 2. Make sure the user can access sound devices
sudo usermod -aG audio lightberrydev

# 3. Keep the user's systemd instance running from boot
sudo loginctl enable-linger lightberrydev

# 4. Determine the UID (default to 1000 if 'id' fails for some reason)
LIGHT_UID=$(id -u lightberrydev 2>/dev/null || echo 1000)
echo "Using UID=${LIGHT_UID} for lightberrydev"

# 5. Enable PipeWire / Pulse for that user right now (non-fatal if the bus is not yet up)
sudo -u lightberrydev \
  XDG_RUNTIME_DIR=/run/user/${LIGHT_UID} \
  DBUS_SESSION_BUS_ADDRESS=unix:path=/run/user/${LIGHT_UID}/bus \
  systemctl --user enable --now pipewire wireplumber pipewire-pulse || true


# 6. Create or update the override that points the system unit at the user’s audio socket
sudo mkdir -p /etc/systemd/system/lightberry.service.d
sudo tee /etc/systemd/system/lightberry.service.d/override.conf >/dev/null <<EOF
[Unit]
After=network.target sound.target
Requires=sound.target

[Service]
User=lightberrydev
Group=audio
Environment=XDG_RUNTIME_DIR=/run/user/${LIGHT_UID}
Environment=DBUS_SESSION_BUS_ADDRESS=unix:path=/run/user/${LIGHT_UID}/bus
Environment=PULSE_SERVER=unix:/run/user/${LIGHT_UID}/pulse/native
EOF

# 7. Reload systemd and (re)start the service so the new env vars are picked up
sudo systemctl daemon-reload
sudo systemctl enable --now lightberry.service

echo "----------  Audio auto-configuration complete ----------"

