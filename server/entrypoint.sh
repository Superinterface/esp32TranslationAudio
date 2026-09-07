#!/bin/sh
set -e

# Auto-download models if missing (e.g. when an empty volume is mounted to /app/models)
if [ ! -f "/app/models/silero_vad.onnx" ] || [ ! -f "/app/models/sensevoice/model.int8.onnx" ]; then
    echo "[Docker] ⬇️ AI models not found in /app/models, downloading SenseVoice & Silero VAD..."
    python download_models.py
fi

echo "[Docker] 🚀 Starting ESP32 Audio Transcription Server..."
exec "$@"
