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
import pathlib # Added pathlib for path handling

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

# --- Add Audio Loading Function ---
def load_raw_audio(filepath: Union[str, pathlib.Path]) -> Optional[bytes]:
    """Loads raw audio data from a file."""
    try:
        path = pathlib.Path(filepath)
        if not path.is_file():
             logger.error(f"Raw audio file not found: {filepath}")
             return None
        with open(path, "rb") as f:
             audio_data = f.read()
        logger.info(f"Successfully loaded raw audio file: {filepath} ({len(audio_data)} bytes)")
        return audio_data
    except Exception as e:
        logger.error(f"Error loading raw audio file {filepath}: {e}", exc_info=True)
        return None
# --- End Audio Loading Function ---

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


class AudioIOHandler(IOHandlerInterface): # Implements IOHandlerInterface for Audio
    """Manages interaction with audio input/output components and state, including input pausing."""
    def __init__(self,
                 audio_input: AudioInputInterface,
                 audio_output: AudioOutputInterface,
                 input_processor: InputProcessorInterface, # Keep input_processor param
                 logger_instance: Optional['Logger'] = None):
        self.audio_input = audio_input
        self.audio_output = audio_output
        self.input_processor = input_processor
        self.logger = logger_instance or logger
        self._loop = asyncio.get_running_loop()
        self._user_audio_buffer_for_saving = asyncio.Queue(maxsize=500)
        self._playback_finished_event: Optional[asyncio.Event] = None

        # New members for input pausing
        self._internal_input_queue = asyncio.Queue(maxsize=200) # Intermediate queue
        self._pause_input_event = asyncio.Event() # Event to control pausing
        self._pause_input_event.set() # Start unpaused
        self._transfer_input_task: Optional[asyncio.Task] = None # Task for the transfer loop

        self.logger.debug("AudioIOHandler initialized with input pausing.")
        if hasattr(self.audio_output, 'get_playback_finished_event'):
            event = self.audio_output.get_playback_finished_event()
            if isinstance(event, asyncio.Event):
                self._playback_finished_event = event
                self.logger.info("IOHandler obtained playback finished event.")
            else:
                self.logger.warning("playback_finished_event from audio_output is not an asyncio.Event")
        else:
            self.logger.warning("Audio output component does not have 'get_playback_finished_event' method.")

    def get_input_queue(self) -> asyncio.Queue:
        """Returns the internal, pausable audio chunk queue for the input processor."""
        # Return the new internal queue instead of the direct one
        if not self._internal_input_queue:
            self.logger.error("IOHandler: Internal input queue not initialized!")
            # Raise an error or return a dummy to prevent downstream None errors
            raise RuntimeError("IOHandler internal state error: input queue missing")
        return self._internal_input_queue

    async def send_output_chunk(self, chunk: bytes):
        """Sends an audio chunk to the output component."""
        await self.audio_output.send_audio_chunk(chunk)

    async def signal_start_of_speech(self):
        """Signals the start of assistant speech, pauses input transfer, and clears the save buffer."""
        await self.audio_output.signal_start_of_speech()
        # Pause the input transfer loop
        self._pause_input_event.clear()
        self.logger.debug("IOHandler: Input transfer paused.")
        # Clear the save buffer when assistant starts speaking
        self.logger.debug("IOHandler: Assistant started speaking, clearing save buffer.")
        await self._empty_queue(self._user_audio_buffer_for_saving)

    async def signal_end_of_speech(self):
        """Signals the end of assistant speech, waits for playback, cleans input buffers, and resumes input transfer."""
        # Signal end to the component (which handles its internal queue joining)
        await self.audio_output.signal_end_of_speech()
        # Wait for the component to signal completion via the event
        await self._wait_for_output_completion()
        # Clean input buffers after playback is fully finished
        self.logger.debug("IOHandler: Playback finished, clearing input buffers.")
        await self._clear_input_buffers()
        # Resume the input transfer loop *after* buffers are cleared
        # NOTE: Resuming is now handled by LightberryLocalClient.resume() based on trigger state
        # self._pause_input_event.set()
        # self.logger.debug("IOHandler: Input transfer resumed.")
        self.logger.debug("IOHandler: Input transfer remains paused until explicitly resumed.")


    async def _wait_for_output_completion(self):
        """(Private) Waits for the playback finished event from the output component."""
        if self._playback_finished_event:
            try:
                self.logger.debug("IOHandler waiting for playback finished event...")
                # Wait until the event is set by the audio output component
                await asyncio.wait_for(self._playback_finished_event.wait(), timeout=60.0) # Add timeout
                self.logger.debug("IOHandler: Playback finished event received.")
            except asyncio.TimeoutError:
                self.logger.warning("IOHandler: Timeout waiting for playback finished event.")
            except Exception as e:
                self.logger.error(f"IOHandler: Error waiting for playback finished event: {e}")
        else:
            self.logger.warning("IOHandler: Playback finished event not available, cannot wait.")
            await asyncio.sleep(0.5) # Fallback delay

    def is_output_active(self) -> bool:
        """Checks if the audio output component is currently speaking."""
        return self.audio_output.is_speaking

    def should_process_input(self) -> bool:
        """Checks if the main processing loop should proceed (e.g., output not active)."""
        # Currently just checks if output is active, can be expanded
        return not self.is_output_active()

    async def buffer_chunk_for_saving(self, chunk: bytes):
        """Adds a user audio chunk to the save buffer."""
        try:
            await asyncio.wait_for(self._user_audio_buffer_for_saving.put(chunk), timeout=0.05)
        except asyncio.TimeoutError:
            self.logger.warning("IOHandler: Timeout putting chunk into save buffer queue.")
        except asyncio.QueueFull:
            self.logger.warning("IOHandler: User audio save buffer full, dropping chunk.")
        except Exception as e:
            self.logger.error(f"IOHandler: Error buffering chunk for saving: {e}")

    async def save_buffered_audio(self, transcript_for_filename: str):
        """Saves the currently buffered user audio chunks to a timestamped file."""
        if self._user_audio_buffer_for_saving.empty():
            self.logger.debug("IOHandler: No user audio chunks buffered for saving.")
            return

        chunks_to_save = []
        while not self._user_audio_buffer_for_saving.empty():
            try:
                chunk = self._user_audio_buffer_for_saving.get_nowait()
                chunks_to_save.append(chunk)
                self._user_audio_buffer_for_saving.task_done()
            except asyncio.QueueEmpty:
                break
            except Exception as e:
                self.logger.error(f"IOHandler: Error getting chunk from save buffer: {e}")
                return # Avoid partial save

        if not chunks_to_save:
            self.logger.warning("IOHandler: Attempted to save user audio, but chunk list was empty after retrieval.")
            return

        try:
            timestamp = time.strftime("%Y%m%d_%H%M%S")
            safe_transcript = "".join(c for c in transcript_for_filename if c.isalnum() or c in " _-").strip()[:50]
            filename = f"recordings/{timestamp}_{safe_transcript}.raw"

            self.logger.info(f"IOHandler: Saving {len(chunks_to_save)} user audio chunks to {filename}")
            os.makedirs("recordings", exist_ok=True)
            audio_data = b"".join(chunks_to_save)
            with open(filename, "wb") as f:
                f.write(audio_data)
            self.logger.debug(f"IOHandler: Successfully saved user audio to {filename}")

        except Exception as e:
            self.logger.error(f"IOHandler: Failed to save user audio chunks: {e}", exc_info=True)

    async def discard_buffered_audio(self):
        """Discards the current content of the user audio save buffer."""
        if not self._user_audio_buffer_for_saving.empty():
            self.logger.debug("IOHandler: Discarding buffered audio...")
            await self._empty_queue(self._user_audio_buffer_for_saving)
            self.logger.debug("IOHandler: Buffered audio discarded.")

    async def _clear_input_buffers(self):
        """(Private) Clears source input queue, internal queue, save buffer."""
        self.logger.debug("IOHandler: Clearing input buffers...")
        # 1. Clear save buffer
        await self._empty_queue(self._user_audio_buffer_for_saving)
        self.logger.debug("IOHandler: Save buffer cleared.")

        # 2. Clear internal input queue
        await self._empty_queue(self._internal_input_queue)
        self.logger.debug("IOHandler: Internal input queue cleared.")

        # 3. Clear source queue in audio input component
        try:
            input_queue = self.audio_input.get_audio_chunk_queue()
            await self._empty_queue(input_queue)
            self.logger.debug("IOHandler: Source audio input queue cleared.")
        except AttributeError:
            self.logger.warning("IOHandler: Cannot clear source input queue - audio_input lacks get_audio_chunk_queue.")
        except Exception as e:
            self.logger.error(f"IOHandler: Error clearing source input component queue: {e}")

        self.logger.debug("IOHandler: Input buffers cleared.")


    async def _empty_queue(self, q: asyncio.Queue):
        """Helper to quickly empty an asyncio queue."""
        # Note: This is duplicated from StreamingClient, consider a utils file?
        while not q.empty():
            try:
                item = q.get_nowait()
                q.task_done()
                # Log discarded item type/size for debugging if needed
                # self.logger.debug(f"Discarding item from queue {q}: {type(item)}")
            except asyncio.QueueEmpty:
                break
            except Exception as e:
                self.logger.error(f"IOHandler: Error emptying queue {q}: {e}")
                break # Avoid potential infinite loop

    # Add start/stop methods to conform to interface (might be no-ops)
    async def start(self):
        self.logger.debug("AudioIOHandler start called. Starting underlying components and transfer task...")
        # Ensure input starts paused until explicitly resumed by client logic
        self._pause_input_event.clear()
        await self.audio_input.start()
        await self.audio_output.start()
        # Start the input transfer task
        if self._transfer_input_task is None or self._transfer_input_task.done():
            self._transfer_input_task = asyncio.create_task(self._transfer_input_loop(), name="IOHandlerTransferInput")
            self.logger.info("AudioIOHandler: Started input transfer task.")
        else:
            self.logger.warning("AudioIOHandler: Input transfer task already running?")


    async def stop(self):
        self.logger.debug("AudioIOHandler stop called. Stopping transfer task and underlying components...")
        # Stop underlying components first
        tasks = []
        if self.audio_input and hasattr(self.audio_input, 'stop'):
            tasks.append(asyncio.create_task(self.audio_input.stop(), name="AudioInputStop"))
        if self.audio_output and hasattr(self.audio_output, 'stop'):
            tasks.append(asyncio.create_task(self.audio_output.stop(), name="AudioOutputStop"))
        if tasks:
             stop_results = await asyncio.gather(*tasks, return_exceptions=True)
             self.logger.debug(f"AudioIOHandler: Underlying components stop results: {stop_results}")

        # Ensure the pause event is set so the transfer task doesn't block indefinitely during shutdown
        self._pause_input_event.set()

        # Stop the internal transfer task
        transfer_task = self._transfer_input_task
        self._transfer_input_task = None
        if transfer_task and not transfer_task.done():
            self.logger.debug("AudioIOHandler: Cancelling input transfer task...")
            transfer_task.cancel()
            try:
                await asyncio.wait_for(transfer_task, timeout=2.0)
                self.logger.debug("AudioIOHandler: Input transfer task cancelled successfully.")
            except asyncio.TimeoutError:
                self.logger.warning("AudioIOHandler: Timeout waiting for input transfer task to cancel.")
            except asyncio.CancelledError:
                self.logger.debug("AudioIOHandler: Input transfer task already cancelled.")
            except Exception as e:
                self.logger.error(f"AudioIOHandler: Error stopping input transfer task: {e}")

        self.logger.info("AudioIOHandler stopped.")

    async def _transfer_input_loop(self):
        """(Private) Loop transferring audio from source input queue to internal queue, respecting pause event."""
        self.logger.info("AudioIOHandler: Input transfer loop started.")
        source_queue = None
        try:
            source_queue = self.audio_input.get_audio_chunk_queue()
        except Exception as e:
             self.logger.error(f"IOHandler Transfer Loop: Failed to get source input queue: {e}. Exiting.")
             return # Cannot proceed without source queue

        while True:
            try:
                # Wait until input is NOT paused
                await self._pause_input_event.wait()

                # Get chunk from the actual source queue
                # Use timeout to periodically check for cancellation
                chunk_tuple = await asyncio.wait_for(source_queue.get(), timeout=0.5)

                # Put chunk tuple into the internal queue
                try:
                    # Use a short timeout for the internal queue put
                    await asyncio.wait_for(self._internal_input_queue.put(chunk_tuple), timeout=0.1)
                except asyncio.QueueFull:
                    self.logger.warning("IOHandler Transfer Loop: Internal input queue full, discarding chunk.")
                    # Need to mark task_done on internal queue if we add tracking there
                except asyncio.TimeoutError:
                    self.logger.warning("IOHandler Transfer Loop: Timeout putting chunk into internal queue.")
                finally:
                     source_queue.task_done() # Mark as done on the source queue regardless

            except asyncio.TimeoutError:
                # Normal timeout while waiting for source queue, check if stopping
                # Cancellation is handled by the CancelledError below
                continue
            except asyncio.CancelledError:
                self.logger.info("IOHandler Transfer Loop: Cancelled.")
                break
            except Exception as e:
                self.logger.error(f"IOHandler Transfer Loop: Error: {e}", exc_info=True)
                # Avoid tight loop on unexpected error
                await asyncio.sleep(0.1)

        self.logger.info("AudioIOHandler: Input transfer loop finished.")


# --- PyAudio Class (Unchanged) ---
class PyAudioMicrophoneInput(AudioInputInterface):
    """AudioInputInterface that reads microphone audio using PyAudio and puts chunks onto a queue."""
    SILENCE_THRESHOLD = 20
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
                    if audio_chunk:
                        audio_int16 = np.frombuffer(audio_chunk, dtype=np.int16)
                        if audio_int16.size > 0: 
                            energy = np.mean(np.abs(audio_int16)) + 1e-9 # Add epsilon for safety
                            if energy < self.SILENCE_THRESHOLD:
                                self.logger.debug(f"PyAudio loop: Read chunk with low energy, skipping. Energy: {energy}")
                                continue
                    #self.logger.debug(f"PyAudio loop: Read chunk of size {len(audio_chunk)}") # DEBUG

                    if not self._is_running: break # Check running flag after blocking read

                    # --- Add timing and put chunk (with metadata) onto queue ---
                    chunk_id = -1 # Default value if ID callback fails
                    t_capture = 1
                    try:
                        if self._get_chunk_id:
                            chunk_id = self._get_chunk_id()
                        else:
                            self.logger.warning("Chunk ID callback missing, cannot get ID.")

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


# --- LocalIOHandler Modifications ---
class LocalIOHandler(AudioIOHandler):
    # Define constants for silence detection
    SILENCE_THRESHOLD = 50  # Amplitude threshold (adjust based on mic sensitivity)
    SILENCE_TIMEOUT_S = 10.0 # Seconds of silence before pausing input

    def __init__(self,
                 audio_input: AudioInputInterface,
                 audio_output: AudioOutputInterface,
                 client_instance: 'LightberryLocalClient', # Added client instance
                 logger_instance: Optional[logging.Logger] = None,
                 trigger_event_queue: Optional[asyncio.Queue] = None): # Add trigger queue
        # Pass None for input_processor as it's not used here
        super().__init__(audio_input, audio_output, None, logger_instance)
        self._client = client_instance # Store client instance
        self._trigger_event_queue = trigger_event_queue # Store trigger queue
        # Pass client callbacks to the audio input instance if it supports them
        if hasattr(self.audio_input, '_log_timing') and hasattr(self.audio_input, '_get_chunk_id'):
            self.logger.info("Passing timing/chunk ID callbacks to PyAudioMicrophoneInput.")
            # Ensure the attributes exist on the client before assigning
            if hasattr(self._client, '_log_timing') and hasattr(self._client, '_get_next_chunk_id'):
                self.audio_input._log_timing = self._client._log_timing
                self.audio_input._get_chunk_id = self._client._get_next_chunk_id
            else:
                 self.logger.error("Client instance is missing required timing/chunk ID methods.")
        else:
            self.logger.warning("Audio input interface does not support timing/chunk ID callbacks.")

        self.internal_output_queue = asyncio.Queue(maxsize=200) # Queue for LiveKit -> Speaker transfer
        self._transfer_output_task: Optional[asyncio.Task] = None # Task for the transfer loop

        # --- New members for integrated silence monitoring ---
        self.is_monitoring_silence: bool = False
        self.silence_start_time: Optional[float] = None
        # --- End new members ---

    # --- Override _transfer_input_loop to add timing AND silence monitoring ---
    async def _transfer_input_loop(self):
        """(Overridden) Continuously transfers audio chunks from input source to internal queue, adding timing data and handling silence monitoring."""
        source_queue = None
        try:
            source_queue = self.audio_input.get_audio_chunk_queue()
        except AttributeError:
             self.logger.error("IOHandler: Audio input object missing 'get_audio_chunk_queue'. Input transfer cannot start.")
             return
        except Exception as e:
            self.logger.error(f"IOHandler: Error getting source audio queue: {e}. Input transfer cannot start.")
            return

        if source_queue is None:
            self.logger.error("IOHandler: Could not get source audio queue. Input transfer cannot start.")
            return

        self.logger.info("IOHandler: Starting input transfer loop with timing.")
        while True:
            try:
                # Wait if input is paused (controlled externally OR by silence monitor)
                await self._pause_input_event.wait()

                # Expecting (chunk_data, chunk_id, t_capture) from PyAudioMicrophoneInput
                chunk_data, chunk_id, t_capture = await asyncio.wait_for(source_queue.get(), timeout=0.5)
                t_get_mic_q = time.time()
                source_queue.task_done()

                # --- Integrated Silence Monitoring Logic ---
                should_forward_chunk = True # Default to forwarding
                if self.is_monitoring_silence:
                    try:
                        energy = np.mean(np.abs(np.frombuffer(chunk_data, dtype=np.int16))) + 1e-9
                    except Exception as e:
                        self.logger.error(f"Transfer Loop: Error calculating energy for chunk {chunk_id}: {e}")
                        energy = self.SILENCE_THRESHOLD # Treat as silence on error

                    if energy > self.SILENCE_THRESHOLD:
                        # Speech detected, reset silence timer
                        if self.silence_start_time is not None:
                            self.logger.debug("Transfer Loop: Speech detected, resetting silence timer.")
                        self.silence_start_time = None
                    else:
                        # Silence detected
                        if self.silence_start_time is None:
                            self.logger.debug("Transfer Loop: Silence detected, starting timer.")
                            self.silence_start_time = time.time()
                        else:
                            # Timer running, check duration
                            elapsed_silence = time.time() - self.silence_start_time
                            if elapsed_silence > self.SILENCE_TIMEOUT_S:
                                self.logger.info(f"Transfer Loop: {self.SILENCE_TIMEOUT_S}s silence detected. Pausing input transfer and signaling AUTO_PAUSE.")
                                print("Auto-pausing input due to silence...", flush=True)
                                self.is_monitoring_silence = False # Turn off monitoring
                                self.silence_start_time = None
                                self._pause_input_event.clear() # PAUSE this loop
                                # --- Emit AUTO_PAUSE event --- 
                                if self._trigger_event_queue:
                                     try:
                                          self._trigger_event_queue.put_nowait(TriggerEvent.AUTO_PAUSE)
                                     except asyncio.QueueFull:
                                          self.logger.warning("IOHandler: Trigger queue full, cannot emit AUTO_PAUSE.")
                                # ---------------------------
                                should_forward_chunk = False # Don't forward this silent chunk
                                continue # Restart loop (will block on wait() until resumed)
                # --- End Integrated Silence Monitoring Logic ---

                # Forward the chunk if not paused by silence timeout
                if should_forward_chunk:
                    # Put (chunk_data, chunk_id, put_time) into internal queue
                    t_put_internal_q = time.time()
                    item_for_internal_q = (chunk_data, chunk_id, t_put_internal_q)

                    # Log timing if available
                    if hasattr(self._client, '_log_timing'):
                        mic_q_wait = t_get_mic_q - t_capture
                        self._client._log_timing('get_mic_q', chunk_id, timestamp=t_get_mic_q, wait_time=mic_q_wait)
                        self._client._log_timing('put_internal_q', chunk_id, timestamp=t_put_internal_q)

                    # Add to internal queue (used by LightberryLocalClient._forward_mic_to_livekit)
                    try:
                        await asyncio.wait_for(self._internal_input_queue.put(item_for_internal_q), timeout=0.1)
                    except asyncio.TimeoutError:
                        self.logger.warning(f"IOHandler: Timeout putting chunk {chunk_id} into internal queue. Queue possibly full.")
                        if hasattr(self._client, '_log_timing'): self._client._log_timing('timeout_put_internal_q', chunk_id, timestamp=time.time())
                        # continue # Let loop continue normally
                    except asyncio.QueueFull: # Should be caught by TimeoutError with asyncio.Queue
                        self.logger.warning(f"IOHandler: Internal queue full, dropping chunk {chunk_id}.")
                        if hasattr(self._client, '_log_timing'): self._client._log_timing('discard_internal_q_full', chunk_id, timestamp=time.time())
                        # continue # Let loop continue normally

            except asyncio.TimeoutError:
                # Timeout getting chunk from source queue - check silence timer if monitoring
                if self.is_monitoring_silence and self.silence_start_time is not None:
                    elapsed_silence = time.time() - self.silence_start_time
                    if elapsed_silence > self.SILENCE_TIMEOUT_S:
                        self.logger.info(f"Transfer Loop: {self.SILENCE_TIMEOUT_S}s silence detected (source queue empty). Pausing input transfer and signaling AUTO_PAUSE.")
                        print("Auto-pausing input due to silence...", flush=True)
                        self.is_monitoring_silence = False # Turn off monitoring
                        self.silence_start_time = None
                        self._pause_input_event.clear() # PAUSE this loop
                        # --- Emit AUTO_PAUSE event --- 
                        if self._trigger_event_queue:
                             try:
                                  self._trigger_event_queue.put_nowait(TriggerEvent.AUTO_PAUSE)
                             except asyncio.QueueFull:
                                  self.logger.warning("IOHandler: Trigger queue full, cannot emit AUTO_PAUSE.")
                        # ---------------------------
                        # No chunk to forward, just continue loop (will block on wait())
                continue # Continue loop on normal timeout or if silence timer hasn't expired

            except asyncio.CancelledError:
                self.logger.info("IOHandler Input Transfer Loop: Cancelled.")
                break
            except Exception as e:
                self.logger.error(f"IOHandler Input Transfer Loop: Error processing audio chunk: {e}", exc_info=True)
                await asyncio.sleep(0.1) # Avoid tight loop
        self.logger.info("IOHandler: Input transfer loop finished.")

    # --- Override _clear_input_buffers --- (No changes needed here)
    async def _clear_input_buffers(self):
        """(Overridden) Clears source input queue, internal queue, save buffer."""
        self.logger.debug("IOHandler: Clearing input buffers...")
        # 1. Clear save buffer
        await self._empty_queue(self._user_audio_buffer_for_saving)
        self.logger.debug("IOHandler: Save buffer cleared.")

        # 2. Clear internal input queue (contains tuples)
        await self._empty_queue(self._internal_input_queue)
        self.logger.debug("IOHandler: Internal input queue cleared.")

        # 3. Clear source queue in audio input component (contains tuples)
        try:
            has_method = hasattr(self.audio_input, 'get_audio_chunk_queue')
            if has_method:
                 input_queue = self.audio_input.get_audio_chunk_queue()
                 await self._empty_queue(input_queue)
                 self.logger.debug("IOHandler: Source audio input queue cleared.")
            else:
                 self.logger.warning("IOHandler: Cannot clear source input queue - audio_input lacks get_audio_chunk_queue method.")
        except AttributeError: # Should be caught by hasattr now
             self.logger.warning("IOHandler: Cannot clear source input queue - audio_input lacks get_audio_chunk_queue.")
        except Exception as e:
             self.logger.error(f"IOHandler: Error clearing source input component queue: {e}")

        self.logger.debug("IOHandler: Input buffers cleared.")

    # --- Override signal_start/end_of_speech ---
    async def signal_start_of_speech(self):
        """(Overridden) Signals start of speech, stops silence monitor, pauses input transfer."""
        # Stop silence monitoring when assistant starts speaking
        self.is_monitoring_silence = False
        self.silence_start_time = None
        self.logger.debug("IOHandler: Assistant started speaking, silence monitoring stopped.")

        await self.audio_output.signal_start_of_speech()
        # Pause the input transfer loop (mic -> internal queue)
        self._pause_input_event.clear()
        self.logger.debug("IOHandler: Mic input transfer PAUSED due to assistant speech.")
        # Clear the save buffer
        self.logger.debug("IOHandler: Clearing save buffer.")
        await self._empty_queue(self._user_audio_buffer_for_saving)

    async def signal_end_of_speech(self):
        """(Overridden) Signals end of speech, waits for playback, cleans buffers, saves timing, RESUMES input, starts silence monitoring."""
        await self.audio_output.signal_end_of_speech()
        await self._wait_for_output_completion()

        self.logger.info("IOHandler: Playback finished, clearing input buffers.")
        await self._clear_input_buffers()

        # Save Timing Data
        if False and hasattr(self._client, 'save_timing_data'):
            self.logger.info("IOHandler: Saving collected timing data...")
            self._client.save_timing_data()
            self.logger.info("IOHandler: Timing data saved.")
        else:
            self.logger.warning("IOHandler: Client missing save_timing_data method.")

        # Resume input and start silence monitoring
        self.logger.info("IOHandler: Resuming input and enabling silence monitoring.")
        self.is_monitoring_silence = True
        self.silence_start_time = None # Reset timer start
        self._pause_input_event.set() # RESUME input transfer loop

    # --- send_output_chunk --- (No changes needed here)
    async def send_output_chunk(self, chunk: bytes):
        """(Overridden) Puts chunk into the internal output queue for the transfer loop."""
        # This queue is now used for LiveKit -> Speaker transfer
        try:
            await asyncio.wait_for(self.internal_output_queue.put(chunk), timeout=0.1)
        except asyncio.TimeoutError:
             self.logger.warning("IOHandler: Timeout putting chunk into internal *output* queue.")
        except asyncio.QueueFull:
             self.logger.warning("IOHandler: Internal *output* queue full, dropping chunk.")
        except Exception as e:
            self.logger.error(f"IOHandler: Error sending chunk to internal output queue: {e}")

    # --- _transfer_output_loop --- (No changes needed here, uses class SILENCE_THRESHOLD)
    async def _transfer_output_loop(self):
        """
        (Overridden) Transfers audio from internal output queue (filled by LiveKit) to the actual speaker output component.
        Handles start/end of speech detection based on chunk content.
        """
        receiving_output = False # Local state for this loop
        silence_count = 0
        silence_frames_needed = 50 # Frames of silence needed to trigger end of speech

        self.logger.info("IOHandler: Starting output transfer loop (LiveKit -> Speaker).")
        while True:
            try:
                # Wait for a chunk from the internal output queue
                chunk = await asyncio.wait_for(self.internal_output_queue.get(), timeout=0.2)
                self.internal_output_queue.task_done()

                # Skip processing if chunk is None (e.g., sentinel during shutdown)
                if chunk is None:
                    continue

                # Simple energy-based VAD
                try:
                    # Use a small epsilon to avoid issues with pure silence
                    energy = np.mean(np.abs(np.frombuffer(chunk, dtype=np.int16))) + 1e-9
                except Exception as e:
                    self.logger.error(f"IOHandler: Error calculating energy for output chunk: {e}")
                    energy = self.SILENCE_THRESHOLD # Treat as silence on error, use class constant

                if not receiving_output and energy > self.SILENCE_THRESHOLD:
                    receiving_output = True
                    silence_count = 0
                    self.logger.debug("IOHandler Output: Detected start of assistant speech.")
                    await self.signal_start_of_speech()
                elif receiving_output and energy < self.SILENCE_THRESHOLD:
                    silence_count += 1
                    if silence_count > silence_frames_needed:
                        receiving_output = False
                        silence_count = 0
                        self.logger.debug("IOHandler Output: Detected end of assistant speech (silence).")
                        await self.signal_end_of_speech()
                elif receiving_output and energy >= self.SILENCE_THRESHOLD:
                    silence_count = 0

                if self.audio_output.is_speaking:
                    await self.audio_output.send_audio_chunk(chunk)

            except asyncio.TimeoutError:
                if receiving_output:
                    receiving_output = False
                    silence_count = 0
                    self.logger.info("IOHandler Output: Detected end of speech due to timeout.")
                    if self.audio_output.is_speaking:
                         await self.signal_end_of_speech()
                continue
            except asyncio.CancelledError:
                self.logger.info("IOHandler Transfer Output Loop: Cancelled.")
                break
            except Exception as e:
                self.logger.error(f"IOHandler Transfer Output Loop: Error: {e}", exc_info=True)
                receiving_output = False
                silence_count = 0
                await asyncio.sleep(0.1)

        self.logger.info("IOHandler: Output transfer loop finished.")

    # --- start --- (No changes needed here)
    async def start(self):
        self.logger.debug("LocalIOHandler start: Starting components and transfer tasks...")
        # Input starts paused
        self._pause_input_event.clear()
        # Start underlying components
        await self.audio_input.start()
        await self.audio_output.start()

        # Start the input transfer task (Mic -> Internal Queue)
        if self._transfer_input_task is None or self._transfer_input_task.done():
            self._transfer_input_task = asyncio.create_task(self._transfer_input_loop(), name="IOHandlerTransferInput")
            self.logger.info("LocalIOHandler: Started input transfer task.")
        else:
            self.logger.warning("LocalIOHandler: Input transfer task already running?")

        # Start the output transfer task (Internal Queue -> Speaker)
        if self._transfer_output_task is None or self._transfer_output_task.done():
            self._transfer_output_task = asyncio.create_task(self._transfer_output_loop(), name="IOHandlerTransferOutput")
            self.logger.info("LocalIOHandler: Started output transfer task.")
        else:
            self.logger.warning("LocalIOHandler: Output transfer task already running?")

    # --- stop --- (No changes needed here, silence monitoring state handled implicitly)
    async def stop(self):
        self.logger.debug("LocalIOHandler stop: Stopping transfer tasks and components...")
        io_stop_timeout = 5.0 # Shorter timeout for IO tasks

        # --- Stop Transfer Tasks ---
        tasks_to_stop = []
        # Set pause event to allow input task to potentially finish gracefully if waiting
        self._pause_input_event.set()
        if self._transfer_input_task and not self._transfer_input_task.done():
             tasks_to_stop.append(self._transfer_input_task)
             self._transfer_input_task = None # Clear ref early
        if self._transfer_output_task and not self._transfer_output_task.done():
             # Enqueue None to potentially help output loop finish cleanly
             try: self.internal_output_queue.put_nowait(None)
             except asyncio.QueueFull: pass
             tasks_to_stop.append(self._transfer_output_task)
             self._transfer_output_task = None # Clear ref early

        if tasks_to_stop:
             self.logger.debug(f"Cancelling {len(tasks_to_stop)} IO transfer tasks (timeout: {io_stop_timeout}s)...")
             for task in tasks_to_stop:
                 task.cancel()
             try:
                 # Wait for tasks with timeout
                 await asyncio.wait_for(asyncio.gather(*tasks_to_stop, return_exceptions=True), timeout=io_stop_timeout)
                 self.logger.debug("IO transfer tasks cancelled or finished.")
             except asyncio.TimeoutError:
                  self.logger.warning(f"Timeout waiting for IO transfer tasks to cancel after {io_stop_timeout}s.")
             except Exception as e:
                  self.logger.error(f"Error cancelling IO transfer tasks: {e}", exc_info=True)
        # --- End Stop Transfer Tasks ---

        # --- Stop Underlying Components ---
        component_stop_tasks = []
        if self.audio_input and hasattr(self.audio_input, 'stop'):
            component_stop_tasks.append(asyncio.create_task(self.audio_input.stop(), name="AudioInputStop"))
        if self.audio_output and hasattr(self.audio_output, 'stop'):
            component_stop_tasks.append(asyncio.create_task(self.audio_output.stop(), name="AudioOutputStop"))

        if component_stop_tasks:
             self.logger.debug(f"Stopping {len(component_stop_tasks)} underlying audio components (timeout: {io_stop_timeout}s)...")
             try:
                 # Wait for component stop tasks with timeout
                 await asyncio.wait_for(asyncio.gather(*component_stop_tasks, return_exceptions=True), timeout=io_stop_timeout)
                 self.logger.debug("Underlying components stopped or finished.")
             except asyncio.TimeoutError:
                 self.logger.warning(f"Timeout waiting for underlying audio components to stop after {io_stop_timeout}s.")
             except Exception as e:
                 self.logger.error(f"Error stopping underlying audio components: {e}", exc_info=True)
        # --- End Stop Underlying Components ---

        self.logger.info("LocalIOHandler stop sequence finished.")

    # --- clear_output_buffer --- (No changes needed here)
    async def clear_output_buffer(self):
        """Clears the internal output queue (LiveKit -> Speaker)."""
        self.logger.debug("LocalIOHandler: Clearing internal output buffer...")
        await self._empty_queue(self.internal_output_queue)
        self.logger.debug("LocalIOHandler: Internal output buffer cleared.")


# --- LightberryLocalClient Modifications ---
class LightberryLocalClient:
    def __init__(self, url: str, token: str, room_name: str, loop: asyncio.AbstractEventLoop, trigger_event_queue: asyncio.Queue):
        self.livekit_url = url
        self.livekit_token = token
        self.room_name = room_name # Though token usually defines the room
        self._loop = loop
        self.logger = logger.getChild("Client") # Add logger instance
        self._room: Optional[rtc.Room] = None
        self._is_connected = False # Track connection status
        self._is_paused = False # Track pause state

        # --- Timing Infrastructure ---
        self.timing_data = []
        self.chunk_id_counter = 0
        # Initialize path immediately
        self.timing_log_file_path: Optional[str] = self._generate_timing_log_path()
        self._timing_lock = threading.Lock() # Use threading lock for thread safety
        # --- End Timing Infrastructure ---

        # --- Instantiate Audio components ---
        # Use standard rate (48kHz) for LiveKit compatibility
        # Align chunk size with 10ms LiveKit frames (480 samples = 960 bytes)
        mic_frames_per_buffer = 480
        self._mic_input: PyAudioMicrophoneInput = PyAudioMicrophoneInput(
            loop=self._loop,
            sample_rate=LIVEKIT_SAMPLE_RATE,
            frames_per_buffer=mic_frames_per_buffer,
            logger_instance=logger.getChild("MicInput"),
            log_timing_cb=self._log_timing,
            get_chunk_id_cb=self._get_next_chunk_id,
            input_device_index=None # Use default input device
            )
        self._speaker_output: SoundDeviceSpeakerOutput = SoundDeviceSpeakerOutput(
            loop=self._loop,
            sample_rate=LIVEKIT_SAMPLE_RATE,
            channels=1,
            logger_instance=logger.getChild("SpeakerOutput"),
            output_device_index=None # Use default output device
            )
        # --- End Audio Instantiation ---

        # --- Instantiate IO Handler ---
        # Pass self (client instance) AND trigger_event_queue to the handler
        self._local_io_handler: LocalIOHandler = LocalIOHandler(
             self._mic_input, self._speaker_output, self, logger.getChild("LocalIOHandler"), trigger_event_queue=trigger_event_queue
        )
        # --- End IO Handler ---

        self._mic_audio_source: Optional[rtc.AudioSource] = None
        self._mic_track_pub: Optional[rtc.LocalTrackPublication] = None
        self._forward_mic_task: Optional[asyncio.Task] = None
        self._playback_tasks: dict[str, asyncio.Task] = {} # track_sid -> playback task

        # Log the initialized path
        self.logger.info(f"LightberryLocalClient initialized. Timing logs will be saved to '{self.timing_log_file_path}'")

    def _register_listeners(self):
        if not self._room:
            return
        self._room.on("track_subscribed", self._on_track_subscribed)
        self._room.on("disconnected", self._on_disconnected)
        # Add more listeners if needed (e.g., participant connected/disconnected)
        logger.info("Room event listeners registered.")

    async def connect(self):
        """Connects to the LiveKit room."""
        if self._is_connected:
             logger.warning("Already connected to LiveKit.")
             return
        logger.info(f"Attempting to connect to LiveKit URL: {self.livekit_url} in room '{self.room_name}'...")
        try:
            self._room = rtc.Room(loop=self._loop)
            self._register_listeners() # Register listeners before connecting
            # TODO: Add connection timeout?
            await self._room.connect(self.livekit_url, self.livekit_token)
            self._is_connected = True # Set flag on successful connection
            logger.info(f"Successfully connected to LiveKit room: {self._room.name} (SID: {await self._room.sid})")

        except rtc.ConnectError as e:
            logger.error(f"Failed to connect to LiveKit room: {e}", exc_info=True)
            self._room = None
            self._is_connected = False
            raise # Re-raise the exception so caller (main loop) knows connection failed
        except Exception as e:
             logger.error(f"Unexpected error during LiveKit connection: {e}", exc_info=True)
             self._room = None
             self._is_connected = False
             raise


    async def start(self):
        """Starts IO components, connects to LiveKit, and starts processing tasks."""
        logger.info("Starting LightberryLocalClient...")
        if self._is_connected:
            logger.warning("Client already started and connected.")
            return
        try:
            # 1. Start the IO Handler (starts mic/speaker components and IO transfer loops)
            await self._local_io_handler.start()

            # 2. Connect to the room
            await self.connect() # Raises exception on failure

            # 3. Create and publish mic track if connection successful
            if self._is_connected:
                await self._create_and_publish_mic_track() # Creates source and publishes

                # 4. Start the microphone forwarding task if source exists
                if self._mic_audio_source:
                    # Clear buffer and resume input immediately after connecting
                    await self._local_io_handler._clear_input_buffers()
                    self._local_io_handler._pause_input_event.set() # Unpause mic input
                    self._is_paused = False # Ensure not paused on fresh start

                    self._forward_mic_task = asyncio.create_task(self._forward_mic_to_livekit(), name="ForwardMicToLiveKit")
                    logger.info("Microphone forwarding task started.")
                else:
                    logger.error("Could not start microphone forwarding: AudioSource not created.")
                    # Consider stopping if mic forwarding is critical?
                    raise RuntimeError("Failed to create microphone audio source.")

                logger.info("Client started and processing tasks initiated.")
            else:
                 # This case should ideally be handled by connect() raising an error
                 logger.error("Client start failed because room connection failed.")
                 await self.stop() # Attempt cleanup

        except Exception as e:
             logger.error(f"Error during client start sequence: {e}", exc_info=True)
             await self.stop() # Ensure cleanup on error
             raise # Propagate error to main loop

    async def _perform_stop_operations(self, initial_connected_state: bool):
        """Helper coroutine containing the actual stop operations for timeout."""
        # --- Cancel running tasks ---
        tasks_to_cancel: list[Optional[asyncio.Task]] = []
        tasks_to_cancel.append(self._forward_mic_task)
        tasks_to_cancel.extend(self._playback_tasks.values())

        active_tasks = [task for task in tasks_to_cancel if task and not task.done()]

        if active_tasks:
             self.logger.info(f"Cancelling {len(active_tasks)} background tasks...")
             for task in active_tasks:
                 task.cancel()
             results = await asyncio.gather(*active_tasks, return_exceptions=True)
             self.logger.debug(f"Background task cancellation results: {results}")

        self._forward_mic_task = None
        self._playback_tasks.clear()

        # --- Stop IO handler (stops underlying components and its transfer loops) ---
        if self._local_io_handler:
             await self._local_io_handler.stop()

        # --- Unpublish mic track ---
        if self._mic_track_pub and self._room and self._room.local_participant:
            self.logger.info(f"Unpublishing microphone track {self._mic_track_pub.sid}...")
            try:
                if self._room.local_participant:
                    await self._room.local_participant.unpublish_track(self._mic_track_pub.sid)
                    self.logger.info("Microphone track unpublished.")
                else:
                     self.logger.warning("Local participant not found during unpublish.")
            except Exception as e:
                 self.logger.error(f"Error unpublishing mic track: {e}", exc_info=True)
        self._mic_track_pub = None
        self._mic_audio_source = None # Clear source reference

        # --- Disconnect from room ---
        room_to_disconnect = self._room
        self._room = None # Clear room reference
        if room_to_disconnect and initial_connected_state:
            self.logger.info("Disconnecting from LiveKit room...")
            try:
                await room_to_disconnect.disconnect()
                self.logger.info("Disconnected from LiveKit room successfully.")
            except Exception as e:
                 self.logger.error(f"Error during room disconnect: {e}", exc_info=True)
        elif initial_connected_state:
            self.logger.warning("Room reference lost before disconnect could be called.")
        else:
             self.logger.debug("Already disconnected or room not initialized.")

    async def stop(self):
        """Stops all tasks, disconnects, and cleans up resources with a timeout."""
        if not self._is_connected and not self._forward_mic_task and not self._playback_tasks:
             self.logger.debug("Stop called but client appears already stopped or not started.")
             # Reset flags just in case
             self._is_connected = False
             self._is_paused = False
             return

        self.logger.info("Stopping LightberryLocalClient...")
        initial_connected_state = self._is_connected
        self._is_connected = False # Mark as disconnected early
        self._is_paused = False # Reset paused state on stop
        stop_success = False
        timeout_duration = 10.0

        try:
            # Use asyncio.wait_for for older Python versions
            await asyncio.wait_for(self._perform_stop_operations(initial_connected_state), timeout=timeout_duration)
            stop_success = True # Mark as successful if timeout wasn't hit

        except asyncio.TimeoutError: # wait_for raises asyncio.TimeoutError
             self.logger.error(f"LightberryLocalClient stop operation timed out after {timeout_duration}s.")
             # Force clear resources that might be hanging?
             self._forward_mic_task = None
             self._playback_tasks.clear()
             self._mic_track_pub = None
             self._mic_audio_source = None
             self._room = None
             # Re-raise the error to signal failure to the caller (main loop)
             raise
        except Exception as e:
             self.logger.error(f"Unexpected error during client stop: {e}", exc_info=True)
             # Optionally re-raise or just log?
             # raise # Uncomment if main loop should handle this too
        finally:
             if stop_success:
                 self.logger.info("LightberryLocalClient stopped successfully.")
             else:
                 self.logger.warning("LightberryLocalClient stop finished, but may not have completed gracefully (timeout or error).")

    # --- New Pause/Resume Methods ---
    async def pause(self):
        """Pauses audio processing, cancelling silence monitor first."""
        if not self._is_connected:
            logger.warning("Cannot pause, client is not connected.")
            return
        if self._is_paused:
            logger.debug("Client already paused.")
            return

        logger.info("Pausing client...")
        self._is_paused = True
        # Explicitly stop silence monitoring in IO Handler
        if hasattr(self._local_io_handler, 'is_monitoring_silence'): # Check attribute exists
            self._local_io_handler.is_monitoring_silence = False
            self._local_io_handler.silence_start_time = None

        # Pause input transfer loop
        if self._local_io_handler._pause_input_event.is_set():
            self.logger.debug("Pausing input transfer.")
            self._local_io_handler._pause_input_event.clear()
        else:
            self.logger.debug("Input transfer already paused (pause called).")

        # Playback pausing is handled implicitly
        logger.info("Client Paused.")

    async def resume(self):
        """Resumes audio processing."""
        if not self._is_connected:
            logger.warning("Cannot resume, client is not connected.")
            return

        # Explicitly ensure silence monitoring is off when manually resuming
        if hasattr(self._local_io_handler, 'is_monitoring_silence'): # Check attribute exists
            self._local_io_handler.is_monitoring_silence = False
            self._local_io_handler.silence_start_time = None

        self._is_paused = False
        # Clear any potentially stale audio received during pause
        await self._local_io_handler.clear_output_buffer()
        # Resume microphone input transfer
        if not self._local_io_handler._pause_input_event.is_set():
             self.logger.info("Resuming input transfer.")
             self._local_io_handler._pause_input_event.set()
        else:
             self.logger.debug("Input transfer already resumed.")
        logger.info("Client Resumed.")


    async def _create_and_publish_mic_track(self):
        if not self._room or not self._room.local_participant:
            logger.error("Cannot create mic track: Not connected to room or no local participant.")
            return
        if self._mic_audio_source:
            logger.warning("Mic track already created.")
            return
        try:
            # Create source with LIVEKIT_SAMPLE_RATE (48kHz)
            logger.info(f"Creating mic AudioSource with rate {LIVEKIT_SAMPLE_RATE}Hz")
            self._mic_audio_source = rtc.AudioSource(LIVEKIT_SAMPLE_RATE, LIVEKIT_CHANNELS)
            track = rtc.LocalAudioTrack.create_audio_track("mic-audio", self._mic_audio_source)
            options = rtc.TrackPublishOptions(source=rtc.TrackSource.SOURCE_MICROPHONE)
            logger.info("Publishing microphone audio track...")
            self._mic_track_pub = await self._room.local_participant.publish_track(track, options)
            logger.info(f"Microphone track published successfully (SID: {self._mic_track_pub.sid}).")
        except Exception as e:
            logger.error(f"Failed to create or publish mic track: {e}", exc_info=True)
            self._mic_audio_source = None # Clean up source on error
        # Ensure proper spacing/indentation before the next method

    async def _forward_mic_to_livekit(self):
        """Task to read timed audio chunks from IOHandler queue and push frames to LiveKit AudioSource."""
        if not self._mic_audio_source:
            logger.error("Cannot forward mic audio: AudioSource is not available.")
            return

        try:
            # Get queue from IO Handler (contains (chunk_data, chunk_id, t_put_internal_q))
            internal_queue = self._local_io_handler._internal_input_queue

            # Calculate frame properties (should match input buffer now)
            mic_frames_per_buffer = self._mic_input._frames_per_buffer # Get from instance
            samples_per_frame = mic_frames_per_buffer
            bytes_per_frame = samples_per_frame * LIVEKIT_CHANNELS * 2 # e.g., 480 * 1 * 2 = 960

            logger.info(f"Starting microphone forwarding loop (Expecting {bytes_per_frame}-byte frames)")

            while self._is_connected: # Loop while connected
                try:
                    # Get item with timeout to allow checking _is_connected
                    chunk_data, chunk_id, t_put_internal_q = await asyncio.wait_for(internal_queue.get(), timeout=0.5)

                    t_get_internal_q = time.time()
                    internal_queue.task_done()

                    # Log timing if available
                    if hasattr(self, '_log_timing'):
                         internal_q_wait = t_get_internal_q - t_put_internal_q
                         self._log_timing('get_internal_q', chunk_id, timestamp=t_get_internal_q, wait_time=internal_q_wait)

                    # --- Validate and Send Frame ---
                    if len(chunk_data) != bytes_per_frame:
                         logger.warning(f"Chunk {chunk_id} size mismatch! Expected {bytes_per_frame}, got {len(chunk_data)}. Skipping frame.")
                         if hasattr(self, '_log_timing'): self._log_timing('skip_frame_size', chunk_id, timestamp=time.time())
                         continue

                    frame = rtc.AudioFrame(
                        sample_rate=LIVEKIT_SAMPLE_RATE,
                        num_channels=LIVEKIT_CHANNELS,
                        samples_per_channel=samples_per_frame,
                        data=chunk_data
                    )

                    t_start_send = time.time()
                    # Ensure source still exists before capturing
                    if self._mic_audio_source:
                        await self._mic_audio_source.capture_frame(frame)
                    else:
                        logger.warning(f"Mic audio source became None while trying to send frame {chunk_id}. Stopping forwarder.")
                        break # Exit loop if source disappears

                    t_end_send = time.time()
                    if hasattr(self, '_log_timing'):
                         send_duration = t_end_send - t_start_send
                         self._log_timing('sent_livekit', chunk_id, timestamp=t_end_send, duration=send_duration)
                    # --- End Send Frame ---

                except asyncio.TimeoutError:
                    # Just continue loop if timeout, connection check handles exit
                    continue
                except asyncio.CancelledError:
                     logger.info("Microphone forwarding task cancelled.")
                     break # Exit loop on cancellation
                except Exception as e:
                    # Log chunk ID if available
                    log_chunk_id = chunk_id if 'chunk_id' in locals() else 'unknown'
                    logger.error(f"Error in mic forward loop processing chunk {log_chunk_id}: {e}", exc_info=True)
                    await asyncio.sleep(0.1) # Avoid tight loop on error

        except asyncio.CancelledError:
            logger.info("Microphone forwarding task cancelled (outer loop).")
        except Exception as e:
             logger.error(f"Fatal error in microphone forwarding task: {e}", exc_info=True)
        finally:
             logger.info("Microphone forwarding loop finished.")
             # Ensure mic input is paused if loop exits unexpectedly
             if self._local_io_handler: self._local_io_handler._pause_input_event.clear()


    def _on_disconnected(self):
        self.logger.warning("Disconnected from LiveKit room unexpectedly!")
        # Signal the main loop via the trigger queue
        if hasattr(self, '_local_io_handler') and self._local_io_handler._trigger_event_queue:
            try:
                # Use call_soon_threadsafe if this callback might not be on the main loop
                # For now, assume it is or put_nowait is safe enough.
                self._local_io_handler._trigger_event_queue.put_nowait(TriggerEvent.UNEXPECTED_DISCONNECT)
                self.logger.info("Emitted UNEXPECTED_DISCONNECT event to trigger queue.")
            except asyncio.QueueFull:
                self.logger.error("Trigger queue full, cannot emit UNEXPECTED_DISCONNECT event!")
            except Exception as e:
                self.logger.error(f"Error emitting UNEXPECTED_DISCONNECT event: {e}")
        else:
             self.logger.error("Cannot emit UNEXPECTED_DISCONNECT: Trigger queue not found in IO handler.")
        # REMOVED: self._stop_event.set()
        # REMOVED: self._mic_source_ready.clear() # _mic_source_ready was removed earlier

    def _on_track_subscribed(self, track: rtc.Track, publication: rtc.RemoteTrackPublication, participant: rtc.RemoteParticipant):
        logger.info(f"Track subscribed: {track.kind} '{track.name}' from {participant.identity}")
        if track.kind == rtc.TrackKind.KIND_AUDIO and participant.identity == ASSISTANT_IDENTITY:
            if track.sid not in self._playback_tasks:
                logger.info(f"Assistant audio track ({track.sid}) detected. Starting playback task.")
                task = asyncio.create_task(self._forward_livekit_to_speaker(track), name=f"Playback_{track.sid[:6]}")
                self._playback_tasks[track.sid] = task
        else:
            logger.debug("Subscribed track is not the target assistant audio track.")

    async def _forward_livekit_to_speaker(self, track: rtc.Track):
        """Task to read 48kHz audio frames from a LiveKit track and forward to the speaker output."""
        # Use standard rate in log
        logger.info(f"Starting playback loop for track {track.sid} (Rate: {LIVEKIT_SAMPLE_RATE}Hz)") 
        audio_stream = rtc.AudioStream(track)
        # Remove local silence detection state - now handled in LocalIOHandler
        # is_speaking = False # Track speaking state for this specific track
        # silence_threshold = 100 # Adjust as needed for int16 amplitude
        try:
            async for frame_event in audio_stream:
                frame = frame_event.frame
                # Check against standard rate
                if frame.sample_rate != LIVEKIT_SAMPLE_RATE or frame.num_channels != LIVEKIT_CHANNELS:
                    logger.warning(f"Unexpected audio format received on track {track.sid}: SR={frame.sample_rate}, Ch={frame.num_channels}. Skipping frame.")
                    continue

                audio_bytes = bytes(frame.data)

                # --- Remove Silence Detection Logic ---
                # try:
                #     # Assuming int16 PCM data
                #     audio_samples = np.frombuffer(audio_bytes, dtype=np.int16)
                #     # Use absolute mean as a simple measure of energy
                #     # Use a small epsilon to avoid division by zero or log(0) if calculating RMS dB later
                #     abs_mean = np.mean(np.abs(audio_samples)) + 1e-10

                #     currently_speaking = abs_mean > silence_threshold

                #     if currently_speaking and not is_speaking:
                #         timestamp = datetime.now().strftime('%Y-%m-%d %H:%M:%S.%f')[:-3]
                #         logger.info(f"[{timestamp}] Assistant started speaking (Track: {track.sid}, Mean: {abs_mean:.2f})")
                #         is_speaking = True
                #     elif not currently_speaking and is_speaking:
                #         timestamp = datetime.now().strftime('%Y-%m-%d %H:%M:%S.%f')[:-3]
                #         logger.info(f"[{timestamp}] Assistant stopped speaking (Track: {track.sid}, Mean: {abs_mean:.2f})")
                #         is_speaking = False

                # except Exception as e:
                #     logger.error(f"Error during silence detection for track {track.sid}: {e}", exc_info=True)
                # --- End Silence Detection Logic ---

                # Send audio chunk to the IO Handler's output queue
                await self._local_io_handler.send_output_chunk(audio_bytes) 
        except asyncio.CancelledError:
            logger.info(f"Playback task for track {track.sid} cancelled.")
        except Exception as e:
            logger.error(f"Error in playback loop for track {track.sid}: {e}", exc_info=True)
        finally:
            logger.info(f"Playback loop for track {track.sid} finished.")
            self._playback_tasks.pop(track.sid, None)

    # --- Timing Methods ---
    def _generate_timing_log_path(self):
         timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
         return os.path.join(TIMING_LOG_DIR, f"timing_log_{timestamp}.json")

    def _get_next_chunk_id(self) -> int:
         # No need for async lock if counter is only incremented here sequentially
         # If called from multiple async tasks concurrently, locking would be needed
         self.chunk_id_counter += 1
         return self.chunk_id_counter

    def _log_timing(self, event_type: str, chunk_id: int, **kwargs):
         # Using asyncio.Lock is incorrect here as this might be called from sync callbacks
         # Rely on GIL for list append atomicity for simple cases, or use thread-safe queue if needed
         # For now, direct append, assuming low contention from logging points
         log_entry = {
             'timestamp': time.time(),
             'event': event_type,
             'chunk_id': chunk_id,
             **kwargs
         }
         self.timing_data.append(log_entry)
         # Limit log size in memory if necessary
         # MAX_LOG_ENTRIES = 50000 # Example limit
         # if len(self.timing_data) > MAX_LOG_ENTRIES:
         #     self.timing_data.pop(0) # Remove oldest entry

    def save_timing_data(self):
        # No async lock needed if called only from signal_end_of_speech in IOHandler's task
        # Safeguard against None path
        return #remove actual timing saving for latency reasons.
        if self.timing_log_file_path is None:
            self.logger.warning("save_timing_data called with None path, generating new path...")
            self.timing_log_file_path = self._generate_timing_log_path()
            if self.timing_log_file_path is None: # Check again if generation failed
                 self.logger.error("Failed to generate timing log path! Cannot save timing data.")
                 # Reset data even if save fails?
                 self.timing_data = []
                 self.chunk_id_counter = 0
                 return

        self.logger.info(f"Saving {len(self.timing_data)} timing entries to {self.timing_log_file_path}...")
        try:
            # Ensure the directory exists before writing
            os.makedirs(TIMING_LOG_DIR, exist_ok=True)
            with open(self.timing_log_file_path, 'w') as f:
                json.dump(self.timing_data, f, indent=2)
            self.logger.info("Timing data successfully saved.")
        except TypeError as te:
            # Add specific logging for the TypeError case
            self.logger.error(f"TypeError saving timing data: {te}. Path was: {self.timing_log_file_path} (Type: {type(self.timing_log_file_path)}). Data length: {len(self.timing_data)}", exc_info=True)
        except Exception as e:
            self.logger.error(f"Failed to save timing data: {e}", exc_info=True)

        # Reset for the next interaction
        self.timing_data = []
        self.chunk_id_counter = 0
        # Generate path for the *next* save attempt
        self.timing_log_file_path = self._generate_timing_log_path()
        self.logger.info(f"Timing log reset. Next log will be: {self.timing_log_file_path}")
    # --- End Timing Methods ---

    async def _play_raw_audio_helper(self, raw_audio_data: bytes):
        """Internal helper to chunk and play raw audio data."""
        try:
            chunk_size = 960 # 10ms at 48kHz, 16-bit mono
            total_bytes = len(raw_audio_data)
            bytes_sent = 0
            self.logger.debug(f"Starting playback of {total_bytes} bytes of raw local audio.")

            # Use the speaker output's start/end signals
            await self._speaker_output.signal_start_of_speech()

            while bytes_sent < total_bytes:
                chunk = raw_audio_data[bytes_sent : bytes_sent + chunk_size]
                if not chunk:
                    break
                await self._speaker_output.send_audio_chunk(chunk)
                bytes_sent += len(chunk)
                # Yield control briefly to allow other tasks to run
                await asyncio.sleep(0.001) # Sleep slightly less than chunk duration (10ms)

            # Ensure end signal is sent
            await self._speaker_output.signal_end_of_speech()
            self.logger.debug("Finished playback of raw local audio.")

        except asyncio.CancelledError:
            self.logger.info("Local audio playback task cancelled.")
            # Attempt to signal end of speech even on cancellation
            try:
                 await self._speaker_output.signal_end_of_speech()
            except Exception:
                 pass # Ignore errors during cleanup on cancellation
        except Exception as e:
            self.logger.error(f"Error during local raw audio playback: {e}", exc_info=True)
            # Attempt to signal end of speech on error
            try:
                 await self._speaker_output.signal_end_of_speech()
            except Exception:
                 pass

    async def play_local_raw_audio(self, audio_data: bytes):
        """Plays raw audio data from a local file as a background task."""
        if not self._speaker_output or not hasattr(self._speaker_output, '_is_running') or not self._speaker_output._is_running:
             self.logger.warning("Speaker output not initialized or not running. Cannot play local audio.")
             return
        if not audio_data:
             self.logger.warning("No audio data provided to play_local_raw_audio.")
             return

        # Create a task to run the playback helper concurrently
        asyncio.create_task(self._play_raw_audio_helper(audio_data), name="PlayLocalRawAudioHelper")

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

    # --- Load Pre-processed Audio ---
    connect_clip_path = "connect_clip.raw"
    connect_clip_data = load_raw_audio(connect_clip_path)
    if connect_clip_data is None:
         logger.warning(f"Could not load connection audio clip from {connect_clip_path}. Playback will be skipped.")
    # --- End Load Audio ---


    # --- State Machine & Control Variables ---
    loop = asyncio.get_running_loop()
    trigger_event_queue = asyncio.Queue()
    stop_event = asyncio.Event() # Global stop signal
    current_state = AppState.IDLE
    client: Optional[LightberryLocalClient] = None
    trigger_controller: Optional[TriggerController] = None
    token: Optional[str] = None
    room_name: Optional[str] = None
    device_id: Optional[str] = None
    username: Optional[str] = None

    # --- Instantiate Trigger Controller ---
    try:
        if platform.system() == "Darwin":
            logger.info("Platform is Darwin, attempting to use MacKeyboardTrigger.")
            # Check if pynput is available (check done internally by MacKeyboardTrigger class)
            trigger_controller = MacKeyboardTrigger(loop, trigger_event_queue)
            # Add a check here to see if MacKeyboardTrigger considers itself functional
            if not hasattr(trigger_controller, '_listener_loop'): # Simple check if placeholder was used
                 logger.warning("MacKeyboardTrigger is unavailable (pynput likely missing). Trying Trinkey.")
                 trigger_controller = None # Fallback to trinkey
            elif not _pynput_available: # Check the global flag set during import
                 logger.warning("MacKeyboardTrigger loaded but pynput failed import earlier. Trying Trinkey.")
                 trigger_controller = None # Fallback to trinkey
            else:
                 logger.info("MacKeyboardTrigger initialized.")
        # Fallback or default to Trinkey if not Darwin or Mac trigger failed
        if trigger_controller is None:
             logger.info("Attempting to use TrinkeyTrigger.")
             # Note: find_trinkey_path logs warnings/errors if not found
             # TrinkeyTrigger init doesn't fail if path not found initially, listener loop handles it
             trigger_controller = TrinkeyTrigger(loop, trigger_event_queue, ADAFRUIT_VID, TRINKEY_PID, TRINKEY_KEYCODE)
             logger.info("TrinkeyTrigger initialized.")

    except Exception as e:
         logger.error(f"Failed to initialize a trigger controller: {e}", exc_info=True)
         # Allow continuing without a trigger for debugging/testing? Or exit?
         # For now, log error and proceed; loop will handle None controller.
         trigger_controller = None

    if trigger_controller is None:
        logger.error("FATAL: No functional trigger controller could be initialized. Exiting.")
        return

    # --- Main State Machine Loop ---
    try:
        # Define callback for IO Handler to request state change
        async def request_main_state_change(new_state: AppState):
            nonlocal current_state # Allow modification of main loop's state variable
            logger.info(f"IO Handler requested state change to {new_state.name}")
            # Add safety check? e.g., only allow transition to PAUSED from ACTIVE?
            if current_state == AppState.ACTIVE and new_state == AppState.PAUSED:
                 current_state = new_state
            else:
                 logger.warning(f"Ignoring state change request from {current_state.name} to {new_state.name}")

       

        while not stop_event.is_set():
            logger.debug(f"--- Main Loop: Current State = {current_state.name} ---")
            if current_state == AppState.IDLE:
                print("\n>>> Current State: IDLE <<<", flush=True)
                logger.info("Entered IDLE state.")
                # Ensure trigger listener is running
                try:
                    logger.info("Starting trigger listener...")
                    trigger_controller.start_listening() # Safe to call multiple times
                except Exception as e:
                    logger.error(f"Error starting trigger listener: {e}. Stopping.")
                    current_state = AppState.STOPPING
                    continue

                print("Waiting for the first trigger press (Spacebar or Trinkey)...", flush=True)
                logger.info("Clearing potential stale events from trigger queue...")
                """while not trigger_event_queue.empty():
                    try:
                        stale_event = trigger_event_queue.get_nowait()
                        logger.warning(f"Discarding stale event from queue before wait: {stale_event.name}")
                        trigger_event_queue.task_done()
                    except asyncio.QueueEmpty:
                        break
                    except Exception as e:
                         logger.error(f"Error clearing stale event: {e}")

                try:
                    logger.info("<<< Waiting for trigger_event_queue.get() >>>")
                    # Wait indefinitely for the first trigger event
                    event = await trigger_event_queue.get()
                    logger.info(f"<<< Event RECEIVED from queue: {event.name} >>>")
                    trigger_event_queue.task_done()

                    # We expect FIRST_PRESS to initiate connection
                    if event == TriggerEvent.FIRST_PRESS:
                        logger.info("First trigger event received. Transitioning to CONNECTING.")
                        print("Trigger received! Connecting...", flush=True)
                        current_state = AppState.CONNECTING
                        # Keep listener active
                    else:
                        logger.warning(f"Unexpected event '{event.name}' received in IDLE state. Ignoring.")
                
                except asyncio.CancelledError:
                    logger.info("IDLE state wait cancelled.")
                    current_state = AppState.STOPPING
                except Exception as e:
                    logger.error(f"Error waiting for trigger event in IDLE state: {e}", exc_info=True)
                    await asyncio.sleep(1) # Pause before retry (loop continues)"""
                current_state = AppState.CONNECTING
            elif current_state == AppState.CONNECTING:
                print(">>> Current State: CONNECTING <<<", flush=True)
                logger.info("Entered CONNECTING state.")
                # Fetch credentials only when connecting
                token, room_name = None, None # Reset before trying
                try:
                    device_id = get_or_create_device_id() # Assuming this is quick/sync
                    username = "recDNsqLQ9CKswdVJ" # Example: Default user
                    logger.info(f"Fetching credentials for Device ID: {device_id}, User: {username}")
                    token, room_name = await get_credentials(device_id, username)

                    if token and room_name:
                        logger.info("Credentials obtained successfully.")
                        logger.info(f"LiveKit URL: {LIVEKIT_URL}, Room: {room_name}")
                        # Create and start the client
                        logger.info("Creating LightberryLocalClient instance...")
                        # Stop passing stop_event to client, manage externally
                        # client = LightberryLocalClient(LIVEKIT_URL, token, room_name, loop, stop_event)
                        client = LightberryLocalClient(LIVEKIT_URL, token, room_name, loop, trigger_event_queue)
                        

                        # --- Play Connection Sound ---
                        if connect_clip_data:
                             logger.info("Attempting to play connection sound...")
                             # Don't await this, let it play in the background
                             asyncio.create_task(client.play_local_raw_audio(connect_clip_data), name="PlayConnectClip")
                        # --- End Play Sound ---
                        logger.info("Starting client connection and processing...")
                        await client.start() # Connects, publishes, starts IO/forwarding
                        logger.info("Client started successfully.")
                        current_state = AppState.ACTIVE
                        print("Connected and Active!", flush=True)
                    else:
                        logger.error("Failed to obtain credentials.")
                        print("Connection failed (credentials). Returning to IDLE.", flush=True)
                        current_state = AppState.IDLE
                        await asyncio.sleep(2)

                except Exception as e:
                    logger.error(f"Error during CONNECTING state: {e}", exc_info=True)
                    print(f"Connection failed ({type(e).__name__}). Returning to IDLE.", flush=True)
                    if client:
                        logger.info("Attempting to stop client after connection error...")
                        try:
                            await client.stop()
                        except Exception as stop_err:
                            logger.error(f"Error stopping client after connection failure: {stop_err}")
                    client = None # Ensure client is None after failure
                    current_state = AppState.IDLE
                    await asyncio.sleep(5) # Wait longer after error

            elif current_state == AppState.ACTIVE:
                print(">>> Current State: ACTIVE <<< Mic Live | Single Press: Pause | Double Press: Stop", flush=True)
                logger.info("Entered ACTIVE state. Waiting for trigger.")
                try:
                    # Wait for next trigger (pause, stop, auto-pause, or disconnect)
                    event = await trigger_event_queue.get()
                    logger.info(f"Event received in ACTIVE state: {event.name}")
                    trigger_event_queue.task_done()

                    # Treat FIRST_PRESS as manual pause
                    if event == TriggerEvent.FIRST_PRESS or event == TriggerEvent.SUBSEQUENT_PRESS:
                        logger.info("Manual pause trigger received. Pausing client.")
                        print("Pausing...", flush=True)
                        if client:
                             await client.pause() # This now stops monitoring and pauses input
                        current_state = AppState.PAUSED
                    # Handle auto-pause event from IO Handler
                    elif event == TriggerEvent.AUTO_PAUSE:
                         logger.info("Auto-pause event received due to silence. Transitioning to PAUSED state.")
                         # IOHandler already paused input, just update state
                         current_state = AppState.PAUSED
                    elif event == TriggerEvent.DOUBLE_PRESS:
                        logger.info("Stop trigger (double press) received. Stopping client.")
                        print("Stopping...", flush=True)
                        current_state = AppState.STOPPING
                    elif event == TriggerEvent.UNEXPECTED_DISCONNECT:
                         logger.warning("Unexpected disconnect event received. Stopping client.")
                         current_state = AppState.STOPPING
                    else:
                         logger.warning(f"Unexpected event '{event.name}' received in ACTIVE state.")

                except asyncio.CancelledError:
                    logger.info("ACTIVE state wait cancelled.")
                    current_state = AppState.STOPPING
                except Exception as e:
                    logger.error(f"Error in ACTIVE state: {e}", exc_info=True)
                    current_state = AppState.STOPPING # Stop on error?

            elif current_state == AppState.PAUSED:
                print(">>> Current State: PAUSED <<< ⏸️ | Single Press: Resume | Double Press: Stop", flush=True)
                logger.info("Entered PAUSED state. Waiting for trigger.")
                try:
                    # Wait for next trigger (resume, stop, or disconnect)
                    event = await trigger_event_queue.get()
                    logger.info(f"Event received in PAUSED state: {event.name}")
                    trigger_event_queue.task_done()

                    # Treat FIRST_PRESS as toggle/resume when paused
                    if event == TriggerEvent.FIRST_PRESS or event == TriggerEvent.SUBSEQUENT_PRESS:
                        logger.info("Resume trigger received. Resuming client.")
                        print("Resuming...", flush=True)
                        if client:
                             await client.resume()
                        current_state = AppState.ACTIVE
                    elif event == TriggerEvent.DOUBLE_PRESS:
                        logger.info("Stop trigger (double press) received. Stopping client.")
                        print("Stopping...", flush=True)
                        current_state = AppState.STOPPING
                    elif event == TriggerEvent.UNEXPECTED_DISCONNECT:
                         logger.warning("Unexpected disconnect event received while PAUSED. Stopping client.")
                         current_state = AppState.STOPPING
                    # Ignore AUTO_PAUSE if already paused
                    elif event == TriggerEvent.AUTO_PAUSE:
                         logger.debug("Ignoring AUTO_PAUSE event while already PAUSED.")
                    else:
                         logger.warning(f"Unexpected event '{event.name}' received in PAUSED state.")

                except asyncio.CancelledError:
                    logger.info("PAUSED state wait cancelled.")
                    current_state = AppState.STOPPING
                except Exception as e:
                    logger.error(f"Error in PAUSED state: {e}", exc_info=True)
                    current_state = AppState.STOPPING # Stop on error?

            elif current_state == AppState.STOPPING:
                print(">>> Current State: STOPPING <<<", flush=True)
                logger.info("Entered STOPPING state.")
                if client:
                    logger.info("Stopping Lightberry client...")
                    try:
                        await client.stop() # This call now includes its own timeout handling
                        logger.info("Client stopped.")
                    except asyncio.TimeoutError:
                         logger.error("Client stop timed out during STOPPING state.")
                         # Continue shutdown even if client timed out
                    except Exception as e:
                         logger.error(f"Error stopping client: {e}", exc_info=True)
                client = None # Clear client reference

                if trigger_controller:
                    logger.info("Stopping trigger listener...")
                    try:
                         trigger_controller.stop_listening()
                         logger.info("Trigger listener stopped.")
                    except Exception as e:
                         logger.error(f"Error stopping trigger listener: {e}")
                 # Don't stop the trigger controller instance itself, just the listener

                logger.info("Transitioning back to IDLE state.")
                current_state = AppState.IDLE
                await asyncio.sleep(1) # Brief pause before restarting listener in IDLE
                # REMOVED: stop_event.set()
                # REMOVED: break

            else:
                logger.error(f"Reached unexpected state: {current_state}. Resetting to IDLE.")

            # Small sleep to prevent tight loop in case of unexpected state transitions or errors
            # await asyncio.sleep(0.05)

    except asyncio.CancelledError:
         logger.info("Main loop task cancelled.")
         # Ensure cleanup path is taken even on cancellation
         current_state = AppState.STOPPING
    except Exception as e:
        logger.error(f"Unhandled exception in main state machine loop: {e}", exc_info=True)
        current_state = AppState.STOPPING # Ensure cleanup path
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
