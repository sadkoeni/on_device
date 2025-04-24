echo "Updating package list..."
sudo apt-get update || { echo "apt-get update failed. Exiting."; exit 1; }

echo "Installing Portaudio (dependency for pyaudio and sounddevice)"
sudo apt-get install -y portaudio19-dev || { echo "Failed to install portaudio19-dev. Exiting."; exit 1; }
echo "Installed successfully"
echo "creating virtual environment"
python -m venv venv
echo "installing python dependencies"
./venv/bin/python -m pip install -r requirements.txt

echo "renaming directory"
cd ..
mv on_device/ lb
cd lb 

echo "installing startup services"
sudo cp lightberry.service /etc/systemd/system/ || { echo "Failed to copy lightberry.service. Exiting."; exit 1; }
sudo cp managment/bt-wifi-config.service /etc/systemd/system/ || { echo "Failed to copy bt-wifi-config.service. Exiting."; exit 1; }
sudo cp gitpull.service /etc/systemd/system/ || { echo "Failed to copy gitpull.service. Exiting."; exit 1; }

echo "enabling services so they start on system boot"
sudo systemctl enable lightberry || { echo "Failed to enable lightberry. Exiting."; exit 1; }
sudo systemctl enable bt-wifi-config || { echo "Failed to enable bt-wifi-config. Exiting."; exit 1; }
sudo systemctl enable gitpull || { echo "Failed to enable gitpull. Exiting."; exit 1; }