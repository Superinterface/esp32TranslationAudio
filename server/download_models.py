#!/usr/bin/env python3
"""
Model Downloader for ESP32-S3 Audio Whisper
Downloads SenseVoice-Small ONNX and Silero-VAD models if not present.
"""
import os
import sys
import tarfile
import urllib.request

MODELS_DIR = os.path.join(os.path.dirname(__file__), "models")
SENSEVOICE_DIR = os.path.join(MODELS_DIR, "sensevoice")
VAD_MODEL_PATH = os.path.join(MODELS_DIR, "silero_vad.onnx")

SENSEVOICE_URL = "https://github.com/k2-fsa/sherpa-onnx/releases/download/asr-models/sherpa-onnx-sense-voice-zh-en-ja-ko-yue-2024-07-17.tar.bz2"
SILERO_VAD_URL = "https://github.com/snakers4/silero-vad/raw/master/src/silero_vad/data/silero_vad.onnx"

def download_progress(count, block_size, total_size):
    percent = int(count * block_size * 100 / total_size) if total_size > 0 else 0
    sys.stdout.write(f"\rDownloading... {percent}%")
    sys.stdout.flush()

def main():
    os.makedirs(MODELS_DIR, exist_ok=True)
    os.makedirs(SENSEVOICE_DIR, exist_ok=True)

    # 1. Check Silero VAD
    if not os.path.exists(VAD_MODEL_PATH):
        print("[1/2] ⬇️ Downloading Silero VAD model...")
        urllib.request.urlretrieve(SILERO_VAD_URL, VAD_MODEL_PATH, reporthook=download_progress)
        print("\n✅ Silero VAD model downloaded!")
    else:
        print("[1/2] ✅ Silero VAD model already exists.")

    # 2. Check SenseVoice-Small
    target_onnx = os.path.join(SENSEVOICE_DIR, "model.int8.onnx")
    target_tokens = os.path.join(SENSEVOICE_DIR, "tokens.txt")
    if not (os.path.exists(target_onnx) and os.path.exists(target_tokens)):
        print("[2/2] ⬇️ Downloading SenseVoice-Small ONNX archive (~230MB)...")
        tar_path = os.path.join(MODELS_DIR, "sensevoice.tar.bz2")
        urllib.request.urlretrieve(SENSEVOICE_URL, tar_path, reporthook=download_progress)
        print("\n📦 Extracting SenseVoice-Small...")
        with tarfile.open(tar_path, "r:bz2") as tar:
            for member in tar.getmembers():
                if "model.int8.onnx" in member.name:
                    member.name = "model.int8.onnx"
                    tar.extract(member, SENSEVOICE_DIR)
                elif "tokens.txt" in member.name:
                    member.name = "tokens.txt"
                    tar.extract(member, SENSEVOICE_DIR)
        if os.path.exists(tar_path):
            os.remove(tar_path)
        print("✅ SenseVoice-Small model extracted successfully!")
    else:
        print("[2/2] ✅ SenseVoice-Small model already exists.")

    print("\n🎉 All AI models are ready!")

if __name__ == "__main__":
    main()
