import logging
import os
from typing import Optional, Callable, Union
import numpy as np
import time # Added time
import json # Added json
import uuid # Added for device ID
import os.path # Added for device ID file path
import aiohttp # Added for async HTTP requests
# from dotenv import load_dotenv # For API keys - No longer needed for token/room
from datetime import datetime # Added import
import sys # Import sys for stderr redirection

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

ASSISTANT_IDENTITY = os.getenv("ASSISTANT_IDENTITY", "assistant")
ASSISTANT_ROOM_NAME = os.getenv("LIVEKIT_ROOM_NAME", "lightberry")
DEVICE_ID = os.getenv("DEVICE_ID", "rec0BGngnqiV7Nkl2")
#LIVEKIT_ROOM_NAME="lightberry"
LIVEKIT_URL="wss://lb-ub8o0q4v.livekit.cloud"
TIMING_LOG_DIR="timing_logs"

logger = logging.getLogger("LightberryLocalClient")
logger.info(f"Using standard audio sample rate: {LIVEKIT_SAMPLE_RATE}Hz") # Use standard rate
# set logger level to info
logger.setLevel(logging.INFO)

AUTH_API_URL = "https://lightberry.vercel.app/api/authenticate/{}" # Placeholder for device ID

def get_or_create_device_id() -> str:
    """Gets the device ID from a local file or creates a new one."""
    return DEVICE_ID

async def get_credentials(device_id: str, username: str) -> tuple[Optional[str], Optional[str]]:
    """Fetches LiveKit token and room name from the authentication API."""
    url = AUTH_API_URL.format(device_id)
    payload = {"username": username}
    logger.info(f"Requesting credentials from {url} for username '{username}'")

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
                        logger.error("API response missing token or room name.")
                        return None, None
                else:
                    error_msg = data.get("error", "Unknown error")
                    logger.error(f"API request failed: {error_msg}")
                    return None, None
    except aiohttp.ClientError as e:
        logger.error(f"HTTP request error during authentication: {e}")
        return None, None
    except json.JSONDecodeError as e:
        logger.error(f"Failed to decode JSON response from authentication API: {e}")
        return None, None
    except Exception as e:
        logger.error(f"An unexpected error occurred during credential fetching: {e}", exc_info=True)
        return None, None

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
        self._pause_input_event.set()
        self.logger.debug("IOHandler: Input transfer resumed.")

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
        """(Private) Clears source input queue, internal queue, save buffer, and processor transcript queue."""
        self.logger.debug("IOHandler: Clearing all input buffers...")
        # 1. Clear the buffer used for saving audio
        #await self._empty_queue(self._user_audio_buffer_for_saving)
        #self.logger.debug("IOHandler: Save buffer cleared.")

        # 2. Clear the internal input queue
        await self._empty_queue(self._internal_input_queue)
        self.logger.debug("IOHandler: Internal input queue cleared.")

        # 3. Clear the source queue in the audio input component
        try:
            input_queue = self.audio_input.get_audio_chunk_queue()
            await self._empty_queue(input_queue)
            self.logger.debug("IOHandler: Source audio input queue cleared.")
        except AttributeError:
            self.logger.warning("IOHandler: Cannot clear source input queue - audio_input lacks get_audio_chunk_queue.")
        except Exception as e:
            self.logger.error(f"IOHandler: Error clearing source input component queue: {e}")

        self.logger.debug("IOHandler: All input buffers cleared.")

    async def _empty_queue(self, q: asyncio.Queue):
        """Helper to quickly empty an asyncio queue."""
        # Note: This is duplicated from StreamingClient, consider a utils file?
        while not q.empty():
            try:
                q.get_nowait()
                q.task_done()
            except asyncio.QueueEmpty:
                break
            except Exception as e:
                self.logger.error(f"IOHandler: Error emptying queue: {e}")
                break # Avoid potential infinite loop

    # Add start/stop methods to conform to interface (might be no-ops)
    async def start(self):
        self.logger.debug("AudioIOHandler start called. Starting underlying components and transfer task...")
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
        
        # Ensure the pause event is set so the transfer task doesn't block indefinitely
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
                chunk = await asyncio.wait_for(source_queue.get(), timeout=0.5)
                
                # If we got a chunk, put it into the internal queue
                try:
                    # Use a short timeout for the internal queue put
                    await asyncio.wait_for(self._internal_input_queue.put(chunk), timeout=0.1)
                except asyncio.QueueFull:
                    self.logger.warning("IOHandler Transfer Loop: Internal input queue full, discarding chunk.")
                    # Need to mark task_done on internal queue if we add tracking there
                except asyncio.TimeoutError:
                    self.logger.warning("IOHandler Transfer Loop: Timeout putting chunk into internal queue.")
                finally:
                     source_queue.task_done() # Mark as done on the source queue regardless

            except asyncio.TimeoutError:
                # Normal timeout while waiting for source queue, check if stopping
                if self._transfer_input_task and self._transfer_input_task.cancelled():
                     self.logger.info("IOHandler Transfer Loop: Task cancelled while waiting for source queue.")
                     break
                # Otherwise, just continue loop
                continue
            except asyncio.CancelledError:
                self.logger.info("IOHandler Transfer Loop: Cancelled.")
                break
            except Exception as e:
                self.logger.error(f"IOHandler Transfer Loop: Error: {e}", exc_info=True)
                # Avoid tight loop on unexpected error
                await asyncio.sleep(0.1)
        
        self.logger.info("AudioIOHandler: Input transfer loop finished.")


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
            self.logger.info("PyAudio stream opened for input.") # This never gets called

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

                    if not self._is_running: break

                    # --- Add timing and put chunk (without timestamp) onto queue ---
                    chunk_id = -1 # Default value if ID callback fails
                    t_capture = time.time()
                    try:
                        if self._get_chunk_id:
                            chunk_id = self._get_chunk_id()
                        else:
                            self.logger.warning("Chunk ID callback missing, cannot get ID.")

                        if self._log_timing and chunk_id != -1:
                            self._log_timing('mic_capture', chunk_id, timestamp=t_capture)

                        # Put chunk, ID, and capture time into queue
                        item_to_put = (audio_chunk, chunk_id, t_capture)

                        try:
                            # Use put_nowait for immediate check
                            self._audio_chunk_queue.put_nowait(item_to_put)
                        except asyncio.QueueFull:
                            # If the queue is full (likely because IOHandler input is paused),
                            # log that we are discarding the chunk.
                            self.logger.warning(f"PyAudio input queue full (likely paused), discarding chunk {chunk_id}.")
                            if self._log_timing and chunk_id != -1:
                                self._log_timing('discard_mic_q_full', chunk_id, timestamp=time.time())
                            # No need to wait or retry, just drop the chunk by doing nothing else.
                        # Removed TimeoutError handling as put_nowait raises QueueFull directly

                    except Exception as e:
                        # Catch potential errors from callbacks or queue logic
                        self.logger.error(f"Error in PyAudio put logic for chunk {chunk_id}: {e}", exc_info=True)

                    # -------------------------------------------------------------

                    # Small yield
                    await asyncio.sleep(0.001)

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
        """Returns the queue where raw audio chunks are placed."""
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

        # Clear the queue on stop? Optional, depends if remaining data is needed.
        # Let's clear it to avoid processing stale data on restart.
        while not self._audio_chunk_queue.empty():
            try:
                self._audio_chunk_queue.get_nowait()
                self._audio_chunk_queue.task_done()
            except asyncio.QueueEmpty:
                break
            except Exception as e:
                self.logger.error(f"Error clearing input audio queue during stop: {e}")
                break

        self.logger.info("PyAudio microphone input stopped.")


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
                        self.logger.debug(f"Playback worker: Attempting to write {chunk_len} float32 samples to stream...")
                        await self._loop.run_in_executor(None, self._stream.write, audio_float32)
                        self.logger.debug(f"Playback worker: Successfully wrote {chunk_len} float32 samples to stream.")
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
             self._playback_finished_event.set() # <<< Signal that playback is truly finished

    async def send_audio_chunk(self, chunk: bytes):
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
                await self.send_audio_chunk(None) 
                #self._audio_queue.put_nowait(None)
                self.logger.debug("Enqueued None sentinel for end of utterance.")
            except asyncio.QueueFull:
                self.logger.warning("Audio queue full when trying to enqueue None sentinel.")
                # If full, the worker might process it later, 
                # but the finished event might be delayed.
            # Remove the immediate check/set of the event here
            # if self._audio_queue.empty():
            #      self.logger.debug("Audio queue empty after signaling end of speech, setting finished event.")
            #      self._playback_finished_event.set()
        # else: # Avoid logging if called redundantly
            # self.logger.debug("signal_end_of_speech called but not speaking.")


    async def stop(self):
        if not self._is_running:
            return
        self.logger.info("Stopping Sounddevice speaker output...")
        self._is_running = False # Signal stop

        # 1. Signal and wait for the single playback task to finish
        if self._playback_task and not self._playback_task.done():
            try:
                # Wait for the worker to finish processing existing items and stop
                await asyncio.wait_for(self._playback_task, timeout=5.0)
            except asyncio.TimeoutError:
                self.logger.warning("Timeout waiting for playback worker to finish, cancelling.")
                self._playback_task.cancel()
                # Wait briefly for cancellation to be processed
                await asyncio.sleep(0.1)
            except Exception as e:
                 self.logger.error(f"Error stopping playback worker: {e}", exc_info=True)
                 if self._playback_task and not self._playback_task.done(): self._playback_task.cancel(); await asyncio.sleep(0.1)

        self._playback_task = None # Clear the task reference

        # 2. Close the sounddevice stream
        if self._stream:
            stream_to_close = self._stream
            self._stream = None # Prevent worker from using it after this point
            try:
                 # Sounddevice stream needs stop() and close()
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
        # Also check if the playback task is active and queue has items?
        # Simple check for now:
        return self._is_speaking

    def get_playback_finished_event(self) -> asyncio.Event:
         """Returns the event that signals playback completion."""
         return self._playback_finished_event

    
class LocalIOHandler(AudioIOHandler):
    def __init__(self,
                 audio_input: AudioInputInterface,
                 audio_output: AudioOutputInterface,
                 client_instance: 'LightberryLocalClient', # Added client instance
                 logger_instance: Optional[logging.Logger] = None):
        super().__init__(audio_input, audio_output, None, logger_instance)
        self._client = client_instance # Store client instance
        # Pass client callbacks to the audio input instance if it supports them
        if hasattr(self.audio_input, '_log_timing') and hasattr(self.audio_input, '_get_chunk_id'):
            self.logger.info("Passing timing/chunk ID callbacks to PyAudioMicrophoneInput.")
            self.audio_input._log_timing = self._client._log_timing
            self.audio_input._get_chunk_id = self._client._get_next_chunk_id
        else:
            self.logger.warning("Audio input interface does not support timing/chunk ID callbacks.")

        self.receiving_output = False
        self.internal_output_queue = asyncio.Queue()
        self._transfer_output_task: Optional[asyncio.Task] = None # Task for the transfer loop
        self.silence_count = 0
        self.pause_input_event = asyncio.Event()
        self.pause_input_event.clear() # set to false until livekit drain is started.

    # --- Override _transfer_input_loop to add timing ---
    async def _transfer_input_loop(self):
        """(Overridden) Continuously transfers audio chunks from input source to internal queue, adding timing data."""
        source_queue = self.audio_input.get_audio_chunk_queue()
        if source_queue is None:
            self.logger.error("IOHandler: Could not get source audio queue. Input transfer cannot start.")
            return

        self.logger.info("IOHandler: Starting input transfer loop with timing.")
        while True:
            await self._pause_input_event.wait() # Wait if paused

            try:
                # Expecting (chunk_data, chunk_id, t_capture) from PyAudioMicrophoneInput
                chunk_data, chunk_id, t_capture = await source_queue.get()
                t_get_mic_q = time.time()
                source_queue.task_done()

                # Calculate mic queue wait time
                mic_q_wait = t_get_mic_q - t_capture
                # Log get_mic_q event with wait time
                self._client._log_timing('get_mic_q', chunk_id, timestamp=t_get_mic_q, wait_time=mic_q_wait)

                # Put (chunk_data, chunk_id, put_time) into internal queue
                t_put_internal_q = time.time()
                item_for_internal_q = (chunk_data, chunk_id, t_put_internal_q)

                # Log put_internal_q event
                self._client._log_timing('put_internal_q', chunk_id, timestamp=t_put_internal_q)

                # Add to internal queue (used by LightberryLocalClient._forward_mic_to_livekit)
                try:
                    await self._internal_input_queue.put(item_for_internal_q)
                    # Log event moved here, only log if put is successful
                    # self._client._log_timing('put_internal_q', chunk_id, timestamp=t_put_internal_q)
                except asyncio.TimeoutError:
                    self.logger.warning(f"IOHandler: Timeout putting chunk {chunk_id} into internal queue. Queue possibly full.")
                    # Log the timeout event for this specific chunk
                    self._client._log_timing('timeout_put_internal_q', chunk_id, timestamp=time.time())
                    continue # Skip saving if internal transfer failed
                except asyncio.QueueFull: # asyncio.Queue doesn't raise QueueFull on timeout, handled by TimeoutError
                    self.logger.warning(f"IOHandler: Internal queue full, dropping chunk {chunk_id}.")
                    self._client._log_timing('discard_internal_q_full', chunk_id, timestamp=time.time())
                    continue

                # Add original chunk data to save buffer (no timing info needed here)
                if False and self._user_audio_buffer_for_saving is not None:
                    try:
                        # Still save original chunk data without timing info
                        await asyncio.wait_for(self._user_audio_buffer_for_saving.put(chunk_data), timeout=0.1)
                    except asyncio.TimeoutError:
                        self.logger.warning("IOHandler: Timeout putting chunk into save buffer. Buffer full.")

            except asyncio.TimeoutError:
                # No audio chunk received within timeout, continue loop
                continue
            except asyncio.CancelledError:
                self.logger.info("IOHandler Input Transfer Loop: Cancelled.")
                break
            except Exception as e:
                self.logger.error(f"IOHandler Input Transfer Loop: Error processing audio chunk: {e}", exc_info=True)
                # Avoid tight loop on unexpected error
                await asyncio.sleep(0.1)
        self.logger.info("IOHandler: Input transfer loop finished.")
    # --- End Override ---

    async def _clear_input_buffers(self):
        #need to reimplement this from parent class, since the parent class clears the input processor queue which we don't want
        """(Private) Clears source input queue, internal queue, save buffer, and processor transcript queue."""
        self.logger.debug("IOHandler: Clearing all input buffers...")
        # 1. Clear the buffer used for saving audio
        await self._empty_queue(self._user_audio_buffer_for_saving)
        self.logger.debug("IOHandler: Save buffer cleared.")

        # 2. Clear the internal input queue
        # Note: Items now contain (chunk, chunk_id, put_time)
        await self._empty_queue(self._internal_input_queue)
        self.logger.debug("IOHandler: Internal input queue cleared.")

        # 3. Clear the source queue in the audio input component
        # Note: Items now contain (chunk, chunk_id, capture_time)
        try:
            # --- Add diagnostic logging ---
            self.logger.debug(f"IOHandler: Checking audio_input type: {type(self.audio_input)}")
            has_method = hasattr(self.audio_input, 'get_audio_chunk_queue')
            self.logger.debug(f"IOHandler: hasattr(self.audio_input, 'get_audio_chunk_queue'): {has_method}")
            # --- End diagnostic logging ---
            input_queue = self.audio_input.get_audio_chunk_queue()
            await self._empty_queue(input_queue)
            self.logger.debug("IOHandler: Source audio input queue cleared.")
        except AttributeError:
            self.logger.warning("IOHandler: Cannot clear source input queue - audio_input lacks get_audio_chunk_queue.")
        except Exception as e:
            self.logger.error(f"IOHandler: Error clearing source input component queue: {e}")

        self.logger.debug("IOHandler: All input buffers cleared.")

    
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
        """Signals the end of assistant speech, waits for playback, cleans input buffers, resumes input transfer, and saves timing data."""
        # Explicitly end the output in the underlying component.
        # The _transfer_output_loop should not receive a None sentinel.
        # Remove the incorrect send_output_chunk(None) call:
        # await self.send_output_chunk(None) 

        await self.audio_output.signal_end_of_speech()
        # Wait for the component to signal completion via the event
        await self._wait_for_output_completion()
        # Clean input buffers after playback is fully finished
        self.logger.info("IOHandler: Playback finished, clearing input buffers.")
        await self._clear_input_buffers()
        self.receiving_output = False

        # --- Save Timing Data ---
        self.logger.info("IOHandler: Saving collected timing data...")
        self._client.save_timing_data()
        self.logger.info("IOHandler: Timing data saved.")
        # --- End Save Timing Data ---

        # Resume the input transfer loop *after* buffers are cleared and data saved
        self._pause_input_event.set()
        self.logger.info("IOHandler: Input transfer resumed.")

    async def send_output_chunk(self, chunk: bytes):
        await self.internal_output_queue.put(chunk)

    async def _transfer_output_loop(self):
        """
        Main loop for transferring audio data from the internal queue to the speaker.
        Detects start/end of speech based on the chunks.
        """
        def detect_speech_start(chunk: bytes):
            #detect start and end of assistant speech
            #if we detect that the received audio chunk has volume above a threshold and self.receiving_output is False, we have detected the start of assistant speech
            if not self.receiving_output and np.mean(np.abs(np.frombuffer(chunk, dtype=np.int16))) > 50:
                self.receiving_output = True
                self.logger.debug("IOHandler: Detected start of assistant speech.")
                return True
            return False
        
        def detect_speech_end(chunk: bytes):
            #detect end of assistant speech
            #if we detect that the received audio chunk has volume below a threshold and self.receiving_output is True, we have detected the end of assistant speech
            if self.receiving_output and np.mean(np.abs(np.frombuffer(chunk, dtype=np.int16))) < 50:
                self.logger.debug(f"IOHandler: Detected silence. Silence count: {self.silence_count}")
                if self.silence_count > 50:#this is a hack to prevent false positives
                    self.logger.debug("IOHandler: Detected end of assistant speech.")
                    return True
                else:
                    self.silence_count += 1
                    return False
            else:
                self.silence_count = 0
            return False

        while True:
            try:
                # Wait for a chunk with a timeout
                chunk = await asyncio.wait_for(self.internal_output_queue.get(), timeout=0.2) 
                self.internal_output_queue.task_done() # Mark task done if chunk received

                # Detect start/end based on the chunk content
                if detect_speech_start(chunk):
                    await self.signal_start_of_speech()
                elif detect_speech_end(chunk):
                    await self.signal_end_of_speech()
                
                # Send the received chunk to the speaker
                await self.audio_output.send_audio_chunk(chunk)

            except asyncio.TimeoutError:
                # No chunk received within the timeout
                if self.receiving_output:
                    self.logger.info("IOHandler: Timeout waiting for next output chunk, assuming end of speech.")
                if self.audio_output.is_speaking:
                    # If we were receiving speech, assume it has ended due to timeout
                    self.logger.info("IOHandler: Detected end of speech due to timeout.")
                    self.receiving_output = False # Update state first
                    await self.signal_end_of_speech()
                # Continue waiting for the next chunk or further timeout
                continue
            except asyncio.CancelledError:
                self.logger.info("IOHandler Transfer Output Loop: Cancelled.")
                break
            except Exception as e:
                self.logger.error(f"IOHandler Transfer Output Loop: Error: {e}", exc_info=True)
                await asyncio.sleep(0.1) # Avoid tight loop on unexpected error
            
    async def start(self):
        self.logger.debug("AudioIOHandler start called. Starting underlying components and transfer task...")
        await self.audio_input.start()
        await self.audio_output.start()
        # Start the input transfer task
        if self._transfer_input_task is None or self._transfer_input_task.done():
            self._transfer_input_task = asyncio.create_task(self._transfer_input_loop(), name="IOHandlerTransferInput")
            self.logger.info("AudioIOHandler: Started input transfer task.")
        # Start the output transfer task
        if self._transfer_output_task is None or self._transfer_output_task.done():
            self._transfer_output_task = asyncio.create_task(self._transfer_output_loop(), name="IOHandlerTransferOutput")
            self.logger.info("AudioIOHandler: Started output transfer task.")
        else:
            self.logger.warning("AudioIOHandler: Output transfer task already running?")
    

class LightberryLocalClient:
    def __init__(self, url: str, token: str, room_name: str, loop: asyncio.AbstractEventLoop):
        self.livekit_url = url
        self.livekit_token = token
        self.room_name = room_name # Though token usually defines the room
        self._loop = loop
        self._room: Optional[rtc.Room] = None

        # --- Timing Infrastructure ---
        self.timing_data = []
        self.chunk_id_counter = 0
        self.timing_log_file_path = self._generate_timing_log_path()
        self._timing_lock = asyncio.Lock() # Lock for accessing timing_data/counter
        # --- End Timing Infrastructure ---

        # --- Instantiate PyAudio components with LIVEKIT_SAMPLE_RATE (48kHz) ---
        self._mic_input: PyAudioMicrophoneInput = PyAudioMicrophoneInput(
            loop=self._loop,
            sample_rate=LIVEKIT_SAMPLE_RATE, # Use standard rate
            frames_per_buffer=480, # Align chunk size with 10ms LiveKit frames (480 samples = 960 bytes)
            logger_instance=logger.getChild("MicInput"),
            log_timing_cb=self._log_timing,       # Pass timing callback
            get_chunk_id_cb=self._get_next_chunk_id, # Pass chunk ID callback
            input_device_index=None # <<< Use default input device index
            )
        # --- Instantiate Speaker with LIVEKIT_SAMPLE_RATE --- 
        self._speaker_output: SoundDeviceSpeakerOutput = SoundDeviceSpeakerOutput(
            loop=self._loop, 
            sample_rate=LIVEKIT_SAMPLE_RATE, # Use standard rate
            channels=1, # Keep channels as 1 for speaker output
            logger_instance=logger.getChild("SpeakerOutput"),
            output_device_index=None # <<< Use default output device index
            )
        self._local_io_handler = LocalIOHandler(self._mic_input, self._speaker_output, self, logger.getChild("LocalIOHandler"))
        # --- End PyAudio Instantiation ---
        self._mic_audio_source: Optional[rtc.AudioSource] = None
        self._mic_track_pub: Optional[rtc.LocalTrackPublication] = None
        self._forward_mic_task: Optional[asyncio.Task] = None
        self._playback_tasks: dict[str, asyncio.Task] = {} # track_sid -> playback task
        self._stop_event = asyncio.Event()
        # --- Event to signal mic source is ready ---
        self._mic_source_ready = asyncio.Event()
        logger.info(f"LightberryLocalClient initialized. Timing logs will be saved to '{self.timing_log_file_path}'")
        # Remove obsolete TODO 
        # TODO: Ensure PyAudio components are configured for LOCAL_SAMPLE_RATE
        # This might require passing the rate to their constructors or modifying them
        # if they currently rely solely on constants from streaming_client.constants

    def _register_listeners(self):
        if not self._room:
            return
        self._room.on("track_subscribed", self._on_track_subscribed) 
        self._room.on("disconnected", self._on_disconnected)
        logger.info("Room event listeners registered.")

    async def connect(self):
        logger.info(f"Attempting to connect to LiveKit URL: {self.livekit_url} in room '{self.room_name}'...")
        try:
            self._room = rtc.Room(loop=self._loop)
            self._register_listeners() # Register listeners before connecting
            await self._room.connect(self.livekit_url, self.livekit_token)
            logger.info(f"Successfully connected to LiveKit room: {self._room.name} (SID: {await self._room.sid})")
        except rtc.ConnectError as e:
            logger.error(f"Failed to connect to LiveKit room: {e}", exc_info=True)
            self._room = None # Ensure room is None on failure
            raise # Re-raise the exception so caller knows connection failed

    async def start(self):
        logger.info("Starting LightberryLocalClient...")
        self._stop_event.clear()
        try:
            # Start the IO Handler (which starts underlying components and its transfer loop)
            await self._local_io_handler.start() 
            await self.connect() # Connect to the room
            
            if self._room: # Only proceed if connection was successful
                await self._create_and_publish_mic_track()
                # Start the microphone forwarding task if source exists
                if self._mic_audio_source:
                     self._forward_mic_task = asyncio.create_task(self._forward_mic_to_livekit(), name="ForwardMicToLiveKit")
                     logger.info("Microphone forwarding task started.")
                else:
                     logger.error("Could not start microphone forwarding: AudioSource not created.")
                
                logger.info("Client started successfully.")
            else:
                 logger.error("Client start failed because room connection failed.")
                 # Optionally stop mic/speaker if connection fails?
                 await self.stop() # Attempt cleanup

        except Exception as e:
             logger.error(f"Error during client start: {e}", exc_info=True)
             await self.stop() # Ensure cleanup on error
             raise

    async def stop(self):
        if self._stop_event.is_set():
            logger.debug("Stop already initiated.")
            return
        logger.info("Stopping LightberryLocalClient...")
        self._stop_event.set()

        # Clear mic source ready event on stop
        self._mic_source_ready.clear()
        # --- Cancel running tasks ---
        tasks_to_cancel = []
        if self._forward_mic_task and not self._forward_mic_task.done():
            tasks_to_cancel.append(self._forward_mic_task)
        tasks_to_cancel.extend(task for task in self._playback_tasks.values() if task and not task.done())
        
        if tasks_to_cancel:
             logger.info(f"Cancelling {len(tasks_to_cancel)} background tasks...")
             for task in tasks_to_cancel:
                 task.cancel()
             await asyncio.gather(*tasks_to_cancel, return_exceptions=True)
             logger.info("Background tasks cancelled.")
        self._forward_mic_task = None
        self._playback_tasks.clear()
        
        # Stop IO handler (which stops underlying components and its transfer loop)
        if self._local_io_handler:
            await self._local_io_handler.stop()
        # Remove direct stopping of mic/speaker
        # io_stop_tasks = []
        # if self._mic_input:
        #      io_stop_tasks.append(asyncio.create_task(self._mic_input.stop(), name="MicInputStop"))
        # if self._speaker_output:
        #      io_stop_tasks.append(asyncio.create_task(self._speaker_output.stop(), name="SpeakerOutputStop"))
        # if io_stop_tasks:
        #      await asyncio.gather(*io_stop_tasks, return_exceptions=True)
        #      logger.info("Microphone input and speaker output stopped.")

        # Unpublish mic track
        if self._mic_track_pub and self._room and self._room.local_participant:
            logger.info(f"Unpublishing microphone track {self._mic_track_pub.sid}...")
            try:
                await self._room.local_participant.unpublish_track(self._mic_track_pub.sid)
                logger.info("Microphone track unpublished.")
            except Exception as e:
                 logger.error(f"Error unpublishing mic track: {e}")
        self._mic_track_pub = None
        self._mic_audio_source = None # Assuming source is tied to track lifecycle

        # Disconnect from room
        room_to_disconnect = self._room
        self._room = None
        if room_to_disconnect and getattr(room_to_disconnect, 'is_connected', False):
            logger.info("Disconnecting from LiveKit room...")
            try:
                await room_to_disconnect.disconnect()
                logger.info("Disconnected from LiveKit room successfully.")
            except Exception as e:
                 logger.error(f"Error during room disconnect: {e}", exc_info=True)
        else:
             logger.info("Already disconnected or room not initialized.")
        
        logger.info("LightberryLocalClient stopped.")

    async def _create_and_publish_mic_track(self):
        if not self._room or not self._room.local_participant:
            logger.error("Cannot create mic track: Not connected to room.")
            return
        try:
            # Create source with LIVEKIT_SAMPLE_RATE (48kHz)
            logger.info(f"Creating mic AudioSource with rate {LIVEKIT_SAMPLE_RATE}Hz") # Use standard rate
            self._mic_audio_source = rtc.AudioSource(LIVEKIT_SAMPLE_RATE, LIVEKIT_CHANNELS) # Use standard rate
            track = rtc.LocalAudioTrack.create_audio_track("mic-audio", self._mic_audio_source)
            options = rtc.TrackPublishOptions(source=rtc.TrackSource.SOURCE_MICROPHONE)
            logger.info("Publishing microphone audio track...")
            self._mic_track_pub = await self._room.local_participant.publish_track(track, options)
            logger.info(f"Microphone track published successfully (SID: {self._mic_track_pub.sid}).")
            # --- Signal that the mic source is now ready --- 
            self._mic_source_ready.set()
            # --- End Signal ---
        except Exception as e:
            logger.error(f"Failed to create or publish mic track: {e}", exc_info=True)
            self._mic_audio_source = None # Clean up source on error
            self._mic_source_ready.clear() # Ensure event is clear on error

    async def _forward_mic_to_livekit(self):
        """Task to read timed audio chunks from IOHandler queue and push frames to LiveKit AudioSource."""
        if not self._mic_audio_source:
            logger.error("Cannot forward mic audio: AudioSource is not available.")
            return
        
        try:
            # Get queue from IO Handler (contains (chunk_data, chunk_id, t_capture))
            internal_queue = self._local_io_handler._internal_input_queue

            # Calculate frame properties (should match input buffer now)
            samples_per_frame = 480 # From frames_per_buffer
            bytes_per_frame = samples_per_frame * LIVEKIT_CHANNELS * 2 # 960

            logger.info(f"Starting microphone forwarding loop with timing (Expecting {bytes_per_frame}-byte frames)")
            await self._local_io_handler._clear_input_buffers() # Clear any buffered input
            self._local_io_handler._pause_input_event.set() # Start unpaused
            while True:
                try:
                    # Get chunk, ID, and put_time from IO Handler's internal queue
                    chunk_data, chunk_id, t_put_internal_q = await asyncio.wait_for(internal_queue.get(), timeout=1.0)
                    t_get_internal_q = time.time()
                    internal_queue.task_done()
                    
                    # Calculate internal queue wait time
                    internal_q_wait = t_get_internal_q - t_put_internal_q
                    # Log get_internal_q with wait time
                    self._log_timing('get_internal_q', chunk_id, timestamp=t_get_internal_q, wait_time=internal_q_wait)

                    # --- Direct Frame Creation and Send ---
                    # No buffering needed as chunk_data should be bytes_per_frame
                    if len(chunk_data) != bytes_per_frame:
                         logger.warning(f"Chunk {chunk_id} size mismatch! Expected {bytes_per_frame}, got {len(chunk_data)}. Skipping frame.")
                         continue # Skip this inconsistent chunk

                    frame = rtc.AudioFrame(
                        sample_rate=LIVEKIT_SAMPLE_RATE,
                        num_channels=LIVEKIT_CHANNELS,
                        samples_per_channel=samples_per_frame,
                        data=chunk_data # Use the received chunk directly
                    )

                    t_start_send = time.time()
                    await self._mic_audio_source.capture_frame(frame)
                    t_end_send = time.time()
                    send_duration = t_end_send - t_start_send
                    self._log_timing('sent_livekit', chunk_id, timestamp=t_end_send, duration=send_duration)
                    # --- End Direct Frame Creation and Send ---

                except asyncio.TimeoutError:
                    if self._stop_event.is_set():
                         logger.info("Stop event set, exiting mic forward loop.")
                         break
                    continue # Continue if timeout but not stopped
                except Exception as e:
                    logger.error(f"Error in mic forward loop processing chunk {chunk_id if 'chunk_id' in locals() else 'unknown'}: {e}", exc_info=True)
                    # Consider adding chunk_id to error log if available
                    await asyncio.sleep(0.1) # Avoid tight loop on error

        except asyncio.CancelledError:
            logger.info("Microphone forwarding task cancelled.")
        except Exception as e:
             logger.error(f"Fatal error in microphone forwarding task setup: {e}", exc_info=True)
        finally:
             logger.info("Microphone forwarding loop finished.")

    def _on_disconnected(self):
        logger.warning("Disconnected from LiveKit room unexpectedly!")
        # Signal the main loop or trigger cleanup
        self._stop_event.set()
        # Note: Do not await async operations directly in sync event handlers
        # Consider scheduling cleanup if needed: asyncio.create_task(self.stop())?
        # Also clear events here
        self._mic_source_ready.clear()

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
                if self._stop_event.is_set(): break
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
        logger.info(f"Saving {len(self.timing_data)} timing entries to {self.timing_log_file_path}...")
        try:
            # Ensure the directory exists before writing
            os.makedirs(TIMING_LOG_DIR, exist_ok=True)
            with open(self.timing_log_file_path, 'w') as f:
                json.dump(self.timing_data, f, indent=2)
            logger.info("Timing data successfully saved.")
        except Exception as e:
            logger.error(f"Failed to save timing data: {e}", exc_info=True)

        # Reset for the next interaction
        self.timing_data = []
        self.chunk_id_counter = 0
        self.timing_log_file_path = self._generate_timing_log_path() # Get new path for next save
        logger.info(f"Timing log reset. Next log will be: {self.timing_log_file_path}")
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

    # --- Get Device ID and Username ---
    device_id = get_or_create_device_id()
    # For now, let's use a simple default username, or prompt the user
    # username = input("Enter your username: ") # Example: Prompting
    username = "lb-ziv" # Example: Default
    logger.info(f"Using Device ID: {device_id}")
    logger.info(f"Using Username: {username}")


    # --- Fetch Credentials ---
    token, room_name = await get_credentials(device_id, username)
    if not token or not room_name:
        raise Exception("Failed to get credentials. Exiting.")


    # --- Run Client ---
    logger.info("Client setup complete. Credentials obtained.")
    logger.info(f"LiveKit URL: {LIVEKIT_URL}")
    logger.info(f"Room Name: {room_name}")
    # logger.info(f"User ID: {USER_ID}") # User ID is now part of token generation
    logger.info("Attempting to run client...")
    
    client = None # Initialize client to None for finally block
    try:
        # Get the running loop (guaranteed to exist by asyncio.run)
        loop = asyncio.get_running_loop() 
        # Create the client instance, passing the fetched credentials and loop
        client = LightberryLocalClient(LIVEKIT_URL, token, room_name, loop) # Use fetched token and room_name
        # Start the client
        await client.start()
        # Keep the main function alive while the client runs
        # This assumes _stop_event is accessible or we wait indefinitely
        await client._stop_event.wait() 
        logger.info("Client stop event received in main.")
    except Exception as e:
         logger.error(f"An error occurred during client execution in main: {e}", exc_info=True)
    finally:
        logger.info("Main function initiating shutdown...")
        if client:
            await client.stop()
        logger.info("Main function shutdown complete.")

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("Keyboard interrupt received. Shutting down...")
    # Note: General exceptions inside main() are caught within main itself.
    # asyncio.run() will propagate exceptions, but we handle them internally.
    logger.info("Client execution finished.")
