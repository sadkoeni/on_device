import pyaudio # Keep for pyaudio types

# Define constants used for PyAudio configuration
PYAUDIO_CHUNK_SIZE = 1024
PYAUDIO_FORMAT = pyaudio.paInt16 # Corresponds to 16-bit PCM
PYAUDIO_CHANNELS = 1 # Mono audio

# --- LiveKit Audio Constants --- 
# Standard sample rate used throughout the pipeline
LIVEKIT_SAMPLE_RATE = 48000
LIVEKIT_CHANNELS = 1
# Common frame duration for WebRTC/LiveKit (always 10ms)
LIVEKIT_FRAME_DURATION_MS = 10
# Calculated samples per frame based on sample rate and duration
LIVEKIT_SAMPLES_PER_FRAME = int(LIVEKIT_SAMPLE_RATE * LIVEKIT_FRAME_DURATION_MS / 1000)
# Bytes per sample assuming 16-bit PCM audio (INT16)
LIVEKIT_BYTES_PER_SAMPLE = 2
# Total bytes per audio frame
LIVEKIT_BYTES_PER_FRAME = LIVEKIT_SAMPLES_PER_FRAME * LIVEKIT_CHANNELS * LIVEKIT_BYTES_PER_SAMPLE

# Deprecated? Keep for now but aim to use LIVEKIT_SAMPLE_RATE everywhere
INPUT_SAMPLE_RATE = LIVEKIT_SAMPLE_RATE # Changed from 24000 to 48000 for consistency

# Often used for speech recognition 
