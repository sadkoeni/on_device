echo "Updating package list..."
sudo apt-get update || { echo "apt-get update failed. Exiting."; exit 1; }

echo "Installing Portaudio (dependency for pyaudio and sounddevice)"
sudo apt-get install -y portaudio19-dev || { echo "Failed to install portaudio19-dev. Exiting."; exit 1; }
sudo apt-get install -y build-essential python3-dev portaudio19-dev || {echo "Failed to install python3-dev. Exiiting."; exit 1; }
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
sudo cp btwifiset.service /etc/systemd/system/ || { echo "Failed to copy bt-wifi-config.service. Exiting."; exit 1; }
sudo cp gitpull.service /etc/systemd/system/ || { echo "Failed to copy gitpull.service. Exiting."; exit 1; }

echo "installing bt wifi setup"
sudo apt install -y python3 python3-pip bluetooth bluez python3-dbus || {echo "Failed to install btwifi setup dependencies. Exiting."; exit 1;}

cd /usr/local/
echo "downloading bt-wifi setup"
sudo git clone https://github.com/mkaczanowski/btwifiset.git || {echo "Failed to download btwifi setup git repo. Exiting."; exit 1;}

cd btwifiset
sudo pip3 install -r requirements.txt


echo "enabling services so they start on system boot"

sudo systemctl daemon-reload

sudo systemctl enable lightberry || { echo "Failed to enable lightberry. Exiting."; exit 1; }
sudo systemctl enable btwifiset.service|| { echo "Failed to enable bt-wifi-config. Exiting."; exit 1; }
sudo systemctl enable gitpull || { echo "Failed to enable gitpull. Exiting."; exit 1; }

sudo systemctl start btwifiset.service

cd ~/lb