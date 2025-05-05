echo "Updating package list..."
#sudo apt-get update || { echo "apt-get update failed. Exiting."; exit 1; }

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
amixer -D pulse sset Master unmute 100%
amixer sset Master unmute 100%
