import os
import time
import wave
import queue
import threading
import asyncio
import json
import urllib.request
import urllib.parse
import re
from datetime import datetime
from typing import Set, Optional

import numpy as np
from scipy import signal
from fastapi import FastAPI, WebSocket, WebSocketDisconnect, Request
from fastapi.responses import HTMLResponse, FileResponse, JSONResponse

# Configuration Loader from config.env or .env
def load_app_config():
    cfg = {
        "SERVER_HOST": "0.0.0.0",
        "SERVER_PORT": 8000,
        "RECORDINGS_DIR": os.path.join(os.path.dirname(__file__), "recordings"),
        "DEFAULT_LANGUAGE": "auto",
        "TRANSLATE_TARGET": "zh",
        "VAD_THRESHOLD": 0.5,
        "SILENCE_THRESHOLD_SECONDS": 0.65,
    }
    candidates = [
        os.path.join(os.path.dirname(__file__), "..", "config.env"),
        os.path.join(os.path.dirname(__file__), "config.env"),
        os.path.join(os.path.dirname(__file__), "..", ".env"),
        os.path.join(os.path.dirname(__file__), ".env"),
    ]
    for c in candidates:
        if os.path.isfile(c):
            print(f"[Config] ⚙️  Loaded configuration from: {os.path.abspath(c)}")
            try:
                with open(c, "r", encoding="utf-8") as f:
                    for line in f:
                        line = line.strip()
                        if line and not line.startswith("#") and "=" in line:
                            k, v = line.split("=", 1)
                            k, v = k.strip(), v.strip().strip('"').strip("'")
                            if k == "SERVER_PORT":
                                cfg[k] = int(v)
                            elif k in ("VAD_THRESHOLD", "SILENCE_THRESHOLD_SECONDS"):
                                cfg[k] = float(v)
                            elif k in cfg:
                                cfg[k] = v
            except Exception as e:
                print(f"[Config] Warning: error reading {c}: {e}")
            break
    return cfg

APP_CONFIG = load_app_config()

RECORDINGS_DIR = APP_CONFIG["RECORDINGS_DIR"]
STATIC_DIR = os.path.join(os.path.dirname(__file__), "static")
os.makedirs(RECORDINGS_DIR, exist_ok=True)
os.makedirs(STATIC_DIR, exist_ok=True)

SAMPLE_RATE = 16000
SAMPLE_WIDTH = 2  # 16-bit
CHANNELS = 1      # Mono
BYTES_PER_SEC = SAMPLE_RATE * SAMPLE_WIDTH * CHANNELS

# 2nd-order Butterworth High-Pass Filter at 80 Hz (Sample rate 16000)
# Completely removes low-frequency power supply ripple, DC drift, and WiFi periodic noise
hp_b, hp_a = signal.butter(2, 80, btype='high', fs=SAMPLE_RATE)

class StreamFilter:
    def __init__(self):
        self.zi = signal.lfilter_zi(hp_b, hp_a) * 0.0

    def reset(self):
        self.zi = signal.lfilter_zi(hp_b, hp_a) * 0.0

    def process(self, pcm_data: bytes) -> bytes:
        samples = np.frombuffer(pcm_data, dtype=np.int16).astype(np.float32)
        filtered, self.zi = signal.lfilter(hp_b, hp_a, samples, zi=self.zi)
        filtered = np.clip(filtered, -32768, 32767).astype(np.int16)
        return filtered.tobytes()

audio_filter = StreamFilter()

app = FastAPI(title="ESP32-S3 Audio Streamer & Live Whisper")

# Global State
active_browser_clients: Set[WebSocket] = set()
esp32_connected = False
esp32_stats = {
    "connected": False,
    "total_bytes": 0,
    "total_seconds": 0.0,
    "last_packet_time": 0,
}

transcription_queue = queue.Queue()
transcripts_history = []
asr_engine = None
asr_engine_name = "SenseVoice-Small"
main_loop: Optional[asyncio.AbstractEventLoop] = None


# ---------------------------------------------------------
# Track 1: Continuous Long-Recording Manager (长音频连续归档)
# ---------------------------------------------------------
class ContinuousWavRecorder:
    def __init__(self, output_dir: str):
        self.output_dir = output_dir
        self.current_filename: Optional[str] = None
        self.current_filepath: Optional[str] = None
        self.current_wav: Optional[wave.Wave_write] = None
        self.current_hour_str = ""
        self.bytes_written = 0
        self.lock = threading.Lock()

    def get_hour_tag(self) -> str:
        return datetime.now().strftime("%Y%m%d_%H00")

    def write(self, pcm_data: bytes):
        with self.lock:
            try:
                hour_tag = self.get_hour_tag()
                if self.current_wav is None or hour_tag != self.current_hour_str:
                    self._rotate_file(hour_tag)
                if self.current_wav:
                    self.current_wav.writeframes(pcm_data)
                    self.bytes_written += len(pcm_data)
            except Exception as e:
                print(f"[Recorder] Error writing audio chunk: {e}")

    def _rotate_file(self, hour_tag: str):
        if self.current_wav is not None:
            try:
                self.current_wav.close()
            except Exception as e:
                print(f"[Recorder] Error closing previous wav: {e}")

        self.current_hour_str = hour_tag
        base_name = f"session_{hour_tag}00.wav"
        filepath = os.path.join(self.output_dir, base_name)

        # If already exists (e.g. server restart), append timestamp so we don't overwrite
        if os.path.exists(filepath):
            ts = datetime.now().strftime("%Y%m%d_%H%M%S")
            base_name = f"session_{ts}.wav"
            filepath = os.path.join(self.output_dir, base_name)

        self.current_filename = base_name
        self.current_filepath = filepath
        self.current_wav = wave.open(filepath, "wb")
        self.current_wav.setnchannels(CHANNELS)
        self.current_wav.setsampwidth(SAMPLE_WIDTH)
        self.current_wav.setframerate(SAMPLE_RATE)
        self.bytes_written = 0
        print(f"[Recorder] 📁 Opened continuous recording file: {base_name}")

    def append_transcript_to_session(self, timestamp_str: str, text: str):
        with self.lock:
            if not self.current_filepath:
                return
            txt_path = os.path.splitext(self.current_filepath)[0] + ".txt"
            try:
                with open(txt_path, "a", encoding="utf-8") as f:
                    f.write(f"[{timestamp_str}] {text}\n")
            except Exception as e:
                print(f"[Recorder] Error writing session transcript: {e}")

    def close(self):
        with self.lock:
            if self.current_wav:
                try:
                    self.current_wav.close()
                    print(f"[Recorder] Closed continuous recording: {self.current_filename}")
                except Exception:
                    pass
                self.current_wav = None

recorder = ContinuousWavRecorder(RECORDINGS_DIR)

# ---------------------------------------------------------
# Emotion, Event, and Language Mappings (SenseVoice)
# ---------------------------------------------------------
EMOTION_MAP = {
    "<|HAPPY|>": "😄 开心",
    "<|SAD|>": "😢 悲伤",
    "<|ANGRY|>": "😠 愤怒",
    "<|NEUTRAL|>": "😐 平静",
    "<|FEARFUL|>": "😨 恐惧",
    "<|DISGUSTED|>": "🤢 厌恶",
    "<|SURPRISED|>": "😲 惊讶",
}

EVENT_MAP = {
    "<|Laughter|>": "😂 笑声",
    "<|Applause|>": "👏 掌声",
    "<|Cough|>": "😷 咳嗽",
    "<|Cry|>": "😭 哭声",
    "<|Sneeze|>": "🤧 喷嚏",
    "<|BGM|>": "🎵 音乐",
    "<|Sing|>": "🎤 唱歌",
    "<|Speech|>": "🗣️ 说话",
}

LANG_MAP = {
    "<|zh|>": "🇨🇳 中文",
    "<|en|>": "🇬🇧 英语",
    "<|ja|>": "🇯🇵 日语",
    "<|ko|>": "🇰🇷 韩语",
    "<|yue|>": "🇭🇰 粤语",
}

def needs_translation_check(text: str, lang_str: str) -> bool:
    if not text.strip():
        return False
    han_count = sum(1 for c in text if '\u4e00' <= c <= '\u9fff')
    if "中文" in lang_str:
        # If classified as Chinese and has any Han characters (e.g. Chinese mixed with English loanwords), do not translate
        if han_count > 0:
            return False
        return True
    if "粤语" in lang_str:
        return True
    total_alpha = sum(1 for c in text if c.isalnum())
    if total_alpha > 0 and (han_count / total_alpha) > 0.6:
        return False
    return True

def translate_to_chinese(text: str) -> str:
    """Translates foreign language text into Simplified Chinese via fast HTTP API."""
    if not text.strip():
        return ""
    try:
        url = "https://translate.googleapis.com/translate_a/single?client=gtx&sl=auto&tl=zh-CN&dt=t&q=" + urllib.parse.quote(text.strip())
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=2.5) as res:
            data = json.loads(res.read().decode("utf-8"))
            if data and data[0]:
                trans = "".join([part[0] for part in data[0] if part and part[0]]).strip()
                return trans
    except Exception as e:
        print(f"[Translate] Fast translation error: {e}")
    return ""

# ---------------------------------------------------------
# Track 2: Silero VAD AI-Powered Speech Segmenter (AI 人声断句)
# ---------------------------------------------------------
class SileroVadSegmenter:
    """
    Uses Silero VAD (ONNX) to detect human voice activity.
    Only real human vocal cord sounds trigger segmentation.
    Knocks, coughs, typing, and background fans are filtered out.
    """
    def __init__(self, model_path: str, sample_rate: int = 16000, threshold: float = 0.5,
                 min_silence_duration: float = 0.65, min_speech_duration: float = 0.25):
        self.sample_rate = sample_rate
        self.bytes_per_sec = sample_rate * 2
        self.is_ready = False
        self.vad = None

        if os.path.exists(model_path):
            try:
                import sherpa_onnx
                config = sherpa_onnx.VadModelConfig()
                config.silero_vad.model = model_path
                config.silero_vad.threshold = threshold
                config.silero_vad.min_silence_duration = min_silence_duration
                config.silero_vad.min_speech_duration = min_speech_duration
                config.sample_rate = sample_rate
                self.vad = sherpa_onnx.VoiceActivityDetector(config, buffer_size_in_seconds=60)
                self.is_ready = True
                print(f"[VAD] 🧠 Silero VAD (AI-powered voice activity detection) active!")
            except Exception as e:
                print(f"[VAD] Error initializing Silero VAD: {e}")

        # Fallback RMS state
        self.rms_speaking = False
        self.rms_buffer = bytearray()
        self.rms_silence_sec = 0.0

    def process_chunk(self, chunk: bytes) -> list[bytes]:
        results = []
        if self.is_ready and self.vad:
            samples = np.frombuffer(chunk, dtype=np.int16).astype(np.float32) / 32768.0
            self.vad.accept_waveform(samples)
            while not self.vad.empty():
                seg = self.vad.front
                pcm_out = (np.array(seg.samples) * 32768.0).astype(np.int16).tobytes()
                self.vad.pop()
                if len(pcm_out) >= int(0.4 * self.bytes_per_sec):
                    results.append(pcm_out)
            return results
        else:
            # Fallback RMS
            data = np.frombuffer(chunk, dtype=np.int16)
            rms = np.sqrt(np.mean(data.astype(np.float64)**2)) if len(data) > 0 else 0
            chunk_duration = len(chunk) / self.bytes_per_sec
            if rms > 120:
                if not self.rms_speaking:
                    self.rms_speaking = True
                self.rms_buffer.extend(chunk)
                self.rms_silence_sec = 0.0
            else:
                if self.rms_speaking:
                    self.rms_buffer.extend(chunk)
                    self.rms_silence_sec += chunk_duration
                    if self.rms_silence_sec >= 0.7 or len(self.rms_buffer) >= 12 * self.bytes_per_sec:
                        utt = bytes(self.rms_buffer)
                        self.rms_buffer.clear()
                        self.rms_speaking = False
                        self.rms_silence_sec = 0.0
                        if len(utt) >= int(0.5 * self.bytes_per_sec):
                            results.append(utt)
            return results

    def reset(self):
        if self.is_ready and self.vad:
            self.vad.reset()
        self.rms_speaking = False
        self.rms_buffer.clear()
        self.rms_silence_sec = 0.0

VAD_MODEL_PATH = os.path.join(os.path.dirname(__file__), "models", "silero_vad.onnx")
segmenter = SileroVadSegmenter(
    VAD_MODEL_PATH,
    threshold=APP_CONFIG["VAD_THRESHOLD"],
    min_silence_duration=APP_CONFIG["SILENCE_THRESHOLD_SECONDS"],
)


# ---------------------------------------------------------
# Multilingual ASR Engine (SenseVoice-Small / Whisper)
# ---------------------------------------------------------
def init_asr():
    global asr_engine, asr_engine_name
    model_dir = os.path.join(os.path.dirname(__file__), "models", "sensevoice")
    model_file = os.path.join(model_dir, "model.int8.onnx")
    tokens_file = os.path.join(model_dir, "tokens.txt")

    if os.path.exists(model_file) and os.path.exists(tokens_file):
        try:
            import sherpa_onnx
            print(f"[ASR] 🚀 Loading Alibaba SenseVoice-Small (Intel CPU optimized, Multilingual zh/en/ja/ko/yue)...")
            recognizer = sherpa_onnx.OfflineRecognizer.from_sense_voice(
                model=model_file,
                tokens=tokens_file,
                num_threads=4,
                use_itn=True,
            )
            asr_engine = recognizer
            asr_engine_name = "SenseVoice-Small (ONNX Int8)"
            print(f"[ASR] ✅ SenseVoice-Small loaded successfully! Ready for multilingual speech recognition.")
            return
        except Exception as e:
            print(f"[ASR] SenseVoice load error: {e}, trying Faster-Whisper fallback...")

    # Fallback to Faster-Whisper
    try:
        from faster_whisper import WhisperModel
        print("[ASR] Loading faster-whisper model ('base')...")
        asr_engine = WhisperModel("base", device="auto", compute_type="int8")
        asr_engine_name = "Faster-Whisper (base)"
        print("[ASR] Whisper Model loaded successfully!")
    except Exception as e:
        print(f"[ASR] Warning: Could not initialize ASR model: {e}")

def asr_worker():
    while True:
        item = transcription_queue.get()
        if item is None:
            break
        raw_pcm, timestamp_str = item
        try:
            if asr_engine is not None and len(raw_pcm) > 0:
                audio_np = np.frombuffer(raw_pcm, dtype=np.int16).astype(np.float32) / 32768.0
                start_t = time.time()
                text = ""
                lang_str = ""
                emo_str = ""
                event_str = ""

                if "SenseVoice" in asr_engine_name:
                    stream = asr_engine.create_stream()
                    stream.accept_waveform(16000, audio_np)
                    asr_engine.decode_stream(stream)
                    text = stream.result.text.strip()
                    lang_raw = getattr(stream.result, "lang", "")
                    emo_raw = getattr(stream.result, "emotion", "")
                    event_raw = getattr(stream.result, "event", "")

                    lang_str = LANG_MAP.get(lang_raw, lang_raw.replace("<|", "").replace("|>", "").upper())
                    emo_str = EMOTION_MAP.get(emo_raw, "")
                    event_str = EVENT_MAP.get(event_raw, "")
                else:
                    segments, info = asr_engine.transcribe(audio_np, beam_size=3, vad_filter=True)
                    text = "".join([segment.text for segment in segments]).strip()
                    lang_str = info.language.upper() if info else "ZH"

                cost = time.time() - start_t

                if text:
                    # 自动检测是否需要翻译成中文（非中文语种或包含外语）
                    translated_text = ""
                    needs_translation = needs_translation_check(text, lang_str)
                    if needs_translation:
                        try:
                            translated_text = translate_to_chinese(text)
                            if translated_text.strip() == text.strip():
                                translated_text = ""
                        except Exception:
                            translated_text = ""

                    tags_log = f"[{lang_str}]" if lang_str else ""
                    if emo_str and "平静" not in emo_str:
                        tags_log += f"[{emo_str}]"
                    if event_str and "说话" not in event_str:
                        tags_log += f"[{event_str}]"

                    print(f"[{asr_engine_name}] 🗣️ [{timestamp_str}] {tags_log} ({cost:.2f}s): {text}")
                    if translated_text:
                        print(f"       ↳ 🇨🇳 [中文翻译]: {translated_text}")

                    # Append to current session log file (includes bilingual translation if available)
                    log_entry = f"{tags_log} {text}" if tags_log else text
                    if translated_text:
                        log_entry += f"  ➔ 🇨🇳 [译] {translated_text}"
                    recorder.append_transcript_to_session(timestamp_str, log_entry)

                    data = {
                        "timestamp": timestamp_str,
                        "text": text,
                        "translation": translated_text,
                        "duration": f"{round(len(raw_pcm)/BYTES_PER_SEC, 1)}s",
                        "lang": lang_str,
                        "emotion": emo_str,
                        "event": event_str,
                    }
                    transcripts_history.append(data)
                    if len(transcripts_history) > 100:
                        transcripts_history.pop(0)

                    # Broadcast live subtitle to all open browsers
                    if main_loop and main_loop.is_running():
                        asyncio.run_coroutine_threadsafe(
                            broadcast_json({"type": "live_transcript", "data": data}),
                            main_loop
                        )
        except Exception as e:
            print(f"[ASR] Transcription error: {e}")
        finally:
            transcription_queue.task_done()

@app.on_event("startup")
async def startup_event():
    global main_loop
    main_loop = asyncio.get_running_loop()

threading.Thread(target=asr_worker, daemon=True).start()
threading.Thread(target=init_asr, daemon=True).start()

async def broadcast_json(message: dict):
    disconnected = set()
    for client in list(active_browser_clients):
        try:
            await client.send_json(message)
        except Exception:
            disconnected.add(client)
    active_browser_clients.difference_update(disconnected)

# ---------------------------------------------------------
# REST APIs
# ---------------------------------------------------------
@app.get("/")
async def get_index():
    index_file = os.path.join(STATIC_DIR, "index.html")
    if os.path.exists(index_file):
        return FileResponse(index_file)
    return HTMLResponse("<h1>ESP32-S3 Audio Streamer</h1>")

@app.get("/api/status")
async def get_status():
    return JSONResponse({
        "esp32_connected": esp32_connected,
        "current_session": recorder.current_filename or "无正在录制的会话",
        "total_bytes": esp32_stats["total_bytes"],
        "total_seconds": round(esp32_stats["total_bytes"] / BYTES_PER_SEC, 1),
        "whisper_ready": asr_engine is not None,
        "asr_engine": asr_engine_name,
    })

@app.get("/api/recordings")
async def get_recordings():
    files = []
    total_bytes = 0
    if os.path.exists(RECORDINGS_DIR):
        for f in sorted(os.listdir(RECORDINGS_DIR), reverse=True):
            if f.endswith(".wav"):
                wav_path = os.path.join(RECORDINGS_DIR, f)
                txt_path = os.path.splitext(wav_path)[0] + ".txt"
                transcript = ""
                if os.path.exists(txt_path):
                    try:
                        with open(txt_path, "r", encoding="utf-8") as tf:
                            transcript = tf.read().strip()
                    except Exception:
                        pass

                try:
                    stat = os.stat(wav_path)
                    file_bytes = stat.st_size
                    created_at = datetime.fromtimestamp(stat.st_mtime).strftime("%Y-%m-%d %H:%M:%S")
                except Exception:
                    file_bytes = 0
                    created_at = "-"

                total_bytes += file_bytes
                duration_sec = round(file_bytes / BYTES_PER_SEC, 1)
                mins = int(duration_sec // 60)
                secs = int(duration_sec % 60)
                dur_str = f"{mins}分{secs}秒" if mins > 0 else f"{secs}秒"

                is_active = (f == recorder.current_filename and esp32_connected)

                files.append({
                    "filename": f,
                    "is_active": is_active,
                    "size_mb": round(file_bytes / (1024 * 1024), 2),
                    "duration": dur_str,
                    "created_at": created_at,
                    "transcript_summary": (transcript[:90] + "...") if len(transcript) > 90 else transcript,
                    "has_transcript": bool(transcript),
                })
    return JSONResponse({
        "files": files,
        "total_count": len(files),
        "total_size_mb": round(total_bytes / (1024 * 1024), 2),
    })

@app.get("/recordings/{filename}")
async def download_recording(filename: str):
    safe_name = os.path.basename(filename)
    file_path = os.path.join(RECORDINGS_DIR, safe_name)
    if os.path.exists(file_path):
        return FileResponse(file_path, media_type="audio/wav", filename=safe_name)
    return JSONResponse({"error": "File not found"}, status_code=404)

@app.get("/recordings/{filename}/download_txt")
async def download_transcript_txt(filename: str):
    safe_name = os.path.basename(filename)
    txt_filename = os.path.splitext(safe_name)[0] + ".txt"
    txt_path = os.path.join(RECORDINGS_DIR, txt_filename)
    if os.path.exists(txt_path):
        return FileResponse(txt_path, media_type="text/plain; charset=utf-8", filename=txt_filename)
    return JSONResponse({"error": "Transcript not found"}, status_code=404)

@app.get("/recordings/{filename}/transcript")
async def get_transcript_text(filename: str):
    safe_name = os.path.basename(filename)
    txt_filename = os.path.splitext(safe_name)[0] + ".txt"
    txt_path = os.path.join(RECORDINGS_DIR, txt_filename)
    if os.path.exists(txt_path):
        with open(txt_path, "r", encoding="utf-8") as f:
            return HTMLResponse(f"<pre style='font-family: monospace; white-space: pre-wrap;'>{f.read()}</pre>")
    return JSONResponse({"error": "Transcript not found"}, status_code=404)

@app.get("/api/recordings/{filename}/transcript")
async def get_transcript_json(filename: str):
    safe_name = os.path.basename(filename)
    txt_filename = os.path.splitext(safe_name)[0] + ".txt"
    txt_path = os.path.join(RECORDINGS_DIR, txt_filename)
    if os.path.exists(txt_path):
        with open(txt_path, "r", encoding="utf-8") as f:
            content = f.read()
        return JSONResponse({"filename": safe_name, "transcript": content})
    return JSONResponse({"error": "Transcript not found", "transcript": ""}, status_code=404)

@app.delete("/api/recordings/{filename}")
async def delete_recording(filename: str):
    safe_name = os.path.basename(filename)
    if safe_name == recorder.current_filename and esp32_connected:
        return JSONResponse({"error": "该录音正在录制中，无法删除！"}, status_code=400)

    wav_path = os.path.join(RECORDINGS_DIR, safe_name)
    txt_path = os.path.join(RECORDINGS_DIR, os.path.splitext(safe_name)[0] + ".txt")

    deleted = False
    if os.path.exists(wav_path):
        try:
            os.remove(wav_path)
            deleted = True
        except Exception as e:
            return JSONResponse({"error": f"删除音频失败: {e}"}, status_code=500)
    if os.path.exists(txt_path):
        try:
            os.remove(txt_path)
            deleted = True
        except Exception as e:
            pass

    if deleted:
        return JSONResponse({"success": True, "message": f"成功删除 {safe_name}"})
    return JSONResponse({"error": "文件不存在"}, status_code=404)

@app.delete("/api/recordings")
async def batch_delete_recordings():
    deleted_count = 0
    if os.path.exists(RECORDINGS_DIR):
        for f in os.listdir(RECORDINGS_DIR):
            if f == recorder.current_filename and esp32_connected:
                continue
            if f.endswith(".wav") or f.endswith(".txt"):
                file_path = os.path.join(RECORDINGS_DIR, f)
                try:
                    os.remove(file_path)
                    if f.endswith(".wav"):
                        deleted_count += 1
                except Exception as e:
                    print(f"[File] Error deleting {f}: {e}")
    return JSONResponse({"success": True, "deleted_count": deleted_count})

@app.post("/api/recordings/{filename}/rename")
async def rename_recording(filename: str, request: Request):
    safe_old_name = os.path.basename(filename)
    if safe_old_name == recorder.current_filename and esp32_connected:
        return JSONResponse({"error": "该文件正在录制中，请在分卷完成后再重命名！"}, status_code=400)

    data = await request.json()
    new_raw = data.get("new_name", "").strip()
    if new_raw.endswith(".wav") or new_raw.endswith(".txt"):
        new_raw = os.path.splitext(new_raw)[0]
    new_base = re.sub(r'[/\\:*?"<>|]', '_', new_raw).strip(" ._")
    if not new_base:
        return JSONResponse({"error": "文件名不合法或为空"}, status_code=400)

    old_wav = os.path.join(RECORDINGS_DIR, safe_old_name)
    old_txt = os.path.join(RECORDINGS_DIR, os.path.splitext(safe_old_name)[0] + ".txt")

    new_wav_name = f"{new_base}.wav"
    new_txt_name = f"{new_base}.txt"
    new_wav = os.path.join(RECORDINGS_DIR, new_wav_name)
    new_txt = os.path.join(RECORDINGS_DIR, new_txt_name)

    if not os.path.exists(old_wav):
        return JSONResponse({"error": "原录音文件不存在"}, status_code=404)
    if os.path.exists(new_wav) and new_wav != old_wav:
        return JSONResponse({"error": f"目标文件名 '{new_wav_name}' 已存在"}, status_code=400)

    try:
        os.rename(old_wav, new_wav)
        if os.path.exists(old_txt):
            os.rename(old_txt, new_txt)
        return JSONResponse({"success": True, "new_filename": new_wav_name})
    except Exception as e:
        return JSONResponse({"error": f"重命名失败: {str(e)}"}, status_code=500)

# ---------------------------------------------------------
# WebSocket Endpoints
# ---------------------------------------------------------
@app.websocket("/ws/audio")
async def ws_audio_endpoint(websocket: WebSocket):
    global esp32_connected
    await websocket.accept()
    esp32_connected = True
    esp32_stats["connected"] = True
    print(f"[WebSocket] 🎙️ ESP32-S3 connected from {websocket.client.host}!")
    audio_filter.reset()
    segmenter.reset()
    await broadcast_json({"type": "device_status", "connected": True})

    try:
        while True:
            data = await websocket.receive_bytes()
            if not data:
                continue

            # 实时 80Hz 二阶高通语音滤波：切除低频机械振动、DC偏移与 WiFi 周期性底噪
            data = audio_filter.process(data)

            esp32_stats["total_bytes"] += len(data)
            esp32_stats["last_packet_time"] = time.time()

            # 1. 实时原声转发给所有正在网页收听的浏览器
            disconnected = set()
            for browser in list(active_browser_clients):
                try:
                    await browser.send_bytes(data)
                except Exception:
                    disconnected.add(browser)
            active_browser_clients.difference_update(disconnected)

            # 2. 连续写入长音频归档文件 (Track 1)
            recorder.write(data)

            # 3. 内存动态断句并送入 SenseVoice/Whisper (Track 2)
            utterances = segmenter.process_chunk(data)
            for utterance in utterances:
                ts = datetime.now().strftime("%H:%M:%S")
                transcription_queue.put((utterance, ts))

    except WebSocketDisconnect:
        print("[WebSocket] ESP32-S3 disconnected.")
    except Exception as e:
        print(f"[WebSocket] ESP32 error: {e}")
    finally:
        esp32_connected = False
        esp32_stats["connected"] = False
        recorder.close()
        segmenter.reset()
        await broadcast_json({"type": "device_status", "connected": False})

@app.websocket("/ws/live_listen")
async def ws_live_listen_endpoint(websocket: WebSocket):
    await websocket.accept()
    active_browser_clients.add(websocket)
    print(f"[Web] Browser connected for live audio listening ({len(active_browser_clients)} online)")

    # Send status & recent transcripts
    await websocket.send_json({"type": "device_status", "connected": esp32_connected})
    for item in transcripts_history[-15:]:
        await websocket.send_json({"type": "live_transcript", "data": item})

    try:
        while True:
            msg = await websocket.receive_text()
            if msg == "ping":
                await websocket.send_text("pong")
    except WebSocketDisconnect:
        pass
    finally:
        active_browser_clients.discard(websocket)

if __name__ == "__main__":
    import uvicorn
    host = "0.0.0.0"  # Listen on all local interfaces
    port = APP_CONFIG.get("SERVER_PORT", 8000)
    print(f"[Server] 🌐 Starting server at http://{host}:{port} (External port: {port})")
    uvicorn.run(app, host=host, port=port, log_level="info")
