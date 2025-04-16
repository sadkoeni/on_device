# streaming_client/interfaces.py
import asyncio
from abc import ABC, abstractmethod
from typing import Optional, Any

class AudioInputInterface(ABC):
    """Abstract base class for audio input providers.

    Should provide a way to get the final transcript of a user's turn.
    """
    @abstractmethod
    async def start(self): # Optional lifecycle method
        """Start the audio input source (e.g., open mic, connect STT)."""
        pass

    @abstractmethod
    async def stop(self): # Optional lifecycle method
        """Stop the audio input source."""
        pass

class AudioOutputInterface(ABC):
    """Abstract base class for audio output handlers.

    Should provide a way to send audio chunks for playback/transmission.
    """
    @abstractmethod
    async def send_audio_chunk(self, chunk: bytes):
        """Asynchronously send a chunk of audio data (PCM 16kHz)."""
        pass

    @abstractmethod
    async def signal_start_of_speech(self): # Optional status signaling
        """Indicate that assistant speech is starting."""
        pass

    @abstractmethod
    async def signal_end_of_speech(self): # Optional status signaling
        """Indicate that assistant speech has ended."""
        pass

    @abstractmethod
    async def start(self): # Optional lifecycle method
        """Start the audio output sink (e.g., open speaker stream)."""
        pass

    @abstractmethod
    async def stop(self): # Optional lifecycle method
        """Stop the audio output sink."""
        pass

    @property
    @abstractmethod
    def is_speaking(self) -> bool:
        """Return True if the assistant is currently speaking/playing audio, False otherwise."""
        pass

    # Add missing method for event (optional but used by our implementation)
    def get_playback_finished_event(self) -> Optional[asyncio.Event]:
        """Return an event that is set when playback finishes (Optional)."""
        return None # Default implementation returns None


# --- New Interfaces --- #

class IOHandlerInterface(ABC):
    """Abstract base class for managing I/O component interactions and state."""
    @abstractmethod
    def get_input_queue(self) -> asyncio.Queue:
        """Return the raw input queue from the underlying input component."""
        pass

    @abstractmethod
    async def send_output_chunk(self, chunk: bytes):
        """Send an audio chunk to the underlying output component."""
        pass

    @abstractmethod
    async def signal_start_of_speech(self):
        """Signal start of speech and perform related actions (e.g., buffer clearing)."""
        pass

    @abstractmethod
    async def signal_end_of_speech(self):
        """Signal end of speech, wait for completion, and perform cleanup."""
        pass

    @abstractmethod
    def is_output_active(self) -> bool:
        """Check if the output component is currently active."""
        pass

    @abstractmethod
    async def buffer_chunk_for_saving(self, chunk: Any): # Chunk type might be Any
        """Buffer a chunk of raw input data for potential saving."""
        pass

    @abstractmethod
    async def save_buffered_audio(self, identifier: str):
        """Save the currently buffered input data, associating it with an identifier."""
        pass

    @abstractmethod
    async def discard_buffered_audio(self):
        """Discard the currently buffered input data without saving."""
        pass

    @abstractmethod
    async def start(self): # Might not be needed if AudioInput/Output handle start
        """Start the IO Handler (if it has independent tasks)."""
        pass

    @abstractmethod
    async def stop(self): # Might not be needed
        """Stop the IO Handler."""
        pass


class InputProcessorInterface(ABC):
    """Abstract base class for processing raw input into processable units."""
    @abstractmethod
    async def get_next_processable_unit(self) -> Optional[Any]:
        """Consume raw input, process it (e.g., STT), detect unit completion, and return the unit."""
        pass

    @abstractmethod
    async def start(self):
        """Start the input processor's internal components and tasks."""
        pass

    @abstractmethod
    async def stop(self):
        """Stop the input processor's internal components and tasks."""
        pass


class ProcessorInterface(ABC):
    """Abstract base class for the main processing logic (e.g., LLM + TTS)."""
    @abstractmethod
    async def process(self, input_unit: Any) -> Optional[Any]: # Return value might be assistant response text or other status
        """Process a complete input unit and generate output actions via IOHandler."""
        pass

    @abstractmethod
    async def start(self): # Optional: For actions like pre-connecting
        """Initialize or pre-connect resources needed by the processor."""
        pass

    @abstractmethod
    async def stop(self): # Optional: For cleanup
        """Stop or clean up resources used by the processor."""
        pass 