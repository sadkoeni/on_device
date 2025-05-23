import logging
import os
import platform # Added for platform detection
from typing import Optional, Callable, Union, AsyncGenerator
import numpy as np
import time # Added time
import orjson as json # Added json
import binascii # Added for base64 encoding
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
import websocket # Added for ElevenLabs WS
import base64 # Added for ElevenLabs WS audio decoding
import collections # Added for circular buffer
import soundfile as sf # Added for reading WAV files

from livekit import rtc
from livekit import api
from dotenv import load_dotenv
load_dotenv()

import asyncio
import time
import os
from typing import Optional, TYPE_CHECKING
import logging
import pyaudio
import sounddevice as sd # Import sounddevice

import c_resampler

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
    ASSISTANT_ACTIVITY_TIMEOUT = 6 # New event for assistant inactivity
# --- End Enums ---


ASSISTANT_IDENTITY = os.getenv("ASSISTANT_IDENTITY", "assistant")
ASSISTANT_ROOM_NAME = os.getenv("LIVEKIT_ROOM_NAME", "lightberry")
DEVICE_ID = os.getenv("DEVICE_ID", "rechavpPa8YyWv7Zn")
EXPECTED_ASSISTANT_IDENTITY = f"agent-{DEVICE_ID}"
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

# --- ElevenLabs Configuration ---
FALLBACK_API_KEY = "sk_2f5964354fcc33fcf02f94ff257f56d8a57f2bfe6716549e" # Placeholder for fallback
ELEVENLABS_API_KEY = os.getenv("ELEVENLABS_API_KEY", FALLBACK_API_KEY)

logger = logging.getLogger("LightberryLocalClient")
logger.info(f"Using standard audio sample rate: {LIVEKIT_SAMPLE_RATE}Hz") # Use standard rate
# set logger level to info
logger.setLevel(logging.WARNING)

if ELEVENLABS_API_KEY == FALLBACK_API_KEY:
    logger.warning(f"ELEVENLABS_API_KEY not set in environment. Using placeholder fallback key: '{FALLBACK_API_KEY}'. Wake phrase playback will likely fail unless this is replaced.")
elif not ELEVENLABS_API_KEY: # Handles case where env var is set to an empty string
    logger.warning("ELEVENLABS_API_KEY is set but empty. Wake phrase playback will fail.")
# --- End ElevenLabs Configuration ---



AUTH_API_URL = "https://lightberry.vercel.app/api/authenticate/{}" # Placeholder for device ID

def get_or_create_device_id() -> str:
    """Gets the device ID from a local file or creates a new one."""
    return DEVICE_ID

async def get_credentials(device_id: str, username: str) -> tuple[Optional[str], Optional[str], Optional[str], Optional[str]]:
    """Fetches LiveKit token, room name, wake phrase and voice ID from the authentication API, with fallback."""
    url = AUTH_API_URL.format(device_id)
    payload = {"username": username, "x-device-api-key": username}
    logger.info(f"Attempting to fetch credentials from {url} for username '{username}'")

    # --- Define Fallback Credentials ---
    fallback_room_name = "lightberry"
    fallback_token = "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJuYW1lIjoiTGlnaHRiZXJyeSBBc3Npc3RhbnQiLCJ2aWRlbyI6eyJyb29tSm9pbiI6dHJ1ZSwicm9vbSI6ImxpZ2h0YmVycnkiLCJjYW5QdWJsaXNoIjp0cnVlLCJjYW5TdWJzY3JpYmUiOnRydWUsImNhblB1Ymxpc2hEYXRhIjp0cnVlfSwic3ViIjoidGVzdGVyIiwiaXNzIjoiQVBJM0VucVFRbTNqZEFYIiwibmJmIjoxNzQ1Nzg4NTY5LCJleHAiOjE3NDU4MTAxNjl9.JDMdxWZ6Qb6X3H_gCFsHfVJOItgAL0q6EWYp1zv6uO8"
    fallback_wake_phrase = "Hello Sir."  # Default wake phrase
    fallback_voice_id = "C8wZRioDZqA6fkwDW6Df"  # Default voice ID (Bradford)
    # ---------------------------------

    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(url, json=payload) as response:
                response.raise_for_status() # Raise an exception for bad status codes (4xx or 5xx)
                data = await response.json()

                if data.get("success"):
                    token = data.get("livekit_token")
                    room_name = data.get("room_name")
                    wake_phrase = data.get("wake_phrase", fallback_wake_phrase)
                    voice_id = data.get("voice_id", fallback_voice_id)
                    if token and room_name:
                        logger.info(f"Successfully retrieved credentials: {room_name}")
                        return token, room_name, wake_phrase, voice_id
                    else:
                        logger.error("API response missing token or room name. Using fallback.")
                        return fallback_token, fallback_room_name, fallback_wake_phrase, fallback_voice_id
                else:
                    error_msg = data.get("error", "Unknown error")
                    logger.error(f"API request failed: {error_msg}. Using fallback.")
                    return fallback_token, fallback_room_name, fallback_wake_phrase, fallback_voice_id
    except aiohttp.ClientError as e:
        logger.error(f"HTTP request error during authentication: {e}. Using fallback.")
        return fallback_token, fallback_room_name, fallback_wake_phrase, fallback_voice_id
    except asyncio.TimeoutError:
        logger.error(f"Timeout connecting to authentication API at {url}. Using fallback.")
        return fallback_token, fallback_room_name, fallback_wake_phrase, fallback_voice_id
    except json.JSONDecodeError as e:
        logger.error(f"Failed to decode JSON response from authentication API: {e}. Using fallback.")
        return fallback_token, fallback_room_name, fallback_wake_phrase, fallback_voice_id
    except Exception as e:
        logger.error(f"An unexpected error occurred during credential fetching: {e}. Using fallback.", exc_info=True)
        return fallback_token, fallback_room_name, fallback_wake_phrase, fallback_voice_id

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

# --- Resampling Helper --- 
TARGET_SAMPLE_RATE = LIVEKIT_SAMPLE_RATE # Define target rate (should be 48000)

async def resample_audio_chunk(audio_data: bytes, source_rate: int, target_rate: int, logger_instance: Optional[logging.Logger] = None) -> bytes:
    """Resamples an audio chunk using efficient methods based on rates."""
    effective_logger = logger_instance or logger
    source_rate = int(source_rate)
    target_rate = int(target_rate)
    if source_rate == target_rate:
        return audio_data
    
    try:
        # Fast path: 24kHz → 48kHz (TTS upsampling)
        if source_rate == 24000 and target_rate == 48000:
            return c_resampler.upsample_24k_to_48k(audio_data)
        
        # Only import heavy libraries on fallback path
        import resampy
        import soundfile as sf
        import io
        
        # Keep existing resampy code as fallback for other rates
        data_io = io.BytesIO(audio_data)
        audio_array, _ = sf.read(data_io, dtype='int16', channels=1, samplerate=source_rate, format='RAW', subtype='PCM_16')
        audio_float = audio_array.astype(np.float32) / 32768.0
        resampled_float = resampy.resample(audio_float, sr_orig=source_rate, sr_new=target_rate, filter='kaiser_fast')
        resampled_array = (resampled_float * 32768.0).astype(np.int16)
        resampled_bytes_io = io.BytesIO()
        sf.write(resampled_bytes_io, resampled_array, target_rate, format='RAW', subtype='PCM_16')
        return resampled_bytes_io.getvalue()
        
    except Exception as e:
        effective_logger.error(f"Error during audio resampling: {e}", exc_info=True)
        return audio_data
# --- End Resampling Helper --- 


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
                 activation_word: str = "Hi_Ga_nesh",        # Changed default activation word
                 stop_word: str = "go_to_sleep",    # Changed default stop word
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
            self.logger.info("Initializing openWakeWord model with specific custom models...")
            # Use ONNX framework and specify only the required models by path
            inference_framework = 'onnx'
            custom_model_paths = [
                f"wakeword_models/Hi_Ga_nesh.{inference_framework}", 
                f"wakeword_models/go_to_sleep.{inference_framework}"
            ]
            self.oww_model = Model(
                wakeword_models=custom_model_paths,
                inference_framework=inference_framework
            )
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

                    # --- Downsample using custom C code --- #
                    audio = c_resampler.decimate(chunk_data)
                    audio_to_predict = np.frombuffer(audio, dtype=np.int16)
                    
                     
                    if audio_to_predict.size>0:
                        self.oww_model.predict(audio_to_predict)
                        scores = self.oww_model.prediction_buffer
                        activation_score = scores.get(self.activation_word, [0.0])[-1]
                        stop_score = scores.get(self.stop_word, [0.0])[-1]

                        # --- Emit Events --- #
                        current_time = time.time()

                        # Check for STOP word first, independently of refractory period
                        if stop_score > self.threshold:
                            # Check if enough time passed since the *last stop* detection to avoid spamming stops
                            # (Using a shorter refractory for stop, e.g., 0.5s, or potentially none)
                            # For simplicity, let's use the same timer but check it separately.
                            # We only want to emit STOP if not recently activated OR stopped.
                            # Re-evaluate this logic if stop needs to be immediate after activation.
                            if True: #current_time - self._last_detection_time > 1.0: # dont Re-use timer for now
                                self.logger.info(f"Stop word '{self.stop_word}' detected (Score: {stop_score:.2f}). Emitting DOUBLE_PRESS.")
                                self._emit_event(TriggerEvent.DOUBLE_PRESS)
                                self._last_detection_time = current_time # Update timestamp
                            else:
                                 self.logger.debug(f"Stop word '{self.stop_word}' detected but ignored due to refractory period.")
                        
                        # Check for ACTIVATION word, applying the refractory period
                        elif activation_score > self.threshold:
                             if current_time - self._last_detection_time > 1.0: # Refractory check specific to activation
                                self.logger.info(f"Activation word '{self.activation_word}' detected (Score: {activation_score:.2f}). Emitting FIRST_PRESS.")
                                self._emit_event(TriggerEvent.FIRST_PRESS)
                                self._last_detection_time = current_time # Update timestamp
                             else:
                                 self.logger.debug(f"Activation word '{self.activation_word}' detected but ignored due to refractory period.")

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
    SILENCE_THRESHOLD = 20  # LOWERED threshold - much more sensitive to soft sounds
    SILENCE_COUNT_THRESHOLD = 20 # Number of consecutive silent chunks to trigger end of speech
    # Added for early silence detection - increased for safety
    FALLING_EDGE_BUFFER_FRAMES = 50  # Buffer frames (~500ms) after speech → silence transition
    # Pre-speech buffer size - keep recent frames to capture wake word beginnings
    PRE_SPEECH_BUFFER_SIZE = 10  # Keep ~100ms of audio before speech onset
    # Debug toggle to disable silence detection completely
    DISABLE_SILENCE_DETECTION = False  # Set to True to send all frames to wake word detector

    ASSISTANT_INACTIVITY_TIMEOUT_S = 40.0 # Timeout for assistant inactivity

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
        self._stop_event = asyncio.Event() # Global stop for internal loops

        # Initialize tasks
        self._input_dist_task: Optional[asyncio.Task] = None
        self._output_transfer_task: Optional[asyncio.Task] = None

        # Initialize internal state tracking
        self._assistant_speaking: bool = False
        self._livekit_input_enabled: bool = False
        self._playback_finished_event: Optional[asyncio.Event] = None
        self._consecutive_silence_count: int = 0 # Add silence counter
        
        # New state variables for early silence detection in wake word path
        self._is_speech_active = False        # Current speech state
        self._silence_frame_count = 0         # Consecutive silent frames
        self._frames_processed = 0            # Count for debug stats
        self._frames_skipped = 0              # Count for debug stats
        # Create circular buffer for pre-speech frames (stores recent silent frames)
        self._pre_speech_buffer = collections.deque(maxlen=self.PRE_SPEECH_BUFFER_SIZE)
        self._activity_timeout_task: Optional[asyncio.Task] = None # Task for assistant inactivity timer
        
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

    # --- Add helper method for silence detection ---
    def _is_chunk_silent(self, audio_chunk: bytes) -> bool:
        """Fast check if audio chunk is below silence threshold."""
        # Convert bytes to int16 numpy array
        audio_int16 = np.frombuffer(audio_chunk, dtype=np.int16)
        
        # Calculate absolute mean amplitude
        energy = np.mean(np.abs(audio_int16))
        
        # Return True if below threshold
        return energy < self.SILENCE_THRESHOLD

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

    async def signal_client_disconnected(self):
        """Called by the client when it is disconnecting."""
        self.logger.info("Client disconnected signal received. Disabling LiveKit input path.")
        self._cancel_inactivity_timer() # Cancel timer on client disconnect
        await self.disable_livekit_input() # Use new method

    async def enable_livekit_input(self):
        """Enable forwarding of microphone input to the LiveKit queue."""
        if not self._livekit_input_enabled:
            self.logger.info("Enabling LiveKit input path.")
            await self._empty_queue(self._livekit_input_queue) # Clear stale data
            self._livekit_input_enabled = True
        # Always (re)start timer if assistant is not speaking when input becomes active
        self._start_inactivity_timer()

    async def disable_livekit_input(self):
        """Disable forwarding of microphone input to the LiveKit queue."""
        if self._livekit_input_enabled:
            self.logger.info("Disabling LiveKit input path.")
            self._livekit_input_enabled = False
            await self._empty_queue(self._livekit_input_queue) # Clear queue
        # (Re)start timer if assistant is not speaking when input is paused (still in active session)
        self._start_inactivity_timer()

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
        self._cancel_inactivity_timer() # Cancel timer when handler itself is stopping
        self.logger.info("LocalIOHandler stopped.")

    # --- Internal Processing Loops ---
    async def _input_distribution_loop(self):
        """Distributes audio from the single mic input based on flags."""
        self.logger.info("Input distribution loop started.")
        if self.DISABLE_SILENCE_DETECTION:
            self.logger.warning("SILENCE DETECTION DISABLED - sending all frames to wake word")

        source_queue = None
        try:
            if hasattr(self.audio_input, 'get_audio_chunk_queue'):
                source_queue = self.audio_input.get_audio_chunk_queue()
            else:
                self.logger.error("InputDistLoop: audio_input missing get_audio_chunk_queue.") ; return
        except Exception as e:
             self.logger.error(f"InputDistLoop: Failed to get source queue: {e}. Exiting.", exc_info=True) ; return
        if source_queue is None: self.logger.error("InputDistLoop: Source queue is None. Exiting.") ; return

        # Variables for periodic stats logging
        last_stats_time = time.time()
        stats_interval = 60.0  # Log stats every minute
        # Energy logging to debug threshold issues
        last_energy_log_time = time.time()
        energy_log_interval = 5.0  # Log energy levels every 5 seconds during debug

        while not self._stop_event.is_set():
            try:
                chunk_tuple = await asyncio.wait_for(source_queue.get(), timeout=0.5)
                source_queue.task_done()
                self._frames_processed += 1

                # Check assistant speaking flag first
                if self._assistant_speaking:
                    continue # Skip this chunk entirely if assistant is speaking
                
                # Extract chunk data for silence check
                chunk_data = chunk_tuple[0]
                
                # --- Check if silence detection is disabled ---
                if self.DISABLE_SILENCE_DETECTION:
                    # Always send all frames to wake word when disabled
                    try:
                        self._wakeword_audio_queue.put_nowait(chunk_tuple)
                    except queue.Full:
                        self.logger.debug("WakeWord audio queue full, discarding chunk.")
                    except Exception as e:
                        self.logger.error(f"Error putting chunk to WakeWord queue: {e}")
                else:
                    # --- Early Silence Detection for Wake Word Path ---
                    # Check energy level of current chunk
                    current_chunk_silent = self._is_chunk_silent(chunk_data)
                    
                    # Log energy levels periodically during debug
                    if self.logger.isEnabledFor(logging.DEBUG):
                        current_time = time.time()
                        if current_time - last_energy_log_time > energy_log_interval:
                            # Calculate energy level for debugging
                            audio_int16 = np.frombuffer(chunk_data, dtype=np.int16)
                            energy = np.mean(np.abs(audio_int16))
                            self.logger.debug(f"Current audio energy: {energy:.1f}, threshold: {self.SILENCE_THRESHOLD}")
                            last_energy_log_time = current_time
                    
                    # Always store silent frames in pre-speech buffer
                    if current_chunk_silent and not self._is_speech_active:
                        # Add to circular buffer when in silent state
                        self._pre_speech_buffer.append(chunk_tuple)
                    
                    # State machine logic
                    if current_chunk_silent:
                        # Chunk is silent
                        if self._is_speech_active:
                            # We're in speech state but detected silence
                            self._silence_frame_count += 1
                            
                            # Check if we've exceeded buffer length
                            if self._silence_frame_count > self.FALLING_EDGE_BUFFER_FRAMES:
                                # Transition to silent state after buffer period
                                self._is_speech_active = False
                                # Skip sending this frame to wake word (silence beyond buffer)
                                self._frames_skipped += 1
                                if self.logger.isEnabledFor(logging.DEBUG) and self._frames_skipped % 100 == 0:
                                    self.logger.debug(f"Skipped {self._frames_skipped} silent frames")
                            else:
                                # Still in buffer period - send frame to wake word
                                try: 
                                    self._wakeword_audio_queue.put_nowait(chunk_tuple)
                                except queue.Full: 
                                    self.logger.debug("WakeWord audio queue full, discarding chunk.")
                                except Exception as e: 
                                    self.logger.error(f"Error putting chunk to WakeWord queue: {e}")
                        else:
                            # Already in silent state, skip sending to wake word
                            self._frames_skipped += 1
                    else:
                        # Non-silent chunk detected
                        if not self._is_speech_active:
                            # Speech onset detected - transition to speech state
                            self._is_speech_active = True
                            self.logger.info("Speech onset detected - sending pre-speech buffer")
                            
                            # Send pre-speech buffer frames first to capture word beginnings
                            for buffered_chunk in self._pre_speech_buffer:
                                try:
                                    self._wakeword_audio_queue.put_nowait(buffered_chunk)
                                    if self.logger.isEnabledFor(logging.DEBUG):
                                        self.logger.debug(f"Sent pre-speech buffer frame")
                                except queue.Full:
                                    self.logger.debug("WakeWord audio queue full, discarding pre-speech chunk.")
                                except Exception as e:
                                    self.logger.error(f"Error putting pre-speech chunk to WakeWord queue: {e}")
                        
                        # Reset silence counter
                        self._silence_frame_count = 0
                        
                        # Always queue non-silent chunks for wake word
                        try: 
                            self._wakeword_audio_queue.put_nowait(chunk_tuple)
                        except queue.Full: 
                            self.logger.debug("WakeWord audio queue full, discarding chunk.")
                        except Exception as e: 
                            self.logger.error(f"Error putting chunk to WakeWord queue: {e}")
                    # --- End Early Silence Detection ---

                # Always queue for LiveKit *only* if it's enabled by the state machine
                if self._livekit_input_enabled:
                    try:
                        await self._livekit_input_queue.put(chunk_tuple) 
                    except Exception as e: 
                        self.logger.error(f"Error putting chunk to LiveKit input queue: {e}")
                
                # Periodically log silence detection stats
                current_time = time.time()
                if current_time - last_stats_time > stats_interval:
                    if self._frames_processed > 0:
                        skip_percent = (self._frames_skipped / self._frames_processed) * 100
                        self.logger.info(f"Silence detection stats: {self._frames_skipped}/{self._frames_processed} frames skipped ({skip_percent:.1f}%)")
                    # Reset stats
                    self._frames_processed = 0
                    self._frames_skipped = 0
                    last_stats_time = current_time

            except asyncio.TimeoutError: continue
            except asyncio.CancelledError: self.logger.info("Input distribution loop cancelled."); break
            except Exception as e:
                self.logger.error(f"Error in input distribution loop: {e}", exc_info=True)
                await asyncio.sleep(0.1)
        self.logger.info("Input distribution loop finished.")

    async def _transfer_output_loop(self):
        """Reads from speaker queue, manages _assistant_speaking flag, sends to speaker."""
        self.logger.info("Output transfer loop started.")
        
        while not self._stop_event.is_set():
            try:
                chunk = await asyncio.wait_for(self._speaker_output_queue.get(), timeout=0.2)
                self._speaker_output_queue.task_done()
                if chunk is None: continue 

                # --- VAD Logic --- 
                energy = 0.0
                is_currently_speech = False
                try:
                    if chunk:
                        audio_int16 = np.frombuffer(chunk, dtype=np.int16)
                        if audio_int16.size > 0: 
                            energy = np.mean(np.abs(audio_int16)) + 1e-9 # Add epsilon for safety
                            is_currently_speech = energy > self.SILENCE_THRESHOLD
                except ValueError as ve:
                    # Handle potential value error from frombuffer if chunk size is wrong
                    self.logger.error(f"OutputTransferLoop: ValueError calculating energy (likely chunk size issue): {ve}. Chunk size: {len(chunk)}")
                    # Skip processing this chunk?
                    continue 
                except Exception as e: 
                    self.logger.error(f"OutputTransferLoop: Error calculating energy: {e}")
                    # Reset state or continue?
                    continue
                # --- End VAD Logic ---

                # --- Manage _assistant_speaking flag --- 
                if is_currently_speech:
                    self._consecutive_silence_count = 0 
                    if not self._assistant_speaking:
                        # Transition: Silence -> Speech
                        self.logger.debug(f"Output VAD: Start of speech detected (Energy: {energy:.2f}).")
                        self._assistant_speaking = True
                        self._cancel_inactivity_timer() # Assistant started speaking, cancel timer
                        await self.audio_output.signal_start_of_speech()
                        # NO LONGER directly pauses input paths here
                else: # Currently silent chunk
                    if self._assistant_speaking:
                        # Transition: Speech -> Potential Silence
                        self._consecutive_silence_count += 1
                        if self._consecutive_silence_count >= self.SILENCE_COUNT_THRESHOLD:
                            # --- Trigger End of Speech --- 
                            self.logger.info(f"Output VAD: End of speech detected ({self._consecutive_silence_count} consecutive silent chunks). Waiting for playback.")
                            self._consecutive_silence_count = 0 
                            await self.audio_output.signal_end_of_speech()
                            await self._wait_for_output_completion() 
                            self._assistant_speaking = False # <<< Only change this flag
                            self._start_inactivity_timer() # Assistant stopped speaking, start timer
                            # --- End Trigger End of Speech --- 
                    pass # Do nothing
                # --- End Manage _assistant_speaking flag --- 

                # --- Send chunk to speaker --- 
                if hasattr(self, 'audio_output') and self.audio_output and hasattr(self.audio_output, 'send_audio_chunk'):
                     await self.audio_output.send_audio_chunk(chunk)
                else: 
                     self.logger.warning("Cannot send chunk to speaker, audio_output invalid or doesn't support send_audio_chunk.")
                # --- End Send chunk --- 

            except asyncio.TimeoutError:
                 if self._assistant_speaking:
                     # Timeout implies end of speech
                     self.logger.info("Output VAD: End of speech detected (timeout). Waiting for playback.")
                     self._assistant_speaking = False # <<< Only change this flag
                     self._consecutive_silence_count = 0 
                     await self.audio_output.signal_end_of_speech()
                     await self._wait_for_output_completion()
                     self._start_inactivity_timer() # Assistant stopped speaking (timeout), start timer
                 continue # Continue loop after timeout
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

    # --- Assistant Inactivity Timer Methods ---
    def _start_inactivity_timer(self):
        self._cancel_inactivity_timer() # Cancel any existing one first
        if not self._assistant_speaking: # Only start if assistant is not currently speaking
            self.logger.debug(f"Starting assistant inactivity timer for {self.ASSISTANT_INACTIVITY_TIMEOUT_S}s.")
            self._activity_timeout_task = self._loop.create_task(self._assistant_inactivity_monitor())
        else:
            self.logger.debug("Assistant is currently speaking; inactivity timer not started.")

    def _cancel_inactivity_timer(self):
        if self._activity_timeout_task and not self._activity_timeout_task.done():
            self.logger.debug("Cancelling assistant inactivity timer task.")
            self._activity_timeout_task.cancel()
        self._activity_timeout_task = None

    async def _assistant_inactivity_monitor(self):
        try:
            await asyncio.sleep(self.ASSISTANT_INACTIVITY_TIMEOUT_S)
            # If we reach here, the sleep completed without cancellation
            if self._trigger_event_queue: # Check if queue exists
                self.logger.info(f"Assistant inactivity timeout of {self.ASSISTANT_INACTIVITY_TIMEOUT_S}s reached. Emitting ASSISTANT_ACTIVITY_TIMEOUT event.")
                try:
                    # Ensure we are in a context that can put to asyncio.Queue
                    if self._loop.is_running(): # Check if loop is running
                         self._trigger_event_queue.put_nowait(TriggerEvent.ASSISTANT_ACTIVITY_TIMEOUT)
                    else:
                        self.logger.warning("Event loop not running, cannot put ASSISTANT_ACTIVITY_TIMEOUT.")
                except asyncio.QueueFull:
                    self.logger.warning("Trigger event queue full, ASSISTANT_ACTIVITY_TIMEOUT event might be lost.")
                except Exception as e:
                    self.logger.error(f"Error putting ASSISTANT_ACTIVITY_TIMEOUT to queue: {e}", exc_info=True)
            else:
                self.logger.warning("Trigger event queue not available, cannot emit ASSISTANT_ACTIVITY_TIMEOUT.")
        except asyncio.CancelledError:
            self.logger.debug("Assistant inactivity monitor task was cancelled.")
        except Exception as e:
            self.logger.error(f"Error in assistant inactivity monitor: {e}", exc_info=True)
        finally:
            # Clear the task reference if this task instance is the one currently stored
            # and it's finishing (either normally, by error, or cancellation handled by _cancel method)
            if self._activity_timeout_task is asyncio.current_task():
                self._activity_timeout_task = None
    # --- End Assistant Inactivity Timer Methods ---

    # --- Local WAV Playback Method ---
    async def play_local_wav_file(self, file_path: str):
        """Plays a local WAV file through the speaker output."""
        self.logger.info(f"Attempting to play local WAV file: {file_path}")
        if not os.path.exists(file_path):
            self.logger.error(f"Local WAV file not found: {file_path}")
            return

        try:
            # Read WAV file
            audio_data, source_rate = sf.read(file_path, dtype='int16', always_2d=False)

            if audio_data.ndim > 1 and audio_data.shape[1] > 1:
                self.logger.debug(f"WAV is stereo ({audio_data.shape[1]} channels), converting to mono.")
                audio_data = np.mean(audio_data, axis=1).astype(np.int16)
            elif audio_data.ndim > 1 and audio_data.shape[1] == 1:
                audio_data = audio_data[:,0]

            if audio_data.dtype != np.int16:
                self.logger.warning(f"WAV data type is {audio_data.dtype}, attempting conversion to int16.")
                if np.issubdtype(audio_data.dtype, np.floating):
                    audio_data = (audio_data * 32767).astype(np.int16)
                else:
                    self.logger.error("Unsupported WAV data type for direct conversion to int16.")
                    return

            raw_audio_bytes = audio_data.tobytes()

            if source_rate != TARGET_SAMPLE_RATE:
                self.logger.info(f"Resampling WAV from {source_rate}Hz to {TARGET_SAMPLE_RATE}Hz.")
                resampled_bytes = await resample_audio_chunk(raw_audio_bytes, source_rate, TARGET_SAMPLE_RATE, self.logger)
                if not resampled_bytes:
                    self.logger.error("Resampling local WAV file failed.")
                    return
                final_audio_bytes = resampled_bytes
            else:
                final_audio_bytes = raw_audio_bytes

            if not final_audio_bytes:
                self.logger.error("Final audio bytes for local WAV are empty.")
                return

            chunk_duration_ms = 10
            bytes_per_sample = 2 # for int16
            num_channels = 1 # Mono
            chunk_size_bytes = (TARGET_SAMPLE_RATE // (1000 // chunk_duration_ms)) * bytes_per_sample * num_channels
            self.logger.debug(f"Playing WAV: Total bytes {len(final_audio_bytes)}, chunk size {chunk_size_bytes} bytes.")

            if hasattr(self.audio_output, 'signal_start_of_speech'):
                 await self.audio_output.signal_start_of_speech()

            for i in range(0, len(final_audio_bytes), chunk_size_bytes):
                chunk = final_audio_bytes[i:i + chunk_size_bytes]
                if not chunk: break
                await self.play_audio_chunk(chunk)
                await asyncio.sleep(float(len(chunk)) / (TARGET_SAMPLE_RATE * bytes_per_sample * num_channels) * 0.8)

            if hasattr(self.audio_output, 'signal_end_of_speech'):
                await self.audio_output.signal_end_of_speech()

            await self._wait_for_output_completion()
            self.logger.info(f"Finished playing local WAV file: {file_path}")

        except sf.LibsndfileError as e:
            self.logger.error(f"Soundfile error reading WAV {file_path}: {e}")
        except Exception as e:
            self.logger.error(f"Error playing local WAV file {file_path}: {e}", exc_info=True)
    # --- End Local WAV Playback Method ---


# --- ElevenLabs WebSocket TTS --- 
async def stream_wake_phrase(
    text: str,
    voice_id: str,
    io_handler: LocalIOHandler,
    logger_instance: Optional[logging.Logger] = None,
    output_sample_rate: int = TARGET_SAMPLE_RATE # Use 48000 Hz
):
    """Streams TTS for a single wake phrase using ElevenLabs WebSocket.

    Connects, sends the text, streams PCM audio back, resamples, and sends 
    to the IO handler's playback queue.
    """
    effective_logger = logger_instance or logger
    effective_logger.info(f"Initiating WebSocket TTS for wake phrase: '{text[:30]}...'")

    if not ELEVENLABS_API_KEY:
        effective_logger.error("Cannot stream TTS: ELEVENLABS_API_KEY is not set.")
        return

    source_format = "pcm_24000" # Request 24kHz PCM from ElevenLabs
    source_rate = 24000

    ws_url = f"wss://api.elevenlabs.io/v1/text-to-speech/{voice_id}/stream-input?output_format={source_format}"

    wsapp: Optional[websocket.WebSocketApp] = None
    ws_thread: Optional[threading.Thread] = None
    loop = asyncio.get_running_loop()
    audio_processing_queue = asyncio.Queue(maxsize=100) # Queue for thread -> async communication
    connected_event = asyncio.Event()
    error_event = asyncio.Event()
    closed_event = asyncio.Event()
    received_final_message = asyncio.Event()
    processing_error: Optional[Exception] = None
    audio_task: Optional[asyncio.Task] = None

    # --- WebSocket Callbacks (Run in dedicated thread) ---
    def on_message_sync(wsapp_sync, message):
        # Put raw message bytes/str onto the async queue
        loop.call_soon_threadsafe(audio_processing_queue.put_nowait, message)

    def on_error_sync(wsapp_sync, error):
        nonlocal processing_error
        effective_logger.error(f"ElevenLabs WS Error: {error}")
        processing_error = Exception(f"WebSocket Error: {error}")
        loop.call_soon_threadsafe(error_event.set)
        # Also signal queue processor to stop
        loop.call_soon_threadsafe(audio_processing_queue.put_nowait, None)

    def on_close_sync(wsapp_sync, close_status_code, close_msg):
        effective_logger.info(f"ElevenLabs WS Closed: {close_status_code} {close_msg}")
        loop.call_soon_threadsafe(closed_event.set)
        # Also signal queue processor to stop
        loop.call_soon_threadsafe(audio_processing_queue.put_nowait, None)

    def on_open_sync(wsapp_sync):
        effective_logger.info("ElevenLabs WS Opened. Sending BOS and text...")
        try:
            # Send BOS
            bos_message = {
                "text": " ",
                "voice_settings": {"stability": 0.5, "similarity_boost": 0.75},
                "xi_api_key": ELEVENLABS_API_KEY,
            }
            wsapp_sync.send(json.dumps(bos_message))

            # Send the actual text
            text_message = {"text": text}
            wsapp_sync.send(json.dumps(text_message))

            # Send EOS
            eos_message = {"text": ""}
            wsapp_sync.send(json.dumps(eos_message))
            effective_logger.debug("Sent BOS, text, and EOS to ElevenLabs.")
            # Signal async side that connection is open and ready
            loop.call_soon_threadsafe(connected_event.set)
        except Exception as e:
            nonlocal processing_error
            effective_logger.error(f"Error during WS open/send: {e}")
            processing_error = e
            loop.call_soon_threadsafe(error_event.set)
            # Close connection on error during open
            try: wsapp_sync.close() 
            except: pass

    # --- Async Task to Process Audio Messages --- 
    async def process_audio_messages():
        nonlocal processing_error
        audio_chunks_processed = 0
        try:
            # Wait slightly longer for connection than default timeout?
            await asyncio.wait_for(connected_event.wait(), timeout=10.0)
            effective_logger.debug("Connection confirmed, starting audio processing loop.")

            while True:
                message = await audio_processing_queue.get()
                if message is None: # Stop signal
                    effective_logger.debug("Audio processing task received stop signal.")
                    break

                try:
                    data = json.loads(message)
                    if data.get("isFinal"):
                        effective_logger.info(f"Received ElevenLabs isFinal. Processed {audio_chunks_processed} audio chunks.")
                        received_final_message.set()
                        break # Exit loop

                    if "audio" in data and data["audio"]:
                        audio_chunks_processed += 1
                        raw_audio_data = binascii.a2b_base64(data["audio"])
                        if not raw_audio_data: continue

                        # Resample (will log errors internally)
                        resampled_data = await resample_audio_chunk(raw_audio_data, source_rate, output_sample_rate, effective_logger)

                        if resampled_data:
                            # Send to IO Handler playback queue
                            await io_handler.play_audio_chunk(resampled_data)
                        # else: resampling failed, logged in helper

                except json.JSONDecodeError:
                    effective_logger.warning(f"Ignoring invalid JSON from WS: {message[:100]}...")
                except Exception as e:
                    effective_logger.error(f"Error processing audio message: {e}", exc_info=True)
                    processing_error = e
                    error_event.set()
                    break # Stop processing on error
                finally:
                    audio_processing_queue.task_done()

        except asyncio.TimeoutError:
            effective_logger.error("Timeout waiting for ElevenLabs WebSocket connection.")
            processing_error = TimeoutError("ElevenLabs connection timeout")
            error_event.set()
        except asyncio.CancelledError:
            effective_logger.info("Audio processing task cancelled.")
        except Exception as e:
            effective_logger.error(f"Unexpected error in audio processing task: {e}", exc_info=True)
            if not processing_error: processing_error = e
            error_event.set()
        finally:
            effective_logger.debug("Audio processing task finished.")
            # Ensure final message event is set if loop finishes unexpectedly
            if not received_final_message.is_set(): received_final_message.set()

    # --- Main Execution --- 
    try:
        # Start WebSocket connection in thread
        websocket.enableTrace(False) # Disable noisy websocket trace logs
        wsapp = websocket.WebSocketApp(ws_url,
                                       on_open=on_open_sync,
                                       on_message=on_message_sync,
                                       on_error=on_error_sync,
                                       on_close=on_close_sync)
        
        ws_thread = threading.Thread(target=wsapp.run_forever, 
                                   kwargs={"ping_interval": 5, "ping_timeout": 4},
                                   daemon=True,
                                   name=f"WakePhraseTTS-{time.time():.0f}")
        ws_thread.start()

        # Start the async message processor
        audio_task = asyncio.create_task(process_audio_messages(), name="WakePhraseAudioProc")

        # Wait for completion (final message) or error or close
        # Use asyncio.wait for flexibility
        tasks_to_wait = [
            asyncio.create_task(received_final_message.wait(), name="WaitFinalMsg"),
            asyncio.create_task(error_event.wait(), name="WaitError"),
            asyncio.create_task(closed_event.wait(), name="WaitClosed")
        ]
        done, pending = await asyncio.wait(tasks_to_wait, return_when=asyncio.FIRST_COMPLETED, timeout=30.0) # Timeout for whole operation
        
        # Cancel pending wait tasks
        for task in pending: task.cancel()

        # Check results
        if error_event.is_set():
            effective_logger.error(f"Wake phrase TTS failed due to WebSocket/Processing error: {processing_error}")
        elif not received_final_message.is_set():
            effective_logger.warning("Wake phrase TTS finished (or timed out) without receiving final message.")
            # Check if closed event happened instead
            if closed_event.is_set():
                effective_logger.warning("WebSocket closed prematurely.")
            else: # Likely timeout
                 effective_logger.error("Timeout waiting for wake phrase TTS completion.")
        else:
            effective_logger.info("Wake phrase TTS stream completed successfully.")

    except Exception as e:
        effective_logger.error(f"Overall exception during wake phrase streaming: {e}", exc_info=True)
        error_event.set() # Signal error occurred
    finally:
        effective_logger.debug("Cleaning up wake phrase TTS resources...")
        # Ensure audio processing task is cancelled if still running
        if audio_task and not audio_task.done():
            audio_task.cancel()
            try: await asyncio.wait_for(audio_task, timeout=1.0)
            except (asyncio.TimeoutError, asyncio.CancelledError): pass
            
        # Close WebSocket
        if wsapp:
            try: wsapp.close()
            except Exception as e_close: effective_logger.debug(f"Error closing wsapp: {e_close}")
        # Join thread
        if ws_thread and ws_thread.is_alive():
            ws_thread.join(timeout=2.0)
            if ws_thread.is_alive(): 
                effective_logger.warning("Wake phrase TTS WebSocket thread did not exit cleanly.")
        effective_logger.debug("Wake phrase TTS cleanup complete.")
# --- End ElevenLabs WebSocket TTS ---


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
                    self.logger.info("Setting playback finished event after processing None sentinel.")
                    self.logger.info("Stopping sounddevice stream...")
                    await self._loop.run_in_executor(None, self._stream.stop) # wait for output to finish
                    self.logger.info("Sounddevice stream stopped. Setting _playback_finished_event.")
                    self._playback_finished_event.set()
                    self.logger.info("restarting stream...")
                    self._stream.start() # restart the stream
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
        self.logger.info("Requesting IO Handler to disable LiveKit input.") # Changed log
        self._is_paused = True
        try: 
            # await self.io_handler.request_livekit_pause() # REMOVED
            await self.io_handler.disable_livekit_input() # Use new method
        except Exception as e: self.logger.error(f"Error requesting pause from IO Handler: {e}")
        self.logger.info("Client set to Paused state.")

    async def resume(self):
        """Ensures the client is not paused and the LiveKit input path is enabled."""
        
        # Handle the client's internal paused state
        if self._is_paused:
            self.logger.info("Client was paused, setting internal state to resumed.")
            self._is_paused = False
        else:
            # This path might be taken if called right after starting
            self.logger.debug("Client resume called, but client was not internally marked as paused.")

        # Always ensure the IO handler's LiveKit input path is enabled when resume is called.
        # The enable_livekit_input method handles the check if it's already enabled.
        self.logger.info("Requesting IO Handler to enable LiveKit input path.")
        try: 
            await self.io_handler.enable_livekit_input() # Use new method
            self.logger.info("IO Handler LiveKit input path enabled (or already was).")
        except Exception as e: 
            self.logger.error(f"Error requesting enable_livekit_input from IO Handler: {e}")
        
        # Changed log to reflect action completion rather than just state setting
        self.logger.info("Client resume action finished.")


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
                except asyncio.TimeoutError: self.logger.debug(f"_forward_mic_to_livekit: Timeout waiting for chunk.") # DEBUG
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
        if track.kind == rtc.TrackKind.KIND_AUDIO and (participant.identity == ASSISTANT_IDENTITY or participant.identity == EXPECTED_ASSISTANT_IDENTITY):
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
    
    # Try to use uvloop for better performance on Linux/macOS
    try:
        import uvloop
        uvloop.install()
        logger.info("Using uvloop for improved performance")
    except ImportError:
        logger.info("uvloop not available, using standard event loop")

    if not LIVEKIT_URL:
        logger.error("LIVEKIT_URL not set in environment.")
        return # Return instead of exit(1)

    # --- State Machine & Control Variables ---
    loop = asyncio.get_running_loop()
    trigger_event_queue = asyncio.Queue()
    stop_event = asyncio.Event() # Global stop signal for main loop only
    current_state = AppState.IDLE
    client: Optional[LightberryLocalClient] = None
    first_idle_entry = True # Flag to track the first entry into IDLE state

    # --- Persistent Components --- #
    persistent_io_handler: Optional[LocalIOHandler] = None
    active_trigger_controller: Optional[TriggerController] = None
    ww_trigger: Optional[WakeWordTrigger] = None
    trinkey_trigger: Optional[TrinkeyTrigger] = None
    keyboard_trigger: Optional[MacKeyboardTrigger] = None

    # --- Instantiate Persistent IO Handler --- #
    try:
        persistent_io_handler = LocalIOHandler(
            loop=loop,
            logger_instance=logger,
            trigger_event_queue=trigger_event_queue
        )
    except Exception as e:
        logger.error(f"FATAL: Failed to initialize LocalIOHandler: {e}", exc_info=True)
        return

    # --- Instantiate Trigger Controllers --- #
    try:
        # Always create WakeWordTrigger if IO Handler succeeded
        ww_trigger = WakeWordTrigger(
            loop=loop,
            event_queue=trigger_event_queue,
            io_handler=persistent_io_handler
        )
        logger.info("WakeWordTrigger initialized.")

        # Try platform-specific triggers
        if platform.system() == "Darwin":
            logger.info("Platform is Darwin, attempting MacKeyboardTrigger.")
            keyboard_trigger = MacKeyboardTrigger(loop, trigger_event_queue)
            if not hasattr(keyboard_trigger, '_listener_loop') or not _pynput_available:
                logger.warning("MacKeyboardTrigger unavailable or pynput failed. Trying Trinkey.")
                keyboard_trigger = None
                trinkey_trigger = TrinkeyTrigger(loop, trigger_event_queue, ADAFRUIT_VID, TRINKEY_PID, TRINKEY_KEYCODE)
                logger.info("TrinkeyTrigger initialized as fallback.")
            else:
                logger.info("MacKeyboardTrigger initialized successfully.")
        else:
            logger.info("Platform is not Darwin, attempting TrinkeyTrigger.")
            trinkey_trigger = TrinkeyTrigger(loop, trigger_event_queue, ADAFRUIT_VID, TRINKEY_PID, TRINKEY_KEYCODE)
            logger.info("TrinkeyTrigger initialized.")

        # --- Select Active Trigger --- #
        if ww_trigger:
            active_trigger_controller = ww_trigger
        elif keyboard_trigger and _pynput_available:
            active_trigger_controller = keyboard_trigger
        elif trinkey_trigger:
            active_trigger_controller = trinkey_trigger
        else:
            active_trigger_controller = None

        if active_trigger_controller:
            logger.info(f"Selected {active_trigger_controller.__class__.__name__} as the active trigger controller.")
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
        # Declare first_idle_entry as nonlocal here if it's modified within the loop
        # However, since first_idle_entry is defined in the same scope as the while loop,
        # nonlocal is not strictly needed here unless main itself was nested further.
        # The linter might be overly cautious. Let's ensure correct scoping for modification.
        # For simplicity and directness, we ensure it's part of the main's direct scope.
        # The issue was likely the placement of the nonlocal keyword inside the if block.

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
                    # Play startup sound only on the first entry to IDLE
                    # nonlocal first_idle_entry # This was the problematic placement
                    if first_idle_entry:
                        logger.info("Playing startup sound (first IDLE entry)...")
                        await persistent_io_handler.play_local_wav_file("soft_gong_short.wav")
                        first_idle_entry = False # Reset flag after playing

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
                except Exception as e:
                    logger.error(f"Error starting persistent components/listeners: {e}. Stopping application.", exc_info=True)
                    stop_event.set() ; continue

                print(f"Waiting for any trigger...", flush=True)
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
                    await asyncio.sleep(1)

            elif current_state == AppState.CONNECTING:
                print(">>> Current State: CONNECTING <<<", flush=True)
                logger.info("Entered CONNECTING state.")
                token, room_name = None, None
                try:
                    device_id = get_or_create_device_id()
                    username = "recDNsqLQ9CKswdVJ" # TODO: Make configurable
                    logger.info(f"Fetching credentials for Device ID: {device_id}, User: {username}")
                    # Fetch credentials, now including wake phrase and voice ID
                    token, room_name, wake_phrase, voice_id = await get_credentials(device_id, username) # <<< MODIFY THIS LINE
                    if token and room_name:
                        logger.info("Credentials obtained. Creating client...")
                        client = LightberryLocalClient(url=LIVEKIT_URL, token=token, room_name=room_name,
                                                   loop=loop, trigger_event_queue=trigger_event_queue,
                                                   io_handler=persistent_io_handler)
                        await client.start()
                        logger.info("Client started successfully.")
                        # --- Play Wake Phrase (WebSocket) ---
                        await stream_wake_phrase(wake_phrase, voice_id, persistent_io_handler, logger_instance=logger) # <<< REPLACE OLD CALL WITH THIS
                        # ----------------------------------
                        current_state = AppState.ACTIVE
                        await client.resume() # set livekit input path to enabled
                        logger.debug("Clearing trigger queue after activating client...")
                        while not trigger_event_queue.empty():
                            try: trigger_event_queue.get_nowait(); trigger_event_queue.task_done()
                            except asyncio.QueueEmpty: break
                            except Exception: pass
                        logger.debug("Trigger queue cleared.")
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
                    elif event == TriggerEvent.ASSISTANT_ACTIVITY_TIMEOUT:
                        logger.info("Assistant activity timeout. Stopping client and returning to IDLE.")
                        print("Assistant timed out. Returning to IDLE...", flush=True)
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
                    elif event == TriggerEvent.ASSISTANT_ACTIVITY_TIMEOUT:
                        logger.info("Assistant activity timeout while PAUSED. Stopping client and returning to IDLE.")
                        print("Assistant timed out. Returning to IDLE...", flush=True)
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
        stop_event.set()
    finally:
        logger.info("Main function initiating final shutdown...")
        if current_state != AppState.STOPPING:
            logger.warning(f"Main loop exited unexpectedly from state {current_state.name}. Forcing STOPPING.")

        async def _perform_final_cleanup(client_instance, trigger_controller_instance, io_handler_instance):
            if client_instance:
                logger.info("Performing final client stop...")
                try:
                    await client_instance.stop() # This will call io_handler.signal_client_disconnected -> _cancel_inactivity_timer
                except asyncio.TimeoutError:
                    logger.error("Client stop timed out during final cleanup.")
                except Exception as e:
                    logger.error(f"Error during final client stop: {e}", exc_info=True)
            
            # Explicitly stop IO Handler and its timer if not done through client stop
            if io_handler_instance:
                logger.info("Performing final IO Handler stop (ensures timer cancellation)...")
                try:
                    await io_handler_instance.stop() # This calls _cancel_inactivity_timer
                except Exception as e:
                    logger.error(f"Error during final IO Handler stop: {e}", exc_info=True)

            if trigger_controller_instance:
                logger.info("Performing final trigger listener stop...")
                try:
                    trigger_controller_instance.stop_listening()
                    await asyncio.sleep(0.5) # Allow listeners to close
                except Exception as e:
                    logger.error(f"Error during final trigger stop: {e}")
            logger.info("Final cleanup steps completed (within timeout check).")

        shutdown_timeout = 10.0
        try:
            logger.info(f"Attempting graceful shutdown (timeout: {shutdown_timeout}s)...")
            await asyncio.wait_for(_perform_final_cleanup(client, active_trigger_controller, persistent_io_handler), timeout=shutdown_timeout)
            logger.info("Graceful shutdown completed or timed out okay.")
        except asyncio.TimeoutError:
            logger.error(f"Graceful shutdown timed out after {shutdown_timeout}s. Forcing exit.")
            os._exit(1)
        except Exception as final_e:
            logger.error(f"Exception during final shutdown sequence: {final_e}", exc_info=True)
            logger.error("Attempting force exit after shutdown exception.")
            os._exit(1)

        logger.info("Main function shutdown complete.")

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("Keyboard interrupt received. Shutting down...")
    logger.info("Client execution finished.")
