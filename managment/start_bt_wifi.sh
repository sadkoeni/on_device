#!/bin/bash

# /home/lightberry-01/lb/start_bt_wifi.sh  <-- Path on Pi

# Get the directory where the script *resides*
SCRIPT_DIR="$( cd "$( dirname "${BASH_SOURCE[0]}" )" &> /dev/null && pwd )"
# Assume venv is in the WorkingDirectory set by systemd (should be the same as SCRIPT_DIR if placed correctly)
# If systemd WorkingDirectory is /home/lightberry-01/lb, this should work:
VENV_ACTIVATE="./wifienv/bin/activate" # Path to venv activate script relative to WorkingDir
PYTHON_SCRIPT="$SCRIPT_DIR/bluetooth_wifi_config.py"
SESSION_NAME="bluetooth-wifi-config"
LOG_FILE="$SCRIPT_DIR/bluetooth_wifi_config.log"

echo "Starting Bluetooth WiFi Config script in tmux session '$SESSION_NAME'..." | tee -a $LOG_FILE
echo "Working Directory: $(pwd)" | tee -a $LOG_FILE # Log WD for debugging
echo "Log file: $LOG_FILE" | tee -a $LOG_FILE
echo "Script path: $PYTHON_SCRIPT" | tee -a $LOG_FILE
echo "Venv path: $VENV_ACTIVATE" | tee -a $LOG_FILE

# Check if venv activate script exists
if [ ! -f "$VENV_ACTIVATE" ]; then
    echo "Error: Virtual environment activate script not found at $(pwd)/$VENV_ACTIVATE" | tee -a $LOG_FILE
    # Try alternate path relative to script location just in case
    ALT_VENV_ACTIVATE="$SCRIPT_DIR/wifienv/bin/activate"
    if [ -f "$ALT_VENV_ACTIVATE" ]; then
         echo "Warning: Found venv relative to script dir ($ALT_VENV_ACTIVATE), check systemd WorkingDirectory setting." | tee -a $LOG_FILE
         VENV_ACTIVATE="$ALT_VENV_ACTIVATE"
    else
        echo "Error: Also not found at $ALT_VENV_ACTIVATE" | tee -a $LOG_FILE
        exit 1
    fi
fi

# Check if python script exists
if [ ! -f "$PYTHON_SCRIPT" ]; then
    echo "Error: Python script not found at $PYTHON_SCRIPT" | tee -a $LOG_FILE
    exit 1
fi

# Check if tmux is installed
if ! command -v tmux &> /dev/null; then
    echo "Error: tmux is not installed. Please install it (sudo apt install tmux)." | tee -a $LOG_FILE
    exit 1
fi

# Kill existing session if it's running, to ensure a clean start
tmux kill-session -t $SESSION_NAME 2>/dev/null
sleep 1

# Create a new detached tmux session and run the python script within the activated venv
# Redirect stdout and stderr to the log file
tmux new-session -d -s $SESSION_NAME "bash -c 'echo Sourcing $VENV_ACTIVATE... >> \"$LOG_FILE\"; source \"$VENV_ACTIVATE\" && echo Running $PYTHON_SCRIPT... >> \"$LOG_FILE\" && python \"$PYTHON_SCRIPT\" >> \"$LOG_FILE\" 2>&1'"

# Check if the session started
# Give tmux a moment to actually start the process
sleep 2
if tmux has-session -t $SESSION_NAME 2>/dev/null; then
    # Check if the process inside tmux is actually running (may have exited immediately)
    if tmux list-panes -t $SESSION_NAME -F '#{pane_dead}' | grep -q '0'; then
        echo "Successfully started tmux session '$SESSION_NAME' with Python script running." | tee -a $LOG_FILE
        echo "To attach: tmux attach -t $SESSION_NAME" | tee -a $LOG_FILE
        echo "To view logs: tail -f \"$LOG_FILE\"" | tee -a $LOG_FILE
    else
        echo "Error: tmux session '$SESSION_NAME' started but the Python script may have exited immediately. Check logs." | tee -a $LOG_FILE
        # Attempt to capture the last few lines from the log if the script died
        if [ -f "$LOG_FILE" ]; then
             echo "Last lines from log file ($LOG_FILE):" | tee -a $LOG_FILE
             tail -n 10 "$LOG_FILE" | tee -a $LOG_FILE
        fi
        exit 1
    fi
else
    echo "Error: Failed to start tmux session '$SESSION_NAME'." | tee -a $LOG_FILE
    # Attempt to capture the last few lines from the log if the script died early
    if [ -f "$LOG_FILE" ]; then
         echo "Last lines from log file ($LOG_FILE):" | tee -a $LOG_FILE
         tail -n 10 "$LOG_FILE" | tee -a $LOG_FILE
    fi
    exit 1
fi

exit 0 