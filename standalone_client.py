import logging
import os
import platform # Added for platform detection
from typing import Optional, Callable, Union, AsyncGenerator
import numpy as np
import time # Added time
import json # Added json
import uuid # Added for device ID
import os.path # Added for device ID file path
import aiohttp # Added for async HTTP requests
# Conditional import for pynput later
import hid # Added for HID device communication
import threading # Added for background listener thread
# from dotenv import load_dotenv # For API keys - No longer needed for token/room
from datetime import datetime # Added import
import sys # Import sys for stderr redirection
from abc import ABC, abstractmethod # Added for Abstract Base Class
import enum # Added for Enums
# --- Add necessary imports ---
import queue # Standard library queue for thread interaction
from openwakeword.model import Model
import resampy # Import for resampling (will be used later)
# --- End Add necessary imports ---

from livekit import rtc
from livekit import api

import asyncio
import time
import os
from typing import Optional, TYPE_CHECKING
import logging
import pyaudio
import sounddevice as sd # Import sounddevice

from interfaces import AudioInputInterface, AudioOutputInterface, IOHandlerInterface, InputProcessorInterface

from constants import LIVEKIT_SAMPLE_RATE, PYAUDIO_CHUNK_SIZE, PYAUDIO_CHANNELS, PYAUDIO_FORMAT, LIVEKIT_CHANNELS

# --- Enums for State Machine and Triggers ---
class AppState(enum.Enum):
    IDLE = 1
    CONNECTING = 2
    ACTIVE = 3
    PAUSED = 4
    STOPPING = 5

class TriggerEvent(enum.Enum):
    FIRST_PRESS = 1
    SUBSEQUENT_PRESS = 2
    DOUBLE_PRESS = 3
    AUTO_PAUSE = 4
    UNEXPECTED_DISCONNECT = 5 # New event for disconnects
# --- End Enums ---


ASSISTANT_IDENTITY = os.getenv("ASSISTANT_IDENTITY", "assistant")
ASSISTANT_ROOM_NAME = os.getenv("LIVEKIT_ROOM_NAME", "lightberry")
DEVICE_ID = os.getenv("DEVICE_ID", "rechavpPa8YyWv7Zn")
#LIVEKIT_ROOM_NAME="lightberry"
LIVEKIT_URL="wss://lb-ub8o0q4v.livekit.cloud"
TIMING_LOG_DIR="timing_logs"
# --- HID Device Configuration ---
ADAFRUIT_VID = 0x239A
# !!! IMPORTANT: Replace 0xXXXX with your NeoKey Trinkey's actual Product ID (PID) !!!
TRINKEY_PID = 0x8100 # PID when HID keyboard is active
# !!! IMPORTANT: Replace this with the exact HID report byte sequence your Trinkey sends !!!
# Example: F24 key (0x3d) with no modifiers in an 8-byte report
TRINKEY_KEYCODE = bytes([1, 0, 0, 115, 0, 0, 0, 0, 0])
# --- End HID Device Configuration ---

# --- Trigger Configuration ---
DOUBLE_PRESS_TIMEOUT = 0.5 # Seconds to detect double press
# --- End Trigger Configuration ---


logger = logging.getLogger("LightberryLocalClient")
logger.info(f"Using standard audio sample rate: {LIVEKIT_SAMPLE_RATE}Hz") # Use standard rate
# set logger level to info
logger.setLevel(logging.INFO)

AUTH_API_URL = "https://lightberry.vercel.app/api/authenticate/{}" # Placeholder for device ID

def get_or_create_device_id() -> str:
    """Gets the device ID from a local file or creates a new one."""
    return DEVICE_ID

async def get_credentials(device_id: str, username: str) -> tuple[Optional[str], Optional[str]]:
    """Fetches LiveKit token and room name from the authentication API, with fallback."""
    url = AUTH_API_URL.format(device_id)
    payload = {"username": username, "x-device-api-key": username}
    logger.info(f"Attempting to fetch credentials from {url} for username '{username}'")

    # --- Define Fallback Credentials ---
    fallback_room_name = "lightberry"
    fallback_token = "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJuYW1lIjoiTGlnaHRiZXJyeSBBc3Npc3RhbnQiLCJ2aWRlbyI6eyJyb29tSm9pbiI6dHJ1ZSwicm9vbSI6ImxpZ2h0YmVycnkiLCJjYW5QdWJsaXNoIjp0cnVlLCJjYW5TdWJzY3JpYmUiOnRydWUsImNhblB1Ymxpc2hEYXRhIjp0cnVlfSwic3ViIjoidGVzdGVyIiwiaXNzIjoiQVBJM0VucVFRbTNqZEFYIiwibmJmIjoxNzQ1Nzg4NTY5LCJleHAiOjE3NDU4MTAxNjl9.JDMdxWZ6Qb6X3H_gCFsHfVJOItgAL0q6EWYp1zv6uO8" # Replace with a real temp token if available
    # ---------------------------------

    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(url, json=payload) as response:
                response.raise_for_status() # Raise an exception for bad status codes (4xx or 5xx)
                data = await response.json()

                if data.get("success"):
                    token = data.get("livekit_token")
                    room_name = data.get("room_name")
                    if token and room_name:
                        logger.info(f"Successfully retrieved token and room name: {room_name}")
                        return token, room_name
                    else:
                        logger.error("API response missing token or room name. Using fallback.")
                        return fallback_token, fallback_room_name
                else:
                    error_msg = data.get("error", "Unknown error")
                    logger.error(f"API request failed: {error_msg}. Using fallback.")
                    return fallback_token, fallback_room_name
    except aiohttp.ClientError as e:
        logger.error(f"HTTP request error during authentication: {e}. Using fallback.")
        return fallback_token, fallback_room_name
    except asyncio.TimeoutError:
        logger.error(f"Timeout connecting to authentication API at {url}. Using fallback.")
        return fallback_token, fallback_room_name
    except json.JSONDecodeError as e:
        logger.error(f"Failed to decode JSON response from authentication API: {e}. Using fallback.")
        return fallback_token, fallback_room_name
    except Exception as e:
        logger.error(f"An unexpected error occurred during credential fetching: {e}. Using fallback.", exc_info=True)
        return fallback_token, fallback_room_name

# --- Trigger Controller Base Class ---
class TriggerController(ABC):
    """Abstract base class for input trigger controllers."""
    def __init__(self, loop: asyncio.AbstractEventLoop, event_queue: asyncio.Queue):
        self._loop = loop
        self._event_queue = event_queue
        self._listener_thread: Optional[threading.Thread] = None
        self._stop_listening_flag = threading.Event()

    @abstractmethod
    def _listener_loop(self):
        """The actual listening logic running in the thread."""
        pass

    def start_listening(self):
        """Starts the listener thread."""
        if self._listener_thread is None or not self._listener_thread.is_alive():
            self._stop_listening_flag.clear()
            self._listener_thread = threading.Thread(target=self._listener_loop, daemon=True, name=self.__class__.__name__)
            self._listener_thread.start()
            logger.info(f"{self.__class__.__name__} listener started.")
        else:
            logger.warning(f"{self.__class__.__name__} listener already running.")

    def stop_listening(self):
        """Signals the listener thread to stop."""
        if self._listener_thread and self._listener_thread.is_alive():
            logger.info(f"Stopping {self.__class__.__name__} listener...")
            self._stop_listening_flag.set()
            # Optionally join, but daemon=True should allow exit
            # self._listener_thread.join(timeout=1.0)
            # if self._listener_thread.is_alive():
            #     logger.warning(f"{self.__class__.__name__} listener thread did not stop.")
            self._listener_thread = None # Clear reference
        else:
            logger.debug(f"{self.__class__.__name__} listener not running or already stopped.")

    def _emit_event(self, event: TriggerEvent):
        """Safely puts an event onto the asyncio queue from the listener thread."""
        try:
            # Use call_soon_threadsafe to interact with the asyncio event loop
            self._loop.call_soon_threadsafe(self._event_queue.put_nowait, event)
        except asyncio.QueueFull:
            logger.warning(f"{self.__class__.__name__}: Event queue full, discarding event {event.name}")
        except Exception as e:
            logger.error(f"{self.__class__.__name__}: Error emitting event: {e}", exc_info=True)

# --- End Trigger Controller Base Class ---

# --- HID Listener Functions (Moved inside TrinkeyTrigger) ---
# (find_trinkey_path and trinkey_listener_thread removed from global scope)
def find_trinkey_path(vid: int, pid: int) -> Optional[str]:
    """Finds the HID device path for the given VID and PID."""
    try:
        devices = hid.enumerate(vid, pid)
        if not devices:
            logger.warning(f"No HID device found with VID={vid:#06x}, PID={pid:#06x}")
            return None
        # Prefer devices with a usage page/id if possible, but take the first found for now
        # You might need more specific filtering if multiple interfaces exist
        device_path = devices[0]['path']
        logger.info(f"Found Trinkey HID device at path: {device_path.decode('utf-8') if device_path else 'N/A'}")
        # Ensure path is returned as string for hid.device().open_path
        return device_path.decode('utf-8') if device_path else None
    except ImportError:
        logger.error("hidapi library not found or failed to import. Install it: pip install hidapi")
        return None
    except Exception as e:
        logger.error(f"Error enumerating HID devices: {e}")
        return None
# --- End HID Listener Functions ---

# --- Trinkey Trigger Implementation ---
class TrinkeyTrigger(TriggerController):
    def __init__(self, loop: asyncio.AbstractEventLoop, event_queue: asyncio.Queue, vid: int, pid: int, keycode: bytes):
        super().__init__(loop, event_queue)
        self._vid = vid
        self._pid = pid
        self._keycode = keycode
        self._device_path: Optional[str] = None

    def _listener_loop(self):
        """Listens for HID reports from the Trinkey."""
        self._device_path = find_trinkey_path(self._vid, self._pid)
        if not self._device_path:
            logger.error("TrinkeyTrigger: Device not found. Listener thread exiting.")
            return

        trinkey_dev = None
        last_press_time = 0.0
        press_within_timeout = False # Flag to track if the last press was within the double-press window

        logger.info(f"TrinkeyTrigger: Listener thread started for device {self._device_path}")
        try:
            trinkey_dev = hid.device()
            # Pass the string path directly
            trinkey_dev.open_path(self._device_path.encode('utf-8')) # Encode path for hidapi
            trinkey_dev.set_nonblocking(1)
            logger.info("TrinkeyTrigger: Device opened successfully.")

            while not self._stop_listening_flag.is_set():
                report = trinkey_dev.read(64, timeout_ms=50) # Read with short timeout
                if report:
                    if bytes(report) == self._keycode:
                        current_time = time.time()
                        time_elapsed = current_time - last_press_time
                        logger.debug(f"TrinkeyTrigger: Keycode matched (Elapsed: {time_elapsed:.2f}s)")

                        if time_elapsed <= DOUBLE_PRESS_TIMEOUT and press_within_timeout:
                            logger.info("TrinkeyTrigger: Double press detected.")
                            self._emit_event(TriggerEvent.DOUBLE_PRESS)
                            press_within_timeout = False # Reset after double press
                        else:
                            # Treat as a single press (could be first or subsequent)
                            # The main loop state determines if it's FIRST or SUBSEQUENT
                            logger.info("TrinkeyTrigger: Single press detected.")
                            self._emit_event(TriggerEvent.FIRST_PRESS) # Emit generic press, main loop interprets
                            # Set flag to indicate this press could be the start of a double press
                            press_within_timeout = True

                        last_press_time = current_time
                    #else: logger.debug(f"Trinkey report: {list(report)}") # Optional log

                # Check timeout condition for resetting double-press flag
                if press_within_timeout and (time.time() - last_press_time > DOUBLE_PRESS_TIMEOUT):
                    press_within_timeout = False
                    logger.debug("TrinkeyTrigger: Double press window timed out.")

                # Small sleep moved out of read timeout
                # time.sleep(0.02) # Replaced by timeout_ms in read

        except OSError as e:
            logger.error(f"TrinkeyTrigger: OSError: {e} - Check permissions or device connection.")
        except Exception as e:
            logger.error(f"TrinkeyTrigger: Unexpected error: {e}", exc_info=True)
        finally:
            if trinkey_dev:
                trinkey_dev.close()
                logger.info("TrinkeyTrigger: Device closed.")
            logger.info("TrinkeyTrigger: Listener thread finished.")
# --- End Trinkey Trigger ---

# --- WakeWord Trigger Implementation ---
class WakeWordTrigger(TriggerController):
    def __init__(self,
                 loop: asyncio.AbstractEventLoop,
                 event_queue: asyncio.Queue,
                 io_handler: 'LocalIOHandler', # Reference to the IO Handler
                 activation_word: str = "hey_mycroft", # Default activation word
                 stop_word: str = "hey_jarvis",        # Default stop word (maps to double press)
                 threshold: float = 0.5):
        super().__init__(loop, event_queue)
        self.io_handler = io_handler
        self.logger = logger.getChild("WakeWordTrigger")
        self.audio_chunk_queue: Optional[queue.Queue] = None # Use standard queue for thread
        try:
            # Get the dedicated queue from the IO Handler
            # Note: IOHandler needs get_wakeword_audio_queue() defined first
            if hasattr(self.io_handler, 'get_wakeword_audio_queue'):
                 self.audio_chunk_queue = self.io_handler.get_wakeword_audio_queue()
                 self.logger.debug("Successfully retrieved wake word audio queue from IO Handler.")
            else:
                 # This will happen initially until IOHandler is refactored
                 self.logger.warning("IO Handler does not have 'get_wakeword_audio_queue' method yet. Queue set to None.")

        except Exception as e:
            self.logger.error(f"Failed to get wake word audio queue from IO Handler: {e}. Trigger will not function.", exc_info=True)
            # Listener loop will handle None queue gracefully.

        self.activation_word = activation_word
        self.stop_word = stop_word
        self.threshold = threshold
        self.oww_model: Optional[Model] = None
        self._last_detection_time: float = 0.0 # Initialize refractory timer

        try:
            self.logger.info("Initializing openWakeWord model...")
            # Using ONNX by default, assuming it's generally available/performant
            self.oww_model = Model(inference_framework='onnx')
            model_keys = list(self.oww_model.models.keys())
            self.logger.info(f"Loaded openWakeWord models: {model_keys}")

            # Validate and log provided words against loaded models
            if self.activation_word in self.oww_model.models:
                self.logger.info(f"Using '{self.activation_word}' model for ACTIVATION trigger.")
            else:
                self.logger.warning(f"Configured activation word '{self.activation_word}' NOT FOUND in loaded models: {model_keys}")

            if self.stop_word in self.oww_model.models:
                self.logger.info(f"Using '{self.stop_word}' model for STOP trigger (double press event).")
            else:
                 self.logger.warning(f"Configured stop word '{self.stop_word}' NOT FOUND in loaded models: {model_keys}")

        except ImportError:
             self.logger.error("openwakeword library not found. Install it: pip install openwakeword")
        except Exception as e:
            self.logger.error(f"Failed to initialize openWakeWord model: {e}", exc_info=True)

    def _listener_loop(self):
        """Listens for audio chunks and runs wake word detection."""
        if not self.oww_model or self.audio_chunk_queue is None:
            self.logger.error("WakeWordTrigger cannot start listener: Model or audio queue not available.")
            return

        self.logger.info("WakeWordTrigger listener thread starting...")
        print(f"Listening for wake words: ACTIVATION='{self.activation_word}', STOP='{self.stop_word}'...")

        while not self._stop_listening_flag.is_set():
            try:
                # Get chunk tuple: (chunk_data: bytes, chunk_id: int, t_capture: float)
                chunk_tuple = self.audio_chunk_queue.get(timeout=0.5)
                chunk_data, chunk_id, t_capture = chunk_tuple

                # --- Perform Prediction --- #
                try:
                    # Convert raw bytes (assuming int16 @ 48kHz) to numpy array
                    audio_int16_48k = np.frombuffer(chunk_data, dtype=np.int16)

                    # --- Downsample using resampy --- #
                    audio_int16_16k = np.array([], dtype=np.int16) # Default to empty
                    if audio_int16_48k.size > 0:
                        # Convert int16 to float32 for resampy
                        audio_float_48k = audio_int16_48k.astype(np.float32) / 32768.0
                        try:
                            # Resample (using kaiser_fast for potentially better performance)
                            audio_float_16k = resampy.resample(audio_float_48k, sr_orig=LIVEKIT_SAMPLE_RATE, sr_new=16000, filter='kaiser_fast')
                            # Convert back to int16 for openwakeword
                            audio_int16_16k = (audio_float_16k * 32768.0).astype(np.int16)
                        except Exception as resample_err:
                             self.logger.error(f"Error during resampling chunk {chunk_id}: {resample_err}")
                             # audio_int16_16k remains empty on error
                    # --------------------------------- #

                    # Predict using the downsampled audio chunk
                    audio_to_predict = audio_int16_16k # Use the resampled data
                    if audio_to_predict.size > 0:
                        self.oww_model.predict(audio_to_predict)
                        scores = self.oww_model.prediction_buffer
                        activation_score = scores.get(self.activation_word, [0.0])[-1]
                        stop_score = scores.get(self.stop_word, [0.0])[-1]

                        # --- Emit Events --- #
                        current_time = time.time()
                        if current_time - self._last_detection_time > 1.0: # 1-second refractory period
                            if activation_score > self.threshold:
                                self.logger.info(f"Activation word '{self.activation_word}' detected (Score: {activation_score:.2f}). Emitting FIRST_PRESS.")
                                self._emit_event(TriggerEvent.FIRST_PRESS)
                                self._last_detection_time = current_time # Update timestamp
                            elif stop_score > self.threshold:
                                self.logger.info(f"Stop word '{self.stop_word}' detected (Score: {stop_score:.2f}). Emitting DOUBLE_PRESS.")
                                self._emit_event(TriggerEvent.DOUBLE_PRESS)
                                self._last_detection_time = current_time # Update timestamp
                        # else: # Optional: Log if detection ignored due to refractory period
                        #     if activation_score > self.threshold or stop_score > self.threshold:
                        #          self.logger.debug("Wake word detected but ignored due to refractory period.")

                except Exception as pred_err:
                     log_chunk_id = chunk_id if 'chunk_id' in locals() else 'unknown'
                     self.logger.error(f"Error during wake word prediction for chunk {log_chunk_id}: {pred_err}", exc_info=True)

            except queue.Empty: continue
            except Exception as loop_err:
                self.logger.error(f"Error in WakeWord listener loop: {loop_err}", exc_info=True)
                time.sleep(0.1)

        self.logger.info("WakeWordTrigger listener thread finished.")
# --- End WakeWord Trigger ---

# --- Mac Keyboard Trigger Implementation ---
# Conditional import and implementation
if platform.system() == "Darwin":
    try:
        from pynput import keyboard
        _pynput_available = True
    except ImportError:
        logger.warning("pynput library not found. MacKeyboardTrigger disabled. Install it: pip install pynput")
        _pynput_available = False

    if _pynput_available:
        class MacKeyboardTrigger(TriggerController):
            def __init__(self, loop: asyncio.AbstractEventLoop, event_queue: asyncio.Queue):
                super().__init__(loop, event_queue)
                self._listener: Optional[keyboard.Listener] = None
                self._last_press_time = 0.0
                self._press_within_timeout = False

            def _on_press(self, key):
                # Check if stop flag is set before processing
                if self._stop_listening_flag.is_set():
                    self.logger.debug("MacKeyboardTrigger: Stop flag set, stopping listener.")
                    return False # Stop the listener

                try:
                    if key == keyboard.Key.space:
                        current_time = time.time()
                        time_elapsed = current_time - self._last_press_time
                        logger.debug(f"MacKeyboardTrigger: Spacebar pressed (Elapsed: {time_elapsed:.2f}s)")

                        if time_elapsed <= DOUBLE_PRESS_TIMEOUT and self._press_within_timeout:
                            logger.info("MacKeyboardTrigger: Double press detected.")
                            self._emit_event(TriggerEvent.DOUBLE_PRESS)
                            self._press_within_timeout = False # Reset
                        else:
                            logger.info("MacKeyboardTrigger: Single press detected.")
                            self._emit_event(TriggerEvent.FIRST_PRESS) # Emit generic press
                            self._press_within_timeout = True

                        self._last_press_time = current_time
                except Exception as e:
                    logger.error(f"MacKeyboardTrigger: Error in _on_press: {e}", exc_info=True)

                # Need a background task/timer to reset _press_within_timeout if no second press occurs
                # Simple approach: Reset it in the main listener loop check, or rely on next single press
                # Let's rely on the next single press detection for now.

            def _listener_loop(self):
                """Uses pynput listener context manager."""
                logger.info("MacKeyboardTrigger: Listener thread started.")
                try:
                    # Setup the listener in this thread
                    self._listener = keyboard.Listener(on_press=self._on_press)
                    with self._listener as l:
                        # Keep thread alive while listener is running and stop flag not set
                        while l.running and not self._stop_listening_flag.is_set():
                             # Check timeout condition for resetting double-press flag
                            if self._press_within_timeout and (time.time() - self._last_press_time > DOUBLE_PRESS_TIMEOUT):
                                self._press_within_timeout = False
                                logger.debug("MacKeyboardTrigger: Double press window timed out.")
                            time.sleep(0.1) # Prevent busy-waiting
                except Exception as e:
                    logger.error(f"MacKeyboardTrigger: Listener error: {e}", exc_info=True)
                finally:
                    # Listener stops automatically when 'with' block exits or _on_press returns False
                    self._listener = None
                    logger.info("MacKeyboardTrigger: Listener thread finished.")

            def stop_listening(self):
                """Stops the pynput listener."""
                super().stop_listening() # Sets the flag
                # pynput listener needs explicit stop sometimes
                if self._listener:
                    try:
                         # This might need to be called from the listener thread itself by returning False
                         # Or use Listener.stop()
                         self._listener.stop()
                    except Exception as e:
                         logger.error(f"Error stopping pynput listener: {e}")
                logger.info("MacKeyboardTrigger stop signaled.")


# --- End Mac Keyboard Trigger ---

# --- Define Placeholder if not Darwin or pynput fails ---
if platform.system() != "Darwin" or not _pynput_available:
    class MacKeyboardTrigger(TriggerController):
         def __init__(self, loop: asyncio.AbstractEventLoop, event_queue: asyncio.Queue):
              super().__init__(loop, event_queue)
              logger.error("MacKeyboardTrigger unavailable on this platform or pynput missing.")

         def _listener_loop(self):
             logger.error("MacKeyboardTrigger cannot listen.")
             pass # No-op

         def start_listening(self):
             logger.error("MacKeyboardTrigger cannot start listening.")

         def stop_listening(self):
             pass # No-op
# --- End Placeholder ---


# --- LocalIOHandler Definition (Replaces old AudioIOHandler) ---
class LocalIOHandler:
    """Manages interaction with owned audio input/output components and state.
    Handles audio distribution to WakeWord and LiveKit paths, and playback.
    Designed to be persistent and managed by the main application loop.
    """
    # Define constants for silence detection (can be moved later)
    SILENCE_THRESHOLD = 50  # Amplitude threshold (adjust based on mic sensitivity)

    def __init__(self,
                 loop: asyncio.AbstractEventLoop,
                 logger_instance: Optional[logging.Logger] = None,
                 trigger_event_queue: Optional[asyncio.Queue] = None):

        # Basic initialization
        self._loop = loop
        self.logger = (logger_instance or logger).getChild("LocalIOHandler")
        self._trigger_event_queue = trigger_event_queue

        # Instantiate owned audio components
        mic_frames_per_buffer = 480 # Align with 10ms LiveKit frames at 48kHz
        self.audio_input = PyAudioMicrophoneInput(
            loop=self._loop,
            sample_rate=LIVEKIT_SAMPLE_RATE,
            frames_per_buffer=mic_frames_per_buffer,
            logger_instance=self.logger.getChild("MicInput"),
            input_device_index=None # Use default input device
        )
        self.audio_output = SoundDeviceSpeakerOutput(
            loop=self._loop,
            sample_rate=LIVEKIT_SAMPLE_RATE,
            channels=1,
            logger_instance=self.logger.getChild("SpeakerOutput"),
            output_device_index=None # Use default output device
        )

        # Create internal queues
        self._wakeword_audio_queue = queue.Queue(maxsize=50) # Standard queue for WakeWord thread
        self._livekit_input_queue = asyncio.Queue(maxsize=200) # Async queue for Client coroutine
        self._speaker_output_queue = asyncio.Queue(maxsize=200) # Async queue for output transfer coroutine

        # Create internal events
        self._pause_input_event = asyncio.Event() # Gate for LiveKit input path
        self._pause_wakeword_event = asyncio.Event() # Gate for WakeWord input path
        self._stop_event = asyncio.Event() # Global stop for internal loops

        # Initialize event states
        self._pause_input_event.clear() # LiveKit path starts paused
        self._pause_wakeword_event.set() # Wake word path starts active

        # Initialize tasks
        self._input_dist_task: Optional[asyncio.Task] = None
        self._output_transfer_task: Optional[asyncio.Task] = None

        # Initialize internal state tracking
        self._assistant_speaking: bool = False
        self._playback_finished_event: Optional[asyncio.Event] = None
        try:
            # Attempt to get the event from the owned output component
            if hasattr(self.audio_output, 'get_playback_finished_event'):
                 event = self.audio_output.get_playback_finished_event()
                 if isinstance(event, asyncio.Event):
                     self._playback_finished_event = event
                     self.logger.debug("Successfully obtained playback finished event.")
                 else:
                     self.logger.warning("Playback finished event from audio_output is not an asyncio.Event.")
            else:
                self.logger.warning("Audio output component lacks get_playback_finished_event method.")
        except Exception as e:
            self.logger.error(f"Failed to get playback finished event: {e}", exc_info=True)

        self.logger.info("Persistent LocalIOHandler initialized.")

    # --- Public Interface for Client and Trigger ---
    def get_wakeword_audio_queue(self) -> queue.Queue:
        """Returns the thread-safe queue for the WakeWordTrigger."""
        return self._wakeword_audio_queue

    def get_livekit_input_queue(self) -> asyncio.Queue:
        """Returns the async queue for the LightberryLocalClient mic forwarder."""
        return self._livekit_input_queue

    async def play_audio_chunk(self, chunk: bytes):
        """Receives an audio chunk (e.g., from LiveKit client or trigger) for playback."""
        if self._stop_event.is_set():
            self.logger.debug("Handler stopping, discarding audio chunk for playback.")
            return
        try:
            await asyncio.wait_for(self._speaker_output_queue.put(chunk), timeout=0.2)
        except asyncio.TimeoutError:
             self.logger.warning("Timeout putting chunk into speaker output queue.")
        except asyncio.QueueFull:
             self.logger.warning("Speaker output queue full, dropping chunk.")
        except Exception as e:
            self.logger.error(f"Error putting chunk into speaker output queue: {e}")

    async def signal_client_connected(self):
        """Called by the client when it has successfully connected and is ready for audio."""
        self.logger.info("Client connected signal received. Enabling LiveKit input path.")
        await self._empty_queue(self._livekit_input_queue) # Clear any stale chunks
        self._pause_input_event.set() # Allow audio flow to client

    async def signal_client_disconnected(self):
        """Called by the client when it is disconnecting."""
        self.logger.info("Client disconnected signal received. Disabling LiveKit input path.")
        self._pause_input_event.clear() # Stop audio flow to client path
        await self._empty_queue(self._livekit_input_queue)

    async def request_livekit_pause(self):
        """Called by the client to request pausing the audio flow TO LiveKit."""
        self.logger.info("Request received to pause LiveKit input path.")
        self._pause_input_event.clear()

    async def request_livekit_resume(self):
        """Called by the client to request resuming the audio flow TO LiveKit."""
        self.logger.info("Request received to resume LiveKit input path.")
        await self._empty_queue(self._livekit_input_queue) # Clear potential stale input
        # Only resume if assistant is not speaking
        if not self._assistant_speaking:
             self._pause_input_event.set()
        else:
             self.logger.debug("Ignoring resume request as assistant is speaking.")

    # --- Lifecycle Management (Called by main) ---
    async def start(self):
        """Starts the IO Handler's components and internal processing loops."""
        # Prevent multiple starts
        if self._input_dist_task is not None and not self._input_dist_task.done():
            self.logger.warning("LocalIOHandler start called but input task seems already running.")
            return
        if self._output_transfer_task is not None and not self._output_transfer_task.done():
            self.logger.warning("LocalIOHandler start called but output task seems already running.")
            return
        self.logger.info("Starting LocalIOHandler...")
        self._stop_event.clear()
        self._pause_input_event.clear() # Client not connected yet
        self._pause_wakeword_event.set() # Wake word should be active
        self._assistant_speaking = False
        try:
            await self.audio_input.start()
            await self.audio_output.start()
            self._input_dist_task = asyncio.create_task(self._input_distribution_loop(), name="InputDistLoop")
            self._output_transfer_task = asyncio.create_task(self._transfer_output_loop(), name="OutputTransferLoop")
            self.logger.info("LocalIOHandler started successfully.")
        except Exception as e:
             self.logger.error(f"Error during LocalIOHandler start: {e}", exc_info=True)
             await self.stop()
             raise

    async def stop(self):
        """Stops the IO Handler's components and internal processing loops."""
        if self._stop_event.is_set():
            self.logger.debug("LocalIOHandler stop called but already stopping or stopped.")
            return
        self.logger.info("Stopping LocalIOHandler...")
        self._stop_event.set()
        tasks_to_cancel = []
        if self._input_dist_task and not self._input_dist_task.done(): tasks_to_cancel.append(self._input_dist_task)
        if self._output_transfer_task and not self._output_transfer_task.done():
             try: self._speaker_output_queue.put_nowait(None)
             except (asyncio.QueueFull, Exception): pass
             tasks_to_cancel.append(self._output_transfer_task)
        if tasks_to_cancel:
            self.logger.debug(f"Cancelling {len(tasks_to_cancel)} internal IO tasks...")
            for task in tasks_to_cancel: task.cancel()
            try: await asyncio.wait_for(asyncio.gather(*tasks_to_cancel, return_exceptions=True), timeout=5.0)
            except asyncio.TimeoutError: self.logger.warning("Timeout cancelling internal IO tasks.")
            except Exception as e: self.logger.error(f"Error cancelling internal IO tasks: {e}")
        self._input_dist_task = None
        self._output_transfer_task = None
        component_stop_tasks = []
        if hasattr(self, 'audio_input') and self.audio_input and hasattr(self.audio_input, 'stop'): component_stop_tasks.append(asyncio.create_task(self.audio_input.stop(), name="MicStop"))
        if hasattr(self, 'audio_output') and self.audio_output and hasattr(self.audio_output, 'stop'): component_stop_tasks.append(asyncio.create_task(self.audio_output.stop(), name="SpeakerStop"))
        if component_stop_tasks:
             self.logger.debug(f"Stopping {len(component_stop_tasks)} underlying audio components...")
             try: await asyncio.wait_for(asyncio.gather(*component_stop_tasks, return_exceptions=True), timeout=5.0)
             except asyncio.TimeoutError: self.logger.warning("Timeout stopping underlying audio components.")
             except Exception as e: self.logger.error(f"Error stopping underlying audio components: {e}")
        self.logger.info("LocalIOHandler stopped.")

    # --- Internal Processing Loops ---
    async def _input_distribution_loop(self):
        """Distributes audio from the single mic input to WakeWord and LiveKit queues."""
        self.logger.info("Input distribution loop started.")
        source_queue = None
        try:
            if hasattr(self.audio_input, 'get_audio_chunk_queue'):
                source_queue = self.audio_input.get_audio_chunk_queue()
            else:
                self.logger.error("InputDistLoop: audio_input missing get_audio_chunk_queue.") ; return
        except Exception as e:
             self.logger.error(f"InputDistLoop: Failed to get source queue: {e}. Exiting.", exc_info=True) ; return
        if source_queue is None: self.logger.error("InputDistLoop: Source queue is None. Exiting.") ; return

        while not self._stop_event.is_set():
            try:
                chunk_tuple = await asyncio.wait_for(source_queue.get(), timeout=0.5)
                source_queue.task_done()
                if self._pause_wakeword_event.is_set():
                    try: self._wakeword_audio_queue.put_nowait(chunk_tuple)
                    except queue.Full: self.logger.debug("WakeWord audio queue full, discarding chunk.")
                    except Exception as e: self.logger.error(f"Error putting chunk to WakeWord queue: {e}")
                if self._pause_input_event.is_set():
                    try: self._livekit_input_queue.put_nowait(chunk_tuple)
                    except asyncio.QueueFull: self.logger.warning("LiveKit input queue full, discarding chunk.")
                    except Exception as e: self.logger.error(f"Error putting chunk to LiveKit input queue: {e}")
            except asyncio.TimeoutError: continue
            except asyncio.CancelledError: self.logger.info("Input distribution loop cancelled."); break
            except Exception as e:
                self.logger.error(f"Error in input distribution loop: {e}", exc_info=True)
                await asyncio.sleep(0.1)
        self.logger.info("Input distribution loop finished.")

    async def _transfer_output_loop(self):
        """Reads from speaker queue, performs VAD, controls input pauses, sends to speaker."""
        self.logger.info("Output transfer loop started.")
        was_implicitly_paused = False
        while not self._stop_event.is_set():
            try:
                chunk = await asyncio.wait_for(self._speaker_output_queue.get(), timeout=0.2)
                self._speaker_output_queue.task_done()
                if chunk is None: continue
                energy = 0.0 ; is_currently_speech = False
                try:
                    if chunk:
                        audio_int16 = np.frombuffer(chunk, dtype=np.int16)
                        if audio_int16.size > 0: energy = np.mean(np.abs(audio_int16)) + 1e-9; is_currently_speech = energy > self.SILENCE_THRESHOLD
                except Exception as e: self.logger.error(f"OutputTransferLoop: Error calculating energy: {e}")

                if is_currently_speech and not self._assistant_speaking:
                    self.logger.debug(f"Output VAD: Start of speech detected (Energy: {energy:.2f}).")
                    self._assistant_speaking = True
                    await self.audio_output.signal_start_of_speech()
                    was_livekit_active_before_vad = self._pause_input_event.is_set()
                    self._pause_input_event.clear()
                    self._pause_wakeword_event.clear()
                    was_implicitly_paused = was_livekit_active_before_vad
                    self.logger.info("Assistant started speaking, pausing LiveKit & WakeWord input paths.")
                elif not is_currently_speech and self._assistant_speaking:
                    self.logger.debug("Output VAD: End of speech detected (silence). Waiting for playback.")
                    self._assistant_speaking = False
                    await self.audio_output.signal_end_of_speech()
                    await self._wait_for_output_completion()
                    self._pause_wakeword_event.set()
                    if was_implicitly_paused:
                        self._pause_input_event.set()
                        self.logger.info("Assistant finished speaking, resuming WakeWord and LiveKit input paths.")
                        await self._empty_queue(self._livekit_input_queue)
                    else:
                         self.logger.info("Assistant finished speaking, resuming WakeWord path. (LiveKit path remains paused).")
                    was_implicitly_paused = False
                if self._assistant_speaking:
                    if hasattr(self, 'audio_output') and self.audio_output and hasattr(self.audio_output, 'send_audio_chunk'):
                        await self.audio_output.send_audio_chunk(chunk)
                    else: self.logger.warning("Cannot send chunk to speaker, audio_output invalid.")
            except asyncio.TimeoutError:
                 if self._assistant_speaking:
                     self.logger.info("Output VAD: End of speech detected (timeout). Waiting for playback.")
                     self._assistant_speaking = False
                     await self.audio_output.signal_end_of_speech()
                     await self._wait_for_output_completion()
                     self._pause_wakeword_event.set()
                     if was_implicitly_paused:
                          self._pause_input_event.set()
                          self.logger.info("Assistant finished speaking (timeout), resuming WakeWord and LiveKit paths.")
                          await self._empty_queue(self._livekit_input_queue)
                     else:
                          self.logger.info("Assistant finished speaking (timeout), resuming WakeWord path. (LiveKit path remains paused).")
                     was_implicitly_paused = False
                 continue
            except asyncio.CancelledError: self.logger.info("Output transfer loop cancelled."); break
            except Exception as e:
                self.logger.error(f"Error in output transfer loop: {e}", exc_info=True)
                self._assistant_speaking = False; was_implicitly_paused = False
                if self._pause_wakeword_event: self._pause_wakeword_event.set()
                await asyncio.sleep(0.1)
        self.logger.info("Output transfer loop finished.")

    # --- Helper Methods ---
    async def _wait_for_output_completion(self):
        """Waits for the playback finished event from the owned audio output component."""
        if self._playback_finished_event:
            try:
                self.logger.debug("Waiting for playback finished event...")
                await asyncio.wait_for(self._playback_finished_event.wait(), timeout=60.0)
                self.logger.debug("Playback finished event received.")
                self._playback_finished_event.clear()
            except asyncio.TimeoutError: self.logger.warning("Timeout waiting for playback finished event.")
            except Exception as e: self.logger.error(f"Error waiting for playback finished event: {e}", exc_info=True)
        else:
            self.logger.warning("Playback finished event not available, cannot wait.")
            await asyncio.sleep(0.5)

    async def _empty_queue(self, q: Union[asyncio.Queue, queue.Queue]):
        """Helper to quickly empty an asyncio or standard queue."""
        emptied_count = 0
        try:
            if isinstance(q, asyncio.Queue):
                while not q.empty():
                    try: item = q.get_nowait(); q.task_done(); emptied_count += 1
                    except asyncio.QueueEmpty: break
            elif isinstance(q, queue.Queue):
                 while True:
                     try:
                         item = q.get_nowait()
                         try: q.task_done()
                         except ValueError: pass
                         emptied_count += 1
                     except queue.Empty: break
            else: self.logger.warning(f"_empty_queue called with unknown queue type: {type(q)}")
        except Exception as e: self.logger.error(f"Error emptying queue {q}: {e}", exc_info=True)
        if emptied_count > 0: self.logger.debug(f"Emptied {emptied_count} items from queue.")


# --- PyAudio Class (Keep Unchanged) ---
class PyAudioMicrophoneInput(AudioInputInterface):
    """AudioInputInterface that reads microphone audio using PyAudio and puts chunks onto a queue."""
    def __init__(self,
                 loop: asyncio.AbstractEventLoop,
                 sample_rate: int = LIVEKIT_SAMPLE_RATE,
                 logger_instance: Optional[Union[logging.Logger, 'logging.Logger']] = None,
                 frames_per_buffer: int = PYAUDIO_CHUNK_SIZE,
                 log_timing_cb: Optional[Callable] = None,
                 get_chunk_id_cb: Optional[Callable] = None,
                 input_device_index: Optional[int] = None): # Added input_device_index
        self._loop = loop
        self.logger = logger_instance or logger # Use passed logger or default
        # Use provided sample_rate or the default
        self._sample_rate = sample_rate
        self.logger.info(f"PyAudioMicrophoneInput initialized with sample rate: {self._sample_rate}Hz")
        self._audio_chunk_queue = asyncio.Queue(maxsize=500)
        self._audio_task: Optional[asyncio.Task] = None
        self._pyaudio_instance: Optional[pyaudio.PyAudio] = None
        self._pyaudio_stream: Optional[pyaudio.Stream] = None
        self._is_running = False
        self._frames_per_buffer = frames_per_buffer
        # Store callbacks
        self._log_timing = log_timing_cb
        self._get_chunk_id = get_chunk_id_cb
        self._input_device_index = input_device_index # Store the index
        if self._log_timing is None or self._get_chunk_id is None:
            self.logger.warning("Timing/Chunk ID callbacks not provided to PyAudioMicrophoneInput. Detailed timing logs will be incomplete.")
        if self._input_device_index is not None:
             self.logger.info(f"Using specific input device index: {self._input_device_index}")

    async def start(self):
        """Starts PyAudio processing."""
        if self._is_running:
            self.logger.warning("PyAudio input already running.")
            return
        self.logger.info("Starting PyAudio microphone input...")
        self._is_running = True # Set running flag early
        try:
            # Start the background task to read from PyAudio
            self._audio_task = asyncio.create_task(self._audio_processing_loop(), name="PyAudioReader")
            self.logger.info("PyAudio microphone input started successfully.")

        except Exception as e:
            self.logger.error(f"Error starting PyAudio input: {e}", exc_info=True)
            await self.stop() # Ensure cleanup on failed start
            raise # Re-raise the exception

    async def _audio_processing_loop(self):
        """Background task to read audio from PyAudio and feed the audio chunk queue."""
        self.logger.info("PyAudio _audio_processing_loop task started execution.")
        original_stderr = sys.stderr
        try:
            # Initialize PyAudio and stream within the task's execution context
            # --- Suppress stderr during PyAudio init ---
            self.logger.debug("Initializing PyAudio instance for input...")
            sys.stderr = open(os.devnull, 'w')
            self._pyaudio_instance = await self._loop.run_in_executor(None, pyaudio.PyAudio)
            sys.stderr = original_stderr # Restore stderr
            self.logger.debug("PyAudio instance for input initialized.")
            # --- End suppression ---

            self.logger.info(f"Opening PyAudio input stream with SR={self._sample_rate}, Channels={PYAUDIO_CHANNELS}, Chunk={self._frames_per_buffer}, Device={self._input_device_index or 'Default'}") # Log device index
            self._pyaudio_stream = await self._loop.run_in_executor(
                None,
                lambda: self._pyaudio_instance.open(
                    format=PYAUDIO_FORMAT,
                    channels=PYAUDIO_CHANNELS,
                    rate=self._sample_rate, # Use the configured sample rate
                    input=True,
                    frames_per_buffer=self._frames_per_buffer,
                    input_device_index=self._input_device_index # Pass the specific device index
                )
            )
            self.logger.info("PyAudio stream opened for input.")

            while self._is_running:
                #self.logger.debug("PyAudio loop running...") # DEBUG
                if not self._pyaudio_stream:
                    self.logger.warning("Audio loop: Stream missing, stopping.")
                    break
                try:
                    # Read chunk using the configured frames_per_buffer
                    #self.logger.debug("PyAudio loop: Attempting to read chunk...") # DEBUG
                    audio_chunk = await self._loop.run_in_executor(
                        None,
                        lambda: self._pyaudio_stream.read(self._frames_per_buffer, exception_on_overflow=False)
                    )
                    #self.logger.debug(f"PyAudio loop: Read chunk of size {len(audio_chunk)}") # DEBUG

                    if not self._is_running: break # Check running flag after blocking read

                    # --- Add timing and put chunk (with metadata) onto queue ---
                    chunk_id = -1 # Default value if ID callback fails
                    t_capture = time.time()
                    try:
                        if self._get_chunk_id:
                            chunk_id = self._get_chunk_id()
                        # else: # Callback missing - chunk_id remains -1
                        #     pass

                        # Log mic capture time if timing callback exists
                        if self._log_timing and chunk_id != -1:
                            self._log_timing('mic_capture', chunk_id, timestamp=t_capture)

                        # Put chunk, ID, and capture time into queue as a tuple
                        item_to_put = (audio_chunk, chunk_id, t_capture)

                        try:
                            # Use put_nowait for immediate check
                            self._audio_chunk_queue.put_nowait(item_to_put)
                        except asyncio.QueueFull:
                            # If the queue is full (likely because IOHandler input is paused),
                            # log that we are discarding the chunk.
                            # Downgraded from WARNING to DEBUG as this is expected when paused
                            self.logger.debug(f"PyAudio input queue full (likely paused), discarding chunk {chunk_id}.")
                            if self._log_timing and chunk_id != -1:
                                self._log_timing('discard_mic_q_full', chunk_id, timestamp=time.time())
                            # No need to wait or retry, just drop the chunk by doing nothing else.
                        # Removed TimeoutError handling as put_nowait raises QueueFull directly

                    except Exception as e:
                        # Catch potential errors from callbacks or queue logic
                        self.logger.error(f"Error in PyAudio put logic for chunk {chunk_id}: {e}", exc_info=True)

                    # -------------------------------------------------------------

                    # Small yield (consider if necessary with executor calls)
                    # await asyncio.sleep(0.001)

                except IOError as e:
                    # Log PyAudio errors, potentially break loop
                    self.logger.error(f"PyAudio read error: {e}")
                    if "Input overflowed" in str(e):
                        self.logger.warning("Input overflow detected, continuing...")
                        continue # Might be recoverable?
                    else:
                        break # Treat other IOErrors as fatal for this loop
                except Exception as e:
                    self.logger.error(f"Unexpected error in audio processing loop: {e}", exc_info=True)
                    await asyncio.sleep(0.1) # Avoid tight loop on unexpected errors

        except Exception as e:
            self.logger.error(f"Fatal error initializing PyAudio stream: {e}", exc_info=True)
            # Signal component failure? For now, just log.
        finally:
            sys.stderr = original_stderr # Ensure stderr is restored even on error
            self.logger.info("PyAudio processing loop finished. Cleaning up PyAudio resources...")
            # Cleanup PyAudio stream and instance
            stream = self._pyaudio_stream
            pa_instance = self._pyaudio_instance
            self._pyaudio_stream = None
            self._pyaudio_instance = None

            if stream:
                try:
                    # Use run_in_executor for blocking PyAudio calls
                    is_active = await self._loop.run_in_executor(None, stream.is_active)
                    if is_active:
                        await self._loop.run_in_executor(None, stream.stop_stream)
                    await self._loop.run_in_executor(None, stream.close)
                    self.logger.info("PyAudio stream closed.")
                except Exception as e:
                    self.logger.error(f"Error closing PyAudio stream: {e}")
            if pa_instance:
                try:
                    await self._loop.run_in_executor(None, pa_instance.terminate)
                    self.logger.info("PyAudio instance terminated.")
                except Exception as e:
                    self.logger.error(f"Error terminating PyAudio: {e}")

    def get_audio_chunk_queue(self) -> asyncio.Queue:
        """Returns the queue where raw audio chunks (with metadata) are placed."""
        return self._audio_chunk_queue

    async def stop(self):
        """Stops the audio processing loop."""
        if not self._is_running:
            return
        self.logger.info("Stopping PyAudio microphone input...")
        self._is_running = False # Signal tasks to stop

        # 1. Cancel and wait for the audio processing task
        if self._audio_task:
            task = self._audio_task
            self._audio_task = None
            if not task.done():
                self.logger.debug("Cancelling PyAudio processing task...")
                task.cancel()
                try:
                    await asyncio.wait_for(task, timeout=5.0)
                    self.logger.debug("PyAudio processing task finished.")
                except asyncio.TimeoutError:
                    self.logger.warning("Timeout waiting for PyAudio task to cancel.")
                except asyncio.CancelledError:
                    self.logger.debug("PyAudio task was cancelled successfully.")
                except Exception as e:
                    self.logger.error(f"Error waiting for PyAudio task: {e}")

        # Clear the queue on stop
        while not self._audio_chunk_queue.empty():
            try:
                item = self._audio_chunk_queue.get_nowait()
                self._audio_chunk_queue.task_done()
                # Log discarded item ID if timing enabled
                # if self._log_timing and isinstance(item, tuple) and len(item) > 1:
                #     self._log_timing('discard_mic_q_stop', item[1], timestamp=time.time())
            except asyncio.QueueEmpty:
                break
            except Exception as e:
                self.logger.error(f"Error clearing input audio queue during stop: {e}")
                break

        self.logger.info("PyAudio microphone input stopped.")

# --- SoundDevice Class (Unchanged) ---
class SoundDeviceSpeakerOutput(AudioOutputInterface): # Renamed class
    """AudioOutputInterface implementation using sounddevice for local playback."""
    def __init__(self,
                 loop: asyncio.AbstractEventLoop,
                 sample_rate: int = LIVEKIT_SAMPLE_RATE,
                 channels: int = 1,
                 logger_instance: Optional['Logger'] = None,
                 output_device_index: Optional[int] = None): # Kept index
        self._loop = loop
        self.logger = logger_instance or logger
        # Removed PyAudio instance
        self._stream: Optional[sd.OutputStream] = None # Changed stream type
        self._sample_rate = sample_rate
        self._channels = channels
        self._audio_queue = asyncio.Queue(maxsize=100) # Buffer size
        self._playback_task: Optional[asyncio.Task] = None
        self._is_speaking = False
        self._is_running = False # Add state tracking
        self._playback_finished_event = asyncio.Event() # Event to signal playback completion
        self._playback_finished_event.set() # Start in the "finished" state
        self._output_device_index = output_device_index # Store the index
        if self._output_device_index is not None:
             self.logger.info(f"Using specific sounddevice output device index: {self._output_device_index}")
        else:
             self.logger.info("Using default sounddevice output device.")

    async def start(self):
        """Opens the sounddevice stream and starts the playback task."""
        if self._is_running:
            self.logger.warning("Sounddevice speaker output already started.")
            return

        self.logger.info("Starting sounddevice speaker output...")
        self._playback_finished_event.clear() # Ensure event is clear on start

        # No retry loop needed for sounddevice open, it raises exceptions directly
        try:
            self.logger.debug(f"Attempting to open sounddevice output stream (Device={self._output_device_index or 'Default'})...")
            # Open sounddevice stream
            self._stream = sd.OutputStream(
                samplerate=self._sample_rate,
                channels=self._channels,
                device=self._output_device_index,
                dtype='float32', # Use float32 for sounddevice
                blocksize=PYAUDIO_CHUNK_SIZE # Keep using this blocksize for now
            )
            self._stream.start() # Start the stream
            self.logger.info(f"Sounddevice stream opened for output at {self._sample_rate}Hz.")
            self._is_running = True
            # Start the single playback task
            if self._playback_task is None or self._playback_task.done():
                self._playback_task = asyncio.create_task(self._play_audio(), name="SoundDevicePlayAudio") # Renamed task
                self.logger.debug("Sounddevice playback task started execution.")
            else:
                self.logger.warning("Sounddevice playback task already running during start?")

        except sd.PortAudioError as e:
            self.logger.error(f"PortAudio error opening sounddevice output stream: {e}")
            self._stream = None
            self._is_running = False
            raise RuntimeError("Failed to open sounddevice output stream due to PortAudio error.") from e
        except Exception as e:
            # Catch other potential exceptions during open
            self.logger.error(f"Failed to open sounddevice stream for output: {e}", exc_info=True)
            self._stream = None
            self._is_running = False
            raise RuntimeError("Failed to open sounddevice stream due to unexpected error.") from e

    async def _play_audio(self):
        """Internal task to read from queue and write to sounddevice stream."""
        self.logger.debug("Sounddevice playback worker started.")
        try:
            while True:
                try:
                    # Wait briefly for an item, allows checking _is_running periodically
                    chunk_bytes = await asyncio.wait_for(self._audio_queue.get(), timeout=0.5)
                except asyncio.TimeoutError:
                    # If stop() was called and queue is empty, exit.
                    if not self._is_running and self._audio_queue.empty():
                        self.logger.debug("Playback worker stopping: Not running and queue empty.")
                        break
                    # Otherwise, continue waiting for chunks or stop signal
                    continue

                # Check for the end-of-utterance sentinel
                if chunk_bytes is None:
                    self.logger.debug("Playback worker received None sentinel (end of utterance).")
                    self._audio_queue.task_done()
                    # Wait for stream to finish playing buffered data before setting event
                    # This requires knowing when the stream is truly idle.
                    # Sounddevice might not offer a direct async way.
                    # A simpler approach: set event after a short delay or assume write is blocking enough.
                    # Setting immediately after None might be too soon if internal buffers are large.
                    # For now, set immediately as before, but be aware of potential inaccuracy.
                    self.logger.info("Setting playback finished event after processing None sentinel.")
                    self._playback_finished_event.set()
                    continue

                # --- Process normal audio chunk ---
                if self._stream and self._is_running: # Check stream and running state
                    try:
                        # --- Convert bytes (int16) to numpy array (float32) ---
                        # Assuming chunk_bytes contains int16 PCM data
                        audio_int16 = np.frombuffer(chunk_bytes, dtype=np.int16)
                        # Convert int16 range (-32768 to 32767) to float32 range (-1.0 to 1.0)
                        audio_float32 = audio_int16.astype(np.float32) / 32768.0
                        # Ensure it's the correct shape (Nsamples, Nchannels)
                        if self._channels == 1:
                            audio_float32 = audio_float32.reshape(-1, 1)
                        else:
                             # Add logic here if stereo output is ever needed
                             self.logger.warning("Stereo output not fully handled in conversion yet.")
                             # Simple reshape assuming interleaved data:
                             audio_float32 = audio_float32.reshape(-1, self._channels)
                        # -------------------------------------------------------

                        # Run blocking write in executor with the float32 data
                        chunk_len = len(audio_float32) # Get number of samples for logging
                        # self.logger.debug(f"Playback worker: Attempting to write {chunk_len} float32 samples to stream...")
                        await self._loop.run_in_executor(None, self._stream.write, audio_float32)
                        # self.logger.debug(f"Playback worker: Successfully wrote {chunk_len} float32 samples to stream.")
                    except sd.PortAudioError as e:
                        # Handle specific errors like 'Stream is stopped' if needed
                        if self._is_running: # Avoid logging error if stop was intended
                             self.logger.error(f"Sounddevice write error: {e}")
                        # Decide whether to break or continue
                        break
                    except Exception as e:
                        self.logger.error(f"Unexpected error during Sounddevice write: {e}", exc_info=True)
                        break # Exit worker on unexpected error
                elif not self._is_running:
                     self.logger.warning("Playback worker: Output not running, discarding chunk.")
                else: # Stream is None but _is_running is true?
                     self.logger.error("Playback worker: Stream is None but worker is active.")
                     break

                self._audio_queue.task_done()

        except asyncio.CancelledError:
            self.logger.info("Sounddevice playback worker cancelled.")
        except Exception as e:
             self.logger.error(f"Error in playback worker: {e}", exc_info=True)
        finally:
             self.logger.info("Sounddevice playback worker finished.")
             # Ensure event is set when worker loop terminates, regardless of how
             self._playback_finished_event.set()

    async def send_audio_chunk(self, chunk: Optional[bytes]): # Allow None for sentinel
        # Check the unified playback task
        if self._is_running and self._playback_task and not self._playback_task.done():
            try:
                # Use put_nowait or bounded wait if queue full is critical
                await asyncio.wait_for(self._audio_queue.put(chunk), timeout=0.1)
            except asyncio.QueueFull:
                 self.logger.warning("Sounddevice output queue full, dropping chunk.")
            except asyncio.TimeoutError:
                 self.logger.warning("Timeout putting chunk into Sounddevice output queue.")
            except Exception as e:
                self.logger.error(f"Error sending audio chunk: {e}", exc_info=True)
        elif not self._is_running:
             self.logger.warning("Sounddevice output not running, cannot send chunk.")
        else: # Task exists but might be finishing/done
             self.logger.warning("Sounddevice playback task not active, cannot send chunk.")


    async def signal_start_of_speech(self):
        if not self._is_speaking:
            self.logger.info("🔊 Assistant speaking...")
            print("🔊 Assistant speaking...                                ", end="\n", flush=True)
            self._playback_finished_event.clear() # <<< Playback starting, clear event
            self._is_speaking = True

            # Ensure the playback worker is running - DO NOT RECREATE
            if self._playback_task is None or self._playback_task.done():
                # This indicates an issue - the task should have been started in start()
                self.logger.error("signal_start_of_speech called but playback task is not running!")
            # else: # Task is running, which is expected
            #     self.logger.debug("Playback worker task already running.")


    async def signal_end_of_speech(self):
        """Signals the intent to end speech and tells the worker to finish the queue."""
        if self._is_speaking:
            self.logger.info("Assistant finished speaking signal received.")
            print("🔊 Assistant finished speaking signal.                  ") # Indicate signal received
            self._is_speaking = False # Mark as not actively speaking anymore
            # Signal the playback worker to stop *after* processing remaining items
            # Put None sentinel in the queue
            try:
                # Use send_audio_chunk to handle queue logic and potential errors
                await self.send_audio_chunk(None)
                self.logger.debug("Enqueued None sentinel for end of utterance.")
            except Exception as e: # Catch errors from send_audio_chunk if any
                self.logger.error(f"Error enqueuing None sentinel: {e}")

            # Don't immediately set the finished event here. The worker sets it after processing None.
            # if self._audio_queue.empty():
            #      self.logger.debug("Audio queue empty after signaling end of speech, setting finished event.")
            #      self._playback_finished_event.set()


    async def stop(self):
        if not self._is_running:
            return
        self.logger.info("Stopping Sounddevice speaker output...")
        self._is_running = False # Signal stop

        # 1. Signal and wait for the single playback task to finish
        if self._playback_task and not self._playback_task.done():
            # Enqueue None to ensure the worker loop finishes if waiting on queue
            try:
                 self._audio_queue.put_nowait(None)
            except asyncio.QueueFull:
                 logger.warning("Could not enqueue None sentinel during stop.")
            # Now wait for the task
            try:
                await asyncio.wait_for(self._playback_task, timeout=5.0)
            except asyncio.TimeoutError:
                self.logger.warning("Timeout waiting for playback worker to finish, cancelling.")
                self._playback_task.cancel()
                # Wait briefly for cancellation to be processed
                try:
                    await asyncio.wait_for(self._playback_task, timeout=1.0)
                except (asyncio.TimeoutError, asyncio.CancelledError):
                    pass # Ignore further errors after cancellation attempt
            except asyncio.CancelledError:
                 self.logger.debug("Playback task already cancelled.")
            except Exception as e:
                 self.logger.error(f"Error stopping playback worker: {e}", exc_info=True)
                 if self._playback_task and not self._playback_task.done(): self._playback_task.cancel(); await asyncio.sleep(0.1)

        self._playback_task = None # Clear the task reference

        # 2. Close the sounddevice stream
        if self._stream:
            stream_to_close = self._stream
            self._stream = None # Prevent worker from using it after this point
            try:
                 # Sounddevice stream needs stop() and close() - run in executor
                 if not stream_to_close.stopped:
                     await self._loop.run_in_executor(None, stream_to_close.stop)
                 await self._loop.run_in_executor(None, stream_to_close.close)
                 self.logger.debug("Sounddevice output stream closed.")
            except Exception as e:
                self.logger.error(f"Error closing Sounddevice output stream: {e}")

        self.logger.info("Sounddevice speaker output stopped.")
        self._is_speaking = False # Ensure state reset
        self._playback_finished_event.set() # <<< Ensure event is set on stop

    @property
    def is_speaking(self) -> bool:
        """Return True if audio is currently being played."""
        # Simple check for now:
        return self._is_speaking

    def get_playback_finished_event(self) -> asyncio.Event:
         """Returns the event that signals playback completion."""
         return self._playback_finished_event


# --- LightberryLocalClient Modifications ---
class LightberryLocalClient:
    # Note: Client is now transient, created/destroyed per connection cycle.
    # It interacts with the persistent LocalIOHandler.
    def __init__(self, url: str, token: str, room_name: str,
                 loop: asyncio.AbstractEventLoop,
                 trigger_event_queue: asyncio.Queue,
                 io_handler: LocalIOHandler): # Accepts the persistent handler

        self.livekit_url = url
        self.livekit_token = token
        self.room_name = room_name
        self._loop = loop
        self.logger = logger.getChild("Client")
        self.io_handler = io_handler # Store reference to persistent handler
        self._trigger_event_queue = trigger_event_queue

        self._room: Optional[rtc.Room] = None
        self._is_connected = False
        self._is_paused = False

        # --- Timing Infrastructure ---
        self.timing_data = []
        self.chunk_id_counter = 0
        self.timing_log_file_path: Optional[str] = self._generate_timing_log_path()
        # --- End Timing Infrastructure ---

        # LiveKit specific members
        self._mic_audio_source: Optional[rtc.AudioSource] = None
        self._mic_track_pub: Optional[rtc.LocalTrackPublication] = None
        self._forward_mic_task: Optional[asyncio.Task] = None
        self._playback_tasks: dict[str, asyncio.Task] = {}

        self.logger.info(f"LightberryLocalClient initialized. Timing logs: '{self.timing_log_file_path}'")

    def _register_listeners(self):
        if not self._room: return
        self._room.on("track_subscribed", self._on_track_subscribed)
        self._room.on("disconnected", self._on_disconnected)
        self.logger.debug("Room event listeners registered.")

    async def connect(self):
        """Connects to the LiveKit room."""
        if self._is_connected: self.logger.warning("Already connected."); return
        self.logger.info(f"Connecting to {self.livekit_url} in room '{self.room_name}'...")
        try:
            self._room = rtc.Room(loop=self._loop)
            self._register_listeners()
            await self._room.connect(self.livekit_url, self.livekit_token)
            self._is_connected = True
            self.logger.info(f"Connected to LiveKit room: {self._room.name}")
        except Exception as e:
            self.logger.error(f"LiveKit connection failed: {e}", exc_info=True)
            self._room = None ; self._is_connected = False ; raise

    async def start(self):
        """Connects to LiveKit, signals IO Handler, starts processing tasks."""
        self.logger.info("Starting LightberryLocalClient...")
        if self._is_connected: self.logger.warning("Client already started."); return
        try:
            await self.connect()
            if self._is_connected:
                await self.io_handler.signal_client_connected()
                await self._create_and_publish_mic_track()
                if self._mic_audio_source:
                    self._is_paused = False
                    self._forward_mic_task = asyncio.create_task(self._forward_mic_to_livekit(), name="ForwardMicToLiveKit")
                    self.logger.info("Microphone forwarding task started.")
                else:
                    raise RuntimeError("Failed to create microphone audio source.")
                self.logger.info("Client started and processing tasks initiated.")
            else:
                 self.logger.error("Client start failed: room connection failed.")
                 # await self.stop() # Avoid potential recursion if stop fails
        except Exception as e:
             self.logger.error(f"Error during client start sequence: {e}", exc_info=True)
             # await self.stop() # Avoid potential recursion
             raise

    async def _perform_stop_operations(self):
        """Internal helper to perform stop actions in sequence."""
        # Cancel tasks
        tasks_to_cancel = [self._forward_mic_task] + list(self._playback_tasks.values())
        active_tasks = [task for task in tasks_to_cancel if task and not task.done()]
        if active_tasks:
             self.logger.debug(f"Cancelling {len(active_tasks)} client background tasks...")
             for task in active_tasks: task.cancel()
             try: await asyncio.gather(*active_tasks, return_exceptions=True)
             except Exception as e: self.logger.error(f"Error gathering cancelled tasks: {e}", exc_info=True)
        self._forward_mic_task = None
        self._playback_tasks.clear()
        # Unpublish track
        if self._mic_track_pub and self._room and self._room.local_participant:
            self.logger.debug(f"Unpublishing mic track {self._mic_track_pub.sid}...")
            try:
                if self._room.local_participant: await self._room.local_participant.unpublish_track(self._mic_track_pub.sid)
                self.logger.info("Microphone track unpublished.")
            except Exception as e: self.logger.error(f"Error unpublishing mic track: {e}", exc_info=True)
        self._mic_track_pub = None
        self._mic_audio_source = None
        # Disconnect from room
        room_to_disconnect = self._room
        self._room = None
        if room_to_disconnect and self._is_connected: # Check flag again before disconnect
            self.logger.info("Disconnecting from LiveKit room...")
            try: await room_to_disconnect.disconnect()
            except Exception as e: self.logger.error(f"Error during room disconnect: {e}", exc_info=True)
        # Signal Handler AFTER disconnect
        try: await self.io_handler.signal_client_disconnected()
        except Exception as e: self.logger.error(f"Error signaling IO Handler of client disconnect: {e}")
        # Save Timing Data
        try: self.save_timing_data()
        except Exception as e: self.logger.error(f"Error saving client timing data during stop: {e}", exc_info=True)

    async def stop(self):
        """Stops client tasks, disconnects, signals IO Handler."""
        if not self._is_connected and not self._forward_mic_task and not self._playback_tasks:
             self.logger.debug("Client stop called but appears already stopped.")
             return
        self.logger.info("Stopping LightberryLocalClient...")
        # --- Set flags early in case stop operations fail --- #
        was_connected = self._is_connected # Remember initial state
        self._is_connected = False
        self._is_paused = False
        # --------------------------------------------------- #
        try:
            await self._perform_stop_operations()
            self.logger.info("LightberryLocalClient stop sequence completed.")
        except Exception as e:
             self.logger.error(f"Unexpected error during client stop operations: {e}", exc_info=True)
             # Logged error, main loop finally block will handle handler shutdown

    # --- Pause/Resume Methods --- #
    async def pause(self):
        if self._is_paused: self.logger.debug("Client already paused."); return
        # Only allow pause if connected?
        # if not self._is_connected: self.logger.warning("Cannot pause, not connected."); return
        self.logger.info("Requesting IO Handler to pause LiveKit input.")
        self._is_paused = True
        try: await self.io_handler.request_livekit_pause()
        except Exception as e: self.logger.error(f"Error requesting pause from IO Handler: {e}")
        self.logger.info("Client set to Paused state.")

    async def resume(self):
        if not self._is_paused: self.logger.debug("Client is not paused."); return
        # if not self._is_connected: self.logger.warning("Cannot resume, not connected."); return
        self.logger.info("Requesting IO Handler to resume LiveKit input.")
        self._is_paused = False
        try: await self.io_handler.request_livekit_resume()
        except Exception as e: self.logger.error(f"Error requesting resume from IO Handler: {e}")
        self.logger.info("Client set to Resumed state.")

    async def _create_and_publish_mic_track(self):
        if not self._room or not self._room.local_participant: self.logger.error("Cannot create mic track: Not connected/no participant."); return
        if self._mic_audio_source: self.logger.warning("Mic audio source already exists."); return
        try:
            self.logger.debug(f"Creating mic AudioSource (Rate: {LIVEKIT_SAMPLE_RATE}Hz)")
            self._mic_audio_source = rtc.AudioSource(LIVEKIT_SAMPLE_RATE, LIVEKIT_CHANNELS)
            track = rtc.LocalAudioTrack.create_audio_track("mic-audio", self._mic_audio_source)
            options = rtc.TrackPublishOptions(source=rtc.TrackSource.SOURCE_MICROPHONE)
            self.logger.info("Publishing mic audio track...")
            self._mic_track_pub = await self._room.local_participant.publish_track(track, options)
            self.logger.info(f"Mic track published (SID: {self._mic_track_pub.sid}).")
        except Exception as e:
            self.logger.error(f"Failed to create/publish mic track: {e}", exc_info=True)
            self._mic_audio_source = None

    async def _forward_mic_to_livekit(self):
        """Task reads from IOHandler's LiveKit queue, pushes frames to LiveKit."""
        if not self._mic_audio_source: self.logger.error("Cannot forward mic: AudioSource missing."); return
        try:
            livekit_input_queue = self.io_handler.get_livekit_input_queue()
            expected_bytes_per_frame = 480 * LIVEKIT_CHANNELS * 2
            self.logger.info(f"Starting mic forwarding loop (Expecting {expected_bytes_per_frame} bytes)...")
            while self._is_connected:
                try:
                    chunk_data, chunk_id, t_capture = await asyncio.wait_for(livekit_input_queue.get(), timeout=0.5)
                    t_get_lk_q = time.time()
                    livekit_input_queue.task_done()
                    # --- Client-side Timing --- #
                    # Note: t_capture is from the original mic read time
                    lk_q_wait = t_get_lk_q - t_capture # Includes IO handler distribution latency
                    self._log_timing('get_livekit_q', chunk_id, timestamp=t_get_lk_q, wait_time=lk_q_wait)
                    # ------------------------- #
                    if len(chunk_data) != expected_bytes_per_frame:
                         self.logger.warning(f"Chunk {chunk_id} size mismatch! Expected {expected_bytes_per_frame}, got {len(chunk_data)}. Skipping.")
                         self._log_timing('skip_frame_size', chunk_id, timestamp=time.time())
                         continue
                    samples_per_channel = expected_bytes_per_frame // (LIVEKIT_CHANNELS * 2)
                    frame = rtc.AudioFrame(sample_rate=LIVEKIT_SAMPLE_RATE,
                                           num_channels=LIVEKIT_CHANNELS,
                                           samples_per_channel=samples_per_channel,
                                           data=chunk_data)
                    t_start_send = time.time()
                    if self._mic_audio_source:
                        await self._mic_audio_source.capture_frame(frame)
                    else: self.logger.warning(f"Mic source None sending frame {chunk_id}. Stopping."); break
                    t_end_send = time.time()
                    self._log_timing('sent_livekit', chunk_id, timestamp=t_end_send, duration=t_end_send - t_start_send)
                except asyncio.TimeoutError: continue
                except asyncio.CancelledError: self.logger.info("Mic forwarding task cancelled."); break
                except Exception as e:
                    log_chunk_id = chunk_id if 'chunk_id' in locals() else 'unknown'
                    self.logger.error(f"Error in mic forward loop chunk {log_chunk_id}: {e}", exc_info=True)
                    await asyncio.sleep(0.1)
        except asyncio.CancelledError: self.logger.info("Mic forwarding task cancelled (outer).")
        except Exception as e: self.logger.error(f"Fatal error in mic forwarding task: {e}", exc_info=True)
        finally: self.logger.info("Mic forwarding loop finished.")

    def _on_disconnected(self):
        """Callback when disconnected unexpectedly from LiveKit room."""
        self.logger.warning("Disconnected unexpectedly from LiveKit!")
        self._is_connected = False
        if self._trigger_event_queue:
            try: self._loop.call_soon_threadsafe(self._trigger_event_queue.put_nowait, TriggerEvent.UNEXPECTED_DISCONNECT)
            except Exception as e: self.logger.error(f"Error emitting UNEXPECTED_DISCONNECT: {e}")
        else: self.logger.error("Cannot emit UNEXPECTED_DISCONNECT: Trigger queue missing.")

    def _on_track_subscribed(self, track: rtc.Track, publication: rtc.RemoteTrackPublication, participant: rtc.RemoteParticipant):
        self.logger.info(f"Track subscribed: {track.kind} '{track.name}' from {participant.identity}")
        if track.kind == rtc.TrackKind.KIND_AUDIO and participant.identity == ASSISTANT_IDENTITY:
            if track.sid not in self._playback_tasks or self._playback_tasks[track.sid].done():
                self.logger.info(f"Assistant audio track ({track.sid}) detected. Starting playback task.")
                task = asyncio.create_task(self._forward_livekit_to_speaker(track), name=f"Playback_{track.sid[:6]}")
                self._playback_tasks[track.sid] = task
            else: self.logger.warning(f"Assistant audio track ({track.sid}) already has active playback task.")
        else: self.logger.debug(f"Ignoring subscribed track: Kind={track.kind}, Identity={participant.identity}")

    async def _forward_livekit_to_speaker(self, track: rtc.Track):
        """Task reads from LiveKit track, sends chunks to IO Handler playback."""
        self.logger.info(f"Starting playback loop for track {track.sid}")
        audio_stream = rtc.AudioStream(track)
        try:
            async for frame_event in audio_stream:
                frame = frame_event.frame
                if frame.sample_rate != LIVEKIT_SAMPLE_RATE or frame.num_channels != LIVEKIT_CHANNELS:
                    self.logger.warning(f"Unexpected audio format on track {track.sid}. Skipping.")
                    continue
                await self.io_handler.play_audio_chunk(bytes(frame.data))
        except asyncio.CancelledError: self.logger.info(f"Playback task {track.sid} cancelled.")
        except Exception as e: self.logger.error(f"Error in playback loop {track.sid}: {e}", exc_info=True)
        finally:
            self.logger.info(f"Playback loop {track.sid} finished.")
            self._playback_tasks.pop(track.sid, None)

    # --- Timing Methods ---
    def _generate_timing_log_path(self):
         timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
         return os.path.join(TIMING_LOG_DIR, f"timing_log_{timestamp}.json")

    def _get_next_chunk_id(self) -> int:
         self.chunk_id_counter += 1
         return self.chunk_id_counter

    def _log_timing(self, event_type: str, chunk_id: int, **kwargs):
         log_entry = { 'timestamp': time.time(), 'event': event_type,
                       'chunk_id': chunk_id, **kwargs }
         self.timing_data.append(log_entry)

    def save_timing_data(self):
        if not self.timing_data: self.logger.debug("No timing data to save."); return
        if self.timing_log_file_path is None: self.timing_log_file_path = self._generate_timing_log_path()
        if self.timing_log_file_path is None: self.logger.error("Cannot save timing: path generation failed."); return
        self.logger.info(f"Saving {len(self.timing_data)} client timing entries to {self.timing_log_file_path}...")
        try:
            os.makedirs(TIMING_LOG_DIR, exist_ok=True)
            with open(self.timing_log_file_path, 'w') as f: json.dump(self.timing_data, f, indent=2)
            self.logger.info("Client timing data saved.")
        except Exception as e: self.logger.error(f"Failed to save timing data: {e}", exc_info=True)
        self.timing_data = [] ; self.chunk_id_counter = 0
        self.timing_log_file_path = self._generate_timing_log_path()
    # --- End Timing Methods ---


async def main():
    # --- Basic Setup ---
    logging.basicConfig(
        level=logging.INFO, # Start with INFO level for client
        format='%(asctime)s %(levelname)s:%(name)s:%(message)s'
    )
    # Configure numba logger if librosa is used heavily (optional here for now)
    # logging.getLogger('numba').setLevel(logging.WARNING)

    if not LIVEKIT_URL:
        logger.error("LIVEKIT_URL not set in environment.")
        return # Return instead of exit(1)

    # --- State Machine & Control Variables ---
    loop = asyncio.get_running_loop()
    trigger_event_queue = asyncio.Queue()
    stop_event = asyncio.Event() # Global stop signal for main loop only
    current_state = AppState.IDLE
    client: Optional[LightberryLocalClient] = None

    # --- Persistent Components --- #
    persistent_io_handler: Optional[LocalIOHandler] = None
    active_trigger_controller: Optional[TriggerController] = None
    ww_trigger: Optional[WakeWordTrigger] = None
    trinkey_trigger: Optional[TrinkeyTrigger] = None
    keyboard_trigger: Optional[MacKeyboardTrigger] = None
    # --- End Persistent Components --- #

    token: Optional[str] = None
    room_name: Optional[str] = None
    device_id: Optional[str] = None
    username: Optional[str] = None

    # --- Instantiate Persistent IO Handler --- #
    try:
        persistent_io_handler = LocalIOHandler(
            loop=loop,
            logger_instance=logger, # Pass main logger
            trigger_event_queue=trigger_event_queue # Pass the shared queue
        )
    except Exception as e:
         logger.error(f"FATAL: Failed to initialize LocalIOHandler: {e}", exc_info=True)
         return # Cannot continue without IO handler

    # --- Instantiate Trigger Controllers --- #
    try:
        # Always create WakeWordTrigger if IO Handler succeeded
        ww_trigger = WakeWordTrigger(
            loop=loop,
            event_queue=trigger_event_queue,
            io_handler=persistent_io_handler # Pass the handler instance
        )
        logger.info("WakeWordTrigger initialized.")

        # Try platform-specific triggers
        if platform.system() == "Darwin":
            logger.info("Platform is Darwin, attempting MacKeyboardTrigger.")
            keyboard_trigger = MacKeyboardTrigger(loop, trigger_event_queue)
            if not hasattr(keyboard_trigger, '_listener_loop') or not _pynput_available:
                 logger.warning("MacKeyboardTrigger unavailable or pynput failed. Trying Trinkey.")
                 keyboard_trigger = None # Indicate failure
                 # Attempt Trinkey as fallback only if Mac failed
                 trinkey_trigger = TrinkeyTrigger(loop, trigger_event_queue, ADAFRUIT_VID, TRINKEY_PID, TRINKEY_KEYCODE)
                 logger.info("TrinkeyTrigger initialized as fallback.")
            else:
                 logger.info("MacKeyboardTrigger initialized successfully.")
        else:
             # Default to Trinkey on non-Darwin platforms
             logger.info("Platform is not Darwin, attempting TrinkeyTrigger.")
             trinkey_trigger = TrinkeyTrigger(loop, trigger_event_queue, ADAFRUIT_VID, TRINKEY_PID, TRINKEY_KEYCODE)
             logger.info("TrinkeyTrigger initialized.")

        # --- Select Active Trigger --- #
        # TODO: Make this selection configurable (e.g., command line arg)
        # For now, let's prioritize WakeWord if available, then keyboard, then trinkey
        # This needs refinement based on desired default behavior
        if ww_trigger:
            active_trigger_controller = ww_trigger
        elif keyboard_trigger and _pynput_available:
             active_trigger_controller = keyboard_trigger
        elif trinkey_trigger:
             active_trigger_controller = trinkey_trigger
        else:
             active_trigger_controller = None # No trigger available

        if active_trigger_controller:
             logger.info(f"Selected {active_trigger_controller.__class__.__name__} as the active trigger controller (for logging/debug).")
        else:
             logger.error("Could not initialize any functional trigger controller!")
             if persistent_io_handler: await persistent_io_handler.stop()
             return

    except Exception as e:
         logger.error(f"FATAL: Error during trigger controller initialization: {e}", exc_info=True)
         if persistent_io_handler: await persistent_io_handler.stop()
         return

    # --- Main State Machine Loop ---
    try:
        while not stop_event.is_set():
            logger.debug(f"--- Main Loop: Current State = {current_state.name} ---")

            if current_state == AppState.IDLE:
                print("\n>>> Current State: IDLE <<<", flush=True)
                logger.info("Entered IDLE state.")
                if client is not None:
                     logger.warning("Client instance found in IDLE state, attempting stop...")
                     try: await client.stop()
                     except Exception as e: logger.error(f"Error stopping stale client: {e}")
                     client = None
                try:
                    logger.info("Ensuring persistent IO handler is started...")
                    await persistent_io_handler.start()
                    # --- Start ALL available trigger listeners --- #
                    logger.info("Starting all available trigger listeners...")
                    if ww_trigger:
                         logger.debug("Starting WakeWordTrigger listener...")
                         ww_trigger.start_listening()
                    if keyboard_trigger:
                         logger.debug("Starting MacKeyboardTrigger listener...")
                         keyboard_trigger.start_listening()
                    if trinkey_trigger:
                         logger.debug("Starting TrinkeyTrigger listener...")
                         trinkey_trigger.start_listening()
                    # --- End Start ALL listeners --- #
                except Exception as e:
                    logger.error(f"Error starting persistent components/listeners: {e}. Stopping application.", exc_info=True)
                    stop_event.set() ; continue

                print(f"Waiting for any trigger...", flush=True)
                # Clear potential stale events
                while not trigger_event_queue.empty():
                    try: stale_event = trigger_event_queue.get_nowait(); trigger_event_queue.task_done()
                    except asyncio.QueueEmpty: break
                    except Exception: pass
                    logger.debug(f"Discarded stale event: {stale_event.name}")
                try:
                    logger.debug("<<< Waiting for trigger event >>>")
                    event = await trigger_event_queue.get()
                    logger.info(f"Event received from trigger queue: {event.name}")
                    trigger_event_queue.task_done()
                    if event == TriggerEvent.FIRST_PRESS:
                        logger.info("Activation trigger received. Transitioning to CONNECTING.")
                        print("Trigger received! Connecting...", flush=True)
                        current_state = AppState.CONNECTING
                    elif event == TriggerEvent.UNEXPECTED_DISCONNECT:
                         logger.warning("Received UNEXPECTED_DISCONNECT in IDLE state. Ignoring.")
                    else:
                        logger.warning(f"Unexpected event '{event.name}' received in IDLE state. Ignoring.")
                except asyncio.CancelledError: logger.info("IDLE state wait cancelled. Stopping application."); stop_event.set()
                except Exception as e:
                    logger.error(f"Error waiting for trigger event in IDLE: {e}", exc_info=True)
                    await asyncio.sleep(1) # Pause before retry within IDLE

            elif current_state == AppState.CONNECTING:
                print(">>> Current State: CONNECTING <<<", flush=True)
                logger.info("Entered CONNECTING state.")
                token, room_name = None, None
                try:
                    device_id = get_or_create_device_id()
                    username = "recDNsqLQ9CKswdVJ" # TODO: Make configurable
                    logger.info(f"Fetching credentials for Device ID: {device_id}, User: {username}")
                    token, room_name = await get_credentials(device_id, username)
                    if token and room_name:
                        logger.info("Credentials obtained. Creating client...")
                        client = LightberryLocalClient(url=LIVEKIT_URL, token=token, room_name=room_name,
                                                   loop=loop, trigger_event_queue=trigger_event_queue,
                                                   io_handler=persistent_io_handler)
                        await client.start() # Connects, signals handler, starts client tasks
                        logger.info("Client started successfully.")
                        current_state = AppState.ACTIVE
                        # --- Clear trigger queue after successful transition --- #
                        logger.debug("Clearing trigger queue after activating client...")
                        while not trigger_event_queue.empty():
                            try: trigger_event_queue.get_nowait(); trigger_event_queue.task_done()
                            except asyncio.QueueEmpty: break
                            except Exception: pass # Ignore other potential errors during clear
                        logger.debug("Trigger queue cleared.")
                        # ----------------------------------------------------- #
                        print("Connected and Active!", flush=True)
                    else:
                        logger.error("Failed to obtain credentials.")
                        print("Connection failed (credentials). Returning to IDLE.", flush=True)
                        current_state = AppState.IDLE ; await asyncio.sleep(2)
                except Exception as e:
                    logger.error(f"Error during CONNECTING state: {e}", exc_info=True)
                    print(f"Connection failed ({type(e).__name__}). Returning to IDLE.", flush=True)
                    if client: 
                        try: await client.stop() 
                        except Exception: pass
                    client = None ; current_state = AppState.IDLE ; await asyncio.sleep(5)

            elif current_state == AppState.ACTIVE:
                print(">>> Current State: ACTIVE <<< Mic Live | Trigger: Pause/Stop", flush=True)
                logger.info("Entered ACTIVE state. Waiting for trigger.")
                try:
                    event = await trigger_event_queue.get()
                    logger.info(f"Event received in ACTIVE state: {event.name}")
                    trigger_event_queue.task_done()
                    if event == TriggerEvent.FIRST_PRESS or event == TriggerEvent.SUBSEQUENT_PRESS:
                        logger.info("Pause trigger received. Pausing client.")
                        print("Pausing...", flush=True)
                        if client: await client.pause()
                        current_state = AppState.PAUSED
                    elif event == TriggerEvent.AUTO_PAUSE:
                         logger.info("Auto-pause event received. Pausing client.")
                         print("Auto-pausing due to silence...", flush=True)
                         if client: await client.pause()
                         current_state = AppState.PAUSED
                    elif event == TriggerEvent.DOUBLE_PRESS:
                        logger.info("Stop trigger received. Stopping client.")
                        print("Stopping...", flush=True)
                        current_state = AppState.STOPPING
                    elif event == TriggerEvent.UNEXPECTED_DISCONNECT:
                         logger.warning("Unexpected disconnect event received. Stopping client.")
                         current_state = AppState.STOPPING
                    else: logger.warning(f"Unexpected event '{event.name}' in ACTIVE state.")
                except asyncio.CancelledError: logger.info("ACTIVE state wait cancelled."); stop_event.set()
                except Exception as e: logger.error(f"Error in ACTIVE state: {e}", exc_info=True); current_state = AppState.STOPPING

            elif current_state == AppState.PAUSED:
                print(">>> Current State: PAUSED <<< ⏸️ | Trigger: Resume/Stop", flush=True)
                logger.info("Entered PAUSED state. Waiting for trigger.")
                try:
                    event = await trigger_event_queue.get()
                    logger.info(f"Event received in PAUSED state: {event.name}")
                    trigger_event_queue.task_done()
                    if event == TriggerEvent.FIRST_PRESS or event == TriggerEvent.SUBSEQUENT_PRESS:
                        logger.info("Resume trigger received. Resuming client.")
                        print("Resuming...", flush=True)
                        if client: await client.resume()
                        current_state = AppState.ACTIVE
                    elif event == TriggerEvent.DOUBLE_PRESS:
                        logger.info("Stop trigger received. Stopping client.")
                        print("Stopping...", flush=True)
                        current_state = AppState.STOPPING
                    elif event == TriggerEvent.UNEXPECTED_DISCONNECT:
                         logger.warning("Unexpected disconnect event while PAUSED. Stopping client.")
                         current_state = AppState.STOPPING
                    elif event == TriggerEvent.AUTO_PAUSE: logger.debug("Ignoring AUTO_PAUSE while PAUSED.")
                    else: logger.warning(f"Unexpected event '{event.name}' in PAUSED state.")
                except asyncio.CancelledError: logger.info("PAUSED state wait cancelled."); stop_event.set()
                except Exception as e: logger.error(f"Error in PAUSED state: {e}", exc_info=True); current_state = AppState.STOPPING

            elif current_state == AppState.STOPPING:
                print(">>> Current State: STOPPING <<<", flush=True)
                logger.info("Entered STOPPING state (stopping client only).")
                if client:
                    logger.info("Stopping Lightberry client...")
                    try: await client.stop()
                    except Exception as e: logger.error(f"Error stopping client: {e}", exc_info=True)
                client = None
                logger.info("Transitioning back to IDLE state.")
                current_state = AppState.IDLE
                await asyncio.sleep(1)

            else:
                logger.error(f"Reached unexpected state: {current_state}. Resetting to IDLE.")
                current_state = AppState.IDLE ; await asyncio.sleep(1)

    except asyncio.CancelledError: logger.info("Main loop task cancelled.")
    except Exception as e:
        logger.error(f"Unhandled exception in main loop: {e}", exc_info=True)
        stop_event.set() # Ensure final cleanup
    finally:
        logger.info("Main function initiating final shutdown...")
        # Ensure cleanup, regardless of how the loop exited
        if current_state != AppState.STOPPING:
             logger.warning(f"Main loop exited unexpectedly from state {current_state.name}. Forcing STOPPING.")

        # Define helper for final cleanup within timeout
        async def _perform_final_cleanup(client_instance, trigger_controller_instance):
            if client_instance:
                logger.info("Performing final client stop...")
                try:
                    # stop() now has its own internal timeout
                    await client_instance.stop()
                except asyncio.TimeoutError:
                    logger.error("Client stop timed out during final cleanup.")
                    # Continue cleanup despite client timeout
                except Exception as e:
                    logger.error(f"Error during final client stop: {e}", exc_info=True)
            if trigger_controller_instance:
                 logger.info("Performing final trigger listener stop...")
                 try:
                      # stop_listening is synchronous, but give thread a moment
                      trigger_controller_instance.stop_listening()
                      # Brief sleep might help ensure thread sees the flag
                      await asyncio.sleep(0.5)
                 except Exception as e:
                      logger.error(f"Error during final trigger stop: {e}")
            logger.info("Final cleanup steps completed (within timeout check).")

        # --- Graceful shutdown with timeout using wait_for ---
        shutdown_timeout = 10.0 # Seconds
        try:
            logger.info(f"Attempting graceful shutdown (timeout: {shutdown_timeout}s)...")
            await asyncio.wait_for(_perform_final_cleanup(client, trigger_controller), timeout=shutdown_timeout)
            logger.info("Graceful shutdown completed or timed out okay.")

        except asyncio.TimeoutError:
             logger.error(f"Graceful shutdown timed out after {shutdown_timeout}s. Forcing exit.")
             os._exit(1) # Force exit
        except Exception as final_e:
             logger.error(f"Exception during final shutdown sequence: {final_e}", exc_info=True)
             logger.error("Attempting force exit after shutdown exception.")
             os._exit(1) # Force exit
        # --- End Graceful shutdown ---

        logger.info("Main function shutdown complete.")

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("Keyboard interrupt received. Shutting down...")
    # Note: General exceptions inside main() are caught within main itself.
    # asyncio.run() will propagate exceptions, but we handle them internally.
    logger.info("Client execution finished.")
