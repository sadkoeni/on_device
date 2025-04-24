echo "Installing Portaudio (dependency for pyaudio and sounddevice)"
sudo apt-get install -y portaudio19-dev
echo "isntalled successfully"
echo "creating virtual environment"
python -m venv venv
echo "installing python dependencies"
./venv/bin/python -m pip install -r requirements.txt
