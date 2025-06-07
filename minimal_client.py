#!/usr/bin/env python3
"""
Minimal Lightberry Client - Optimized for Raspberry Pi Zero 2W
Target: 50MB RAM, <25ms latency, 15-20% CPU
"""

import asyncio
import logging
import threading
import time
import os
import wave
from enum import Enum
from typing import Optional, List
from collections import deque
import json
import urllib.request
import urllib.error

# Minimal imports - only what we need
import numpy as np
import alsaaudio
from livekit import rtc, api
from openwakeword.model import Model
from dotenv import load_dotenv

load_dotenv()

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(name)s: %(message)s'
)
logger = logging.getLogger("Lightberry")

# Constants
SAMPLE_RATE = 48000
CHANNELS = 1
CHUNK_SIZE = 960  # 20ms at 48kHz
PERIOD_SIZE = 480  # 10ms periods
SILENCE_THRESHOLD = 20
LIVEKIT_URL = os.getenv("LIVEKIT_URL", "wss://lb-ub8o0q4v.livekit.cloud")
AUTH_API_URL = "https://lightberry.vercel.app/api/authenticate/{}"


# Simple state enum
class State(Enum):
    IDLE = "idle"
    ACTIVE = "active"


class RingBuffer:
    """Lock-free ring buffer with pre-allocated memory"""
    
    def __init__(self, size: int, chunk_size: int):
        # Pre-allocate all memory at startup
        self.buffer = [bytearray(chunk_size) for _ in range(size)]
        self.size = size
        self.chunk_size = chunk_size
        
        # Separate read/write indices (no locks needed)
        self.write_idx = 0
        self.read_idx = 0
        
    def write(self, data: bytes) -> None:
        """Write data - overwrites old if full"""
        slot = self.buffer[self.write_idx]
        bytes_to_copy = min(len(data), self.chunk_size)
        slot[:bytes_to_copy] = data[:bytes_to_copy]
        
        # Atomic increment
        self.write_idx = (self.write_idx + 1) % self.size
        
    def read(self) -> Optional[bytes]:
        """Read oldest data"""
        if self.is_empty():
            return None
            
        data = bytes(self.buffer[self.read_idx])
        self.read_idx = (self.read_idx + 1) % self.size
        return data
        
    def read_last_n(self, n: int) -> List[bytes]:
        """Read last n chunks for wake word detection"""
        chunks = []
        pos = (self.write_idx - n) % self.size
        
        for _ in range(n):
            if pos != self.read_idx:  # Don't read past read pointer
                chunks.append(bytes(self.buffer[pos]))
                pos = (pos + 1) % self.size
                
        return chunks
        
    def is_empty(self) -> bool:
        """Check if buffer is empty"""
        return self.read_idx == self.write_idx
    
    def clear(self) -> None:
        """Reset buffer"""
        self.read_idx = 0
        self.write_idx = 0


class ALSAAudioManager:
    """Direct ALSA audio with predictive echo cancellation"""
    
    def __init__(self):
        self.logger = logger.getChild("Audio")
        
        # Pre-allocate ring buffers
        self.mic_buffer = RingBuffer(size=50, chunk_size=CHUNK_SIZE)
        self.speaker_buffer = RingBuffer(size=50, chunk_size=CHUNK_SIZE)
        
        # State
        self.running = False
        self.capture_muted = False
        self.assistant_speaking = False  # Critical flag for echo prevention
        
        # VAD state (for end-of-speech only)
        self.vad_counter = 0
        self.silence_counter = 0
        
        # ALSA devices (will be initialized in start())
        self.mic_pcm: Optional[alsaaudio.PCM] = None
        self.speaker_pcm: Optional[alsaaudio.PCM] = None
        self.audio_thread: Optional[threading.Thread] = None
        
        # Pre-allocate work buffers
        self._capture_buffer = bytearray(CHUNK_SIZE)
        self._empty_buffer = bytes(CHUNK_SIZE)  # Silence
        
    def start(self) -> None:
        """Start audio system"""
        self.logger.info("Starting ALSA audio system...")
        
        try:
            # Open microphone with non-blocking mode
            self.mic_pcm = alsaaudio.PCM(
                type=alsaaudio.PCM_CAPTURE,
                mode=alsaaudio.PCM_NONBLOCK,
                device='default',
                rate=SAMPLE_RATE,
                channels=CHANNELS,
                format=alsaaudio.PCM_FORMAT_S16_LE,
                periodsize=PERIOD_SIZE
            )
            
            # Open speaker with normal mode
            self.speaker_pcm = alsaaudio.PCM(
                type=alsaaudio.PCM_PLAYBACK,
                mode=alsaaudio.PCM_NORMAL,
                device='default',
                rate=SAMPLE_RATE,
                channels=CHANNELS,
                format=alsaaudio.PCM_FORMAT_S16_LE,
                periodsize=PERIOD_SIZE
            )
            
            # Start single audio thread
            self.running = True
            self.audio_thread = threading.Thread(
                target=self._audio_loop,
                daemon=True,
                name="AudioThread"
            )
            self.audio_thread.start()
            
            self.logger.info("Audio system started - 20ms max latency")
            
        except Exception as e:
            self.logger.error(f"Failed to start audio: {e}")
            raise
    
    def stop(self) -> None:
        """Stop audio system"""
        self.logger.info("Stopping audio system...")
        self.running = False
        
        if self.audio_thread:
            self.audio_thread.join(timeout=2.0)
            
        if self.mic_pcm:
            self.mic_pcm.close()
        if self.speaker_pcm:
            self.speaker_pcm.close()
            
        self.logger.info("Audio system stopped")
    
    def receive_audio_from_livekit(self, chunk: bytes) -> None:
        """Called when assistant audio arrives - PREDICTIVE FLAG SET HERE"""
        # CRITICAL: Set flag IMMEDIATELY - before audio plays
        self.assistant_speaking = True
        self.silence_counter = 0
        
        # Queue for playback
        self.speaker_buffer.write(chunk)
    
    def set_capture_muted(self, muted: bool) -> None:
        """Mute/unmute microphone capture"""
        self.capture_muted = muted
        self.logger.info(f"Microphone {'muted' if muted else 'unmuted'}")
    
    async def play_sound(self, wav_data: bytes) -> None:
        """Play a pre-loaded WAV file"""
        # For simplicity, write directly to speaker buffer
        # In production, might want to mix with assistant audio
        for i in range(0, len(wav_data), CHUNK_SIZE):
            chunk = wav_data[i:i + CHUNK_SIZE]
            if len(chunk) < CHUNK_SIZE:
                # Pad with silence
                chunk = chunk + self._empty_buffer[:CHUNK_SIZE - len(chunk)]
            self.speaker_buffer.write(chunk)
        
        # Wait for playback to complete (rough estimate)
        duration = len(wav_data) / (SAMPLE_RATE * 2)  # 2 bytes per sample
        await asyncio.sleep(duration)
    
    def _audio_loop(self) -> None:
        """Single thread for all audio I/O"""
        consecutive_read_errors = 0
        
        while self.running:
            # CAPTURE - gated by assistant_speaking flag
            if not self.assistant_speaking and not self.capture_muted:
                try:
                    length, data = self.mic_pcm.read()
                    if length > 0:
                        self.mic_buffer.write(data)
                        consecutive_read_errors = 0
                except alsaaudio.ALSAAudioError as e:
                    if e.errno == -32:  # Broken pipe
                        consecutive_read_errors += 1
                        if consecutive_read_errors > 10:
                            self.logger.error("Too many ALSA errors, reinitializing...")
                            # Could reinitialize here
                    elif e.errno != -11:  # Ignore EAGAIN
                        self.logger.debug(f"Capture error: {e}")
            
            # PLAYBACK - always process if available
            if not self.speaker_buffer.is_empty():
                chunk = self.speaker_buffer.read()
                
                # Always play immediately (no delay)
                try:
                    self.speaker_pcm.write(chunk)
                except alsaaudio.ALSAAudioError as e:
                    self.logger.error(f"Playback error: {e}")
                
                # VAD for end-of-speech only (can be lazy)
                self.vad_counter += 1
                if self.vad_counter % 5 == 0:  # Every 50ms
                    energy = self._calculate_energy_fast(chunk)
                    
                    if energy < SILENCE_THRESHOLD:
                        self.silence_counter += 1
                        if self.silence_counter > 4:  # 200ms silence
                            if self.assistant_speaking:
                                self.logger.debug("Assistant stopped speaking")
                            self.assistant_speaking = False
                            self.silence_counter = 0
                    else:
                        self.silence_counter = 0
            
            # Small sleep to prevent CPU spinning
            time.sleep(0.001)
    
    def _calculate_energy_fast(self, chunk: bytes) -> float:
        """Optimized energy calculation"""
        # Sample every 20th value for speed
        samples = np.frombuffer(chunk[::40], dtype=np.int16)
        return float(np.mean(np.abs(samples))) if len(samples) > 0 else 0.0


class WakeWordDetector:
    """Activity-gated wake word detection"""
    
    def __init__(self, audio_manager: ALSAAudioManager, event_queue: asyncio.Queue):
        self.logger = logger.getChild("WakeWord")
        self.audio_manager = audio_manager
        self.event_queue = event_queue
        
        # Pre-load models at init
        self.logger.info("Loading wake word models...")
        try:
            # Import c_resampler for downsampling
            import c_resampler
            self.c_resampler = c_resampler
            
            self.model = Model(
                wakeword_models=[
                    "wakeword_models/Hi_Bradford.onnx",
                    "wakeword_models/go_to_sleep.onnx"
                ],
                inference_framework='onnx'
            )
            self.logger.info(f"Loaded models: {list(self.model.models.keys())}")
        except Exception as e:
            self.logger.error(f"Failed to load wake word models: {e}")
            raise
        
        # Activity gating
        self.last_activity_time = 0
        self.activity_threshold = 10  # Energy threshold
        
        self.running = False
        self.thread: Optional[threading.Thread] = None
        
    def start(self) -> None:
        """Start detection thread"""
        self.running = True
        self.thread = threading.Thread(
            target=self._detection_loop,
            daemon=True,
            name="WakeWordThread"
        )
        self.thread.start()
        self.logger.info("Wake word detection started")
        
    def stop(self) -> None:
        """Stop detection"""
        self.running = False
        if self.thread:
            self.thread.join(timeout=2.0)
        self.logger.info("Wake word detection stopped")
        
    def _detection_loop(self) -> None:
        """Continuous wake word detection - processes all audio"""
        # Use a circular buffer to always keep last 60ms of audio
        # This ensures we don't miss wake phrases with pauses
        process_counter = 0
        
        while self.running:
            # Always process audio every 30ms (3 chunks)
            # This ensures we catch wake phrases even with pauses
            process_counter += 1
            
            # Get 60ms of audio (6 chunks) - sliding window
            # This gives the model more context for detection
            chunks = self.audio_manager.mic_buffer.read_last_n(6)
            
            if len(chunks) >= 3:  # Need at least 30ms
                # Use the most recent chunks for processing
                audio_data = b''.join(chunks[-3:])  # Last 30ms
                
                try:
                    # Downsample to 16kHz using C extension
                    downsampled = self.c_resampler.decimate(audio_data)
                    
                    # Run inference
                    audio_array = np.frombuffer(downsampled, dtype=np.int16)
                    self.model.predict(audio_array)
                    
                    # Check scores
                    scores = self.model.prediction_buffer
                    
                    # Check for wake word
                    wake_score = scores.get("Hi_Bradford", [0])[-1]
                    if wake_score > 0.5:
                        # Prevent spam by checking time since last detection
                        if time.time() - self.last_activity_time > 2.0:
                            self.logger.info(f"Wake word detected! Score: {wake_score:.2f}")
                            self._put_event_nowait("WAKE_DETECTED")
                            self.last_activity_time = time.time()
                    
                    # Check for stop word
                    stop_score = scores.get("go_to_sleep", [0])[-1]
                    if stop_score > 0.5:
                        if time.time() - self.last_activity_time > 2.0:
                            self.logger.info(f"Stop word detected! Score: {stop_score:.2f}")
                            self._put_event_nowait("STOP_WORD_DETECTED")
                            self.last_activity_time = time.time()
                    
                    # Log periodically for debugging (every ~3 seconds)
                    if process_counter % 100 == 0:
                        self.logger.debug(f"Wake word processing active - wake: {wake_score:.3f}, stop: {stop_score:.3f}")
                        
                except Exception as e:
                    if process_counter % 100 == 0:  # Don't spam errors
                        self.logger.error(f"Error in wake word detection: {e}")
            
            # Process every 30ms for smooth detection
            time.sleep(0.03)
    
    def _put_event_nowait(self, event: str) -> None:
        """Thread-safe event queue put"""
        try:
            self.event_queue.put_nowait(event)
        except asyncio.QueueFull:
            self.logger.warning(f"Event queue full, dropping {event}")


class MinimalLiveKitClient:
    """Minimal LiveKit client - connect/disconnect only"""
    
    def __init__(self, audio_manager: ALSAAudioManager, event_queue: asyncio.Queue):
        self.logger = logger.getChild("LiveKit")
        self.audio_manager = audio_manager
        self.event_queue = event_queue
        
        self.room: Optional[rtc.Room] = None
        self.audio_source: Optional[rtc.AudioSource] = None
        self.connected = False
        self._forward_task: Optional[asyncio.Task] = None
        self._receive_tasks: dict[str, asyncio.Task] = {}
        
    async def connect(self) -> None:
        """Connect to LiveKit - everything else pre-created"""
        if self.connected:
            self.logger.warning("Already connected")
            return
            
        try:
            # Get credentials (simplified for example)
            token = await self._get_credentials()
            
            # Create and connect room
            self.room = rtc.Room()
            self.room.on("track_subscribed", self._on_track_subscribed)
            self.room.on("disconnected", self._on_disconnected)
            
            await self.room.connect(LIVEKIT_URL, token)
            self.logger.info(f"Connected to room: {self.room.name}")
            
            # Create and publish audio track
            self.audio_source = rtc.AudioSource(SAMPLE_RATE, CHANNELS)
            track = rtc.LocalAudioTrack.create_audio_track("microphone", self.audio_source)
            
            options = rtc.TrackPublishOptions(source=rtc.TrackSource.SOURCE_MICROPHONE)
            publication = await self.room.local_participant.publish_track(track, options)
            self.logger.info(f"Published microphone track: {publication.sid}")
            
            # Start forwarding task
            self.connected = True
            self._forward_task = asyncio.create_task(self._forward_mic())
            
        except Exception as e:
            self.logger.error(f"Failed to connect: {e}")
            await self.disconnect()
            raise
    
    async def disconnect(self) -> None:
        """Disconnect from LiveKit"""
        if not self.connected and not self.room:
            return
            
        self.logger.info("Disconnecting from LiveKit...")
        self.connected = False
        
        # Cancel tasks
        if self._forward_task:
            self._forward_task.cancel()
            try:
                await self._forward_task
            except asyncio.CancelledError:
                pass
                
        for task in list(self._receive_tasks.values()):
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
        self._receive_tasks.clear()
        
        # Disconnect room
        if self.room:
            await self.room.disconnect()
            self.room = None
            
        self.audio_source = None
        self.logger.info("Disconnected from LiveKit")
    
    async def _get_credentials(self) -> str:
        """Get LiveKit token from the authentication server."""
        device_id = os.getenv("DEVICE_ID", "test-device")
        username = "recDNsqLQ9CKswdVJ"  # From standalone_client.py for consistency

        url = AUTH_API_URL.format(device_id)
        payload_dict = {"username": username, "x-device-api-key": username}
        payload_bytes = json.dumps(payload_dict).encode('utf-8')
        headers = {'Content-Type': 'application/json'}

        self.logger.info(f"Fetching token from {url} for device '{device_id}'")

        def blocking_request():
            req = urllib.request.Request(url, data=payload_bytes, headers=headers, method='POST')
            try:
                with urllib.request.urlopen(req, timeout=10) as response:
                    if response.status == 200:
                        res_data = json.loads(response.read().decode('utf-8'))
                        if res_data.get("success") and res_data.get("livekit_token"):
                            self.logger.info("Successfully fetched LiveKit token.")
                            return res_data["livekit_token"]
                    self.logger.error(f"Auth server returned status {response.status}: {response.read().decode('utf-8')}")
            except urllib.error.URLError as e:
                self.logger.error(f"Auth request failed: {e.reason}")
            except json.JSONDecodeError:
                self.logger.error("Failed to decode auth server response.")
            except Exception as e:
                self.logger.error(f"Unexpected error fetching token: {e}")
            return None

        loop = asyncio.get_running_loop()
        token = await loop.run_in_executor(None, blocking_request)

        if token:
            return token
        else:
            self.logger.warning("API token fetch failed. Falling back to LIVEKIT_TOKEN env var.")
            fallback_token = os.getenv("LIVEKIT_TOKEN")
            if not fallback_token:
                raise ConnectionError("Failed to get token from API and LIVEKIT_TOKEN env var is not set.")
            return fallback_token
    
    async def _forward_mic(self) -> None:
        """Forward microphone audio to LiveKit"""
        samples_per_channel = PERIOD_SIZE  # 10ms chunks
        
        while self.connected and self.audio_source:
            try:
                # Read from mic buffer
                chunk = self.audio_manager.mic_buffer.read()
                
                if chunk and len(chunk) == CHUNK_SIZE:
                    # Create frame (already correct size)
                    frame = rtc.AudioFrame(
                        sample_rate=SAMPLE_RATE,
                        num_channels=CHANNELS,
                        samples_per_channel=samples_per_channel,
                        data=chunk[:samples_per_channel * CHANNELS * 2]  # Ensure correct size
                    )
                    
                    await self.audio_source.capture_frame(frame)
                else:
                    # No audio available
                    await asyncio.sleep(0.01)
                    
            except Exception as e:
                self.logger.error(f"Error forwarding mic: {e}")
                await asyncio.sleep(0.1)
    
    def _on_track_subscribed(self, track: rtc.Track, publication: rtc.RemoteTrackPublication, 
                           participant: rtc.RemoteParticipant) -> None:
        """Handle incoming tracks"""
        if track.kind == rtc.TrackKind.KIND_AUDIO:
            self.logger.info(f"Subscribed to audio track from {participant.identity}")
            task = asyncio.create_task(self._receive_audio(track))
            self._receive_tasks[track.sid] = task
    
    async def _receive_audio(self, track: rtc.Track) -> None:
        """Receive audio from assistant"""
        audio_stream = rtc.AudioStream(track)
        
        try:
            async for frame_event in audio_stream:
                frame = frame_event.frame
                
                # CRITICAL: This triggers predictive echo cancellation
                self.audio_manager.receive_audio_from_livekit(
                    bytes(frame.data)
                )
                
        except Exception as e:
            self.logger.error(f"Error receiving audio: {e}")
        finally:
            self._receive_tasks.pop(track.sid, None)
    
    def _on_disconnected(self) -> None:
        """Handle unexpected disconnection"""
        self.logger.warning("Unexpectedly disconnected from LiveKit")
        self.connected = False
        try:
            self.event_queue.put_nowait("DISCONNECT")
        except:
            pass


class MinimalLightberryApp:
    """Main application with simplified state machine"""
    
    def __init__(self):
        self.logger = logger
        self.logger.info("Initializing Minimal Lightberry...")
        
        # State
        self.state = State.IDLE
        self.is_muted = False
        self.event_queue = asyncio.Queue(maxsize=10)
        
        # Pre-load ALL components
        self.logger.info("Pre-loading all components...")
        self.audio_manager = ALSAAudioManager()
        self.wake_detector = WakeWordDetector(self.audio_manager, self.event_queue)
        self.livekit_client = MinimalLiveKitClient(self.audio_manager, self.event_queue)
        
        # Pre-load sounds
        self._load_sounds()
        
        # Timeout tracking
        self._timeout_task: Optional[asyncio.Task] = None
        self._timeout_seconds = 300  # 5 minute timeout
        
        self.logger.info("All components loaded - ready for real-time operation")
    
    def _load_sounds(self) -> None:
        """Pre-load all WAV files for instant playback"""
        self.sounds = {}
        
        # Define sound files
        sound_files = {
            'wake': 'bradford-wake.wav',
            'sleep': 'bradford-sleep.wav', 
            'startup': 'soft_gong_short.wav'
        }
        
        for name, filename in sound_files.items():
            if os.path.exists(filename):
                try:
                    with wave.open(filename, 'rb') as w:
                        # Read entire file
                        frames = w.readframes(w.getnframes())
                        self.sounds[name] = frames
                        self.logger.info(f"Loaded sound: {name}")
                except Exception as e:
                    self.logger.warning(f"Failed to load {name} sound: {e}")
                    self.sounds[name] = b''  # Empty sound
            else:
                self.logger.warning(f"Sound file not found: {filename}")
                self.sounds[name] = b''  # Empty sound
    
    async def run(self) -> None:
        """Main state machine loop"""
        try:
            # Start audio system
            self.audio_manager.start()
            
            # Start wake word detection
            self.wake_detector.start()
            
            # Play startup sound
            if self.sounds.get('startup'):
                await self.audio_manager.play_sound(self.sounds['startup'])
            
            self.logger.info("System ready - listening for wake word")
            
            # Main loop
            while True:
                if self.state == State.IDLE:
                    await self._handle_idle()
                elif self.state == State.ACTIVE:
                    await self._handle_active()
                
                await asyncio.sleep(0.01)  # 10ms loop
                
        except asyncio.CancelledError:
            self.logger.info("Main loop cancelled")
        except Exception as e:
            self.logger.error(f"Fatal error in main loop: {e}", exc_info=True)
        finally:
            await self.cleanup()
    
    async def _handle_idle(self) -> None:
        """IDLE state - waiting for wake word"""
        try:
            event = await asyncio.wait_for(self.event_queue.get(), timeout=0.1)
            
            if event == "WAKE_DETECTED":
                self.logger.info("Wake word detected - activating")
                
                # Cancel any existing timeout
                if self._timeout_task:
                    self._timeout_task.cancel()
                
                # Instant feedback
                if self.sounds.get('wake'):
                    asyncio.create_task(self.audio_manager.play_sound(self.sounds['wake']))
                
                # Connect to LiveKit
                try:
                    await self.livekit_client.connect()
                    self.state = State.ACTIVE
                    
                    # Start activity timeout
                    self._timeout_task = asyncio.create_task(self._activity_timeout())
                    
                except Exception as e:
                    self.logger.error(f"Failed to connect: {e}")
                    # Stay in IDLE
                
        except asyncio.TimeoutError:
            pass  # Normal - no events
    
    async def _handle_active(self) -> None:
        """ACTIVE state - connected to assistant"""
        try:
            event = await asyncio.wait_for(self.event_queue.get(), timeout=0.1)
            
            if event == "TOGGLE_MUTE":
                self.is_muted = not self.is_muted
                self.audio_manager.set_capture_muted(self.is_muted)
                self.logger.info(f"Microphone {'muted' if self.is_muted else 'unmuted'}")
                
            elif event in ["STOP_WORD_DETECTED", "DISCONNECT", "TIMEOUT"]:
                self.logger.info(f"Deactivating due to: {event}")
                
                # Cancel timeout
                if self._timeout_task:
                    self._timeout_task.cancel()
                    self._timeout_task = None
                
                # Instant feedback
                if self.sounds.get('sleep'):
                    asyncio.create_task(self.audio_manager.play_sound(self.sounds['sleep']))
                
                # Disconnect
                await self.livekit_client.disconnect()
                
                # Clear audio buffers
                self.audio_manager.mic_buffer.clear()
                self.audio_manager.speaker_buffer.clear()
                
                self.state = State.IDLE
                
        except asyncio.TimeoutError:
            pass  # Normal operation
    
    async def _activity_timeout(self) -> None:
        """Timeout if no activity for N seconds"""
        try:
            await asyncio.sleep(self._timeout_seconds)
            self.logger.info("Activity timeout - disconnecting")
            await self.event_queue.put("TIMEOUT")
        except asyncio.CancelledError:
            pass  # Normal - cancelled when there's activity
    
    async def cleanup(self) -> None:
        """Clean up resources"""
        self.logger.info("Cleaning up...")
        
        # Stop components in order
        if self._timeout_task:
            self._timeout_task.cancel()
            
        await self.livekit_client.disconnect()
        self.wake_detector.stop()
        self.audio_manager.stop()
        
        self.logger.info("Cleanup complete")


async def main():
    """Entry point"""
    # Create and run the app
    app = MinimalLightberryApp()
    
    try:
        await app.run()
    except KeyboardInterrupt:
        logger.info("Received interrupt signal")
    except Exception as e:
        logger.error(f"Application error: {e}", exc_info=True)
    finally:
        await app.cleanup()


if __name__ == "__main__":
    # Run the application
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("Application terminated") 
