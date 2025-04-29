#!/usr/bin/env python3
import argparse
import sys
import signal

import numpy as np
import pyaudio
from openwakeword.model import Model

def parse_args():
    parser = argparse.ArgumentParser(
        description="Real-time wake-word detection from your default microphone"
    )
    parser.add_argument(
        "--inference-framework",
        choices=["tflite", "onnx"],
        default="tflite",
        help="Which backend to use for model inference",
    )
    parser.add_argument(
        "--model-path",
        default="",
        help="Path to your custom wake-word model file (if omitted, loads bundled defaults)",
    )
    parser.add_argument(
        "--chunk-size",
        type=int,
        default=1280,
        help="Number of audio frames per buffer read (default: 1280)",
    )
    parser.add_argument(
        "--rate",
        type=int,
        default=16000,
        help="Sample rate in Hz (default: 16000)",
    )
    parser.add_argument(
        "--channels",
        type=int,
        default=1,
        help="Number of audio channels (default: 1)",
    )
    return parser.parse_args()

def main():
    args = parse_args()

    # ——— PyAudio setup ———
    p = pyaudio.PyAudio()
    try:
        stream = p.open(
            format=pyaudio.paInt16,
            channels=args.channels,
            rate=args.rate,
            input=True,
            frames_per_buffer=args.chunk_size,
        )
    except Exception as e:
        print(f"✖ Failed to open audio stream: {e}")
        sys.exit(1)

    # ——— Model loading ———
    if args.model_path:
        oww_model = Model(
            wakeword_models=[args.model_path],
            inference_framework=args.inference_framework,
        )
    else:
        # Load default models plus 'lightberry' and 'lightberry_restart'
        print("Identifying default models...")
        temp_model = Model(inference_framework=args.inference_framework)
        # Assuming model keys are the names/paths needed for loading
        default_models = list(temp_model.models.keys()) 
        print(f"Default models identified: {default_models}")
        
        # Define additional models to load by constructing their paths
        framework_extension = args.inference_framework # 'tflite' or 'onnx'
        custom_models = [
            f"wakeword_models/lightberry.{framework_extension}", 
            f"wakeword_models/lightberry_restart.{framework_extension}"
        ] 
        
        # Combine lists (ensure no duplicates if defaults somehow include customs)
        all_models_to_load = list(set(default_models + custom_models)) 
        print(f"Attempting to load models: {all_models_to_load}")

        # Load the final model instance with all desired models
        oww_model = Model(
            wakeword_models=all_models_to_load,
            inference_framework=args.inference_framework
        )
        # temp_model will be garbage collected

    n_models = len(oww_model.models)

    # Graceful shutdown on Ctrl+C
    def cleanup(signum, frame):
        print("\nExiting…")
        stream.stop_stream()
        stream.close()
        p.terminate()
        sys.exit(0)
    signal.signal(signal.SIGINT, cleanup)

    # Initial banner
    print("\n\n" + "#" * 100)
    print("Listening for wake-words... (Ctrl+C to quit)")
    print("#" * 100 + "\n" * (n_models * 3))

    # ——— Detection loop ———
    while True:
        try:
            raw = stream.read(args.chunk_size, exception_on_overflow=False)
            audio = np.frombuffer(raw, dtype=np.int16)
        except OSError:
            # overflow or stream‐closed – just skip this frame
            continue

        oww_model.predict(audio)

        # Build and print the table
        n_spaces = 16
        header = """
            Model Name         | Score | Wakeword Status
            --------------------------------------"""
        for mdl, buf in oww_model.prediction_buffer.items():
            scores = list(buf)
            curr = format(scores[-1], ".20f").replace("-", "")
            status = "Wakeword Detected!" if scores[-1] > 0.5 else "--"
            header += (
                f"\n{mdl}{' '*(n_spaces - len(mdl))} | {curr[:5]} | {status}"
            )

        # Move cursor up and overwrite
        print("\033[F" * (4 * n_models + 1), end="")
        print(header, end="\r")

if __name__ == "__main__":
    main()
