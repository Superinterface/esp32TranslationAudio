# ESP32-S3 + INMP441 实时音频监听、录音归档与 SenseVoice 多语种实时转写系统

[![ESP-IDF](https://img.shields.io/badge/ESP--IDF-v5.x%20%7C%20v6.x-blue.svg)](https://idf.espressif.com/)
[![FastAPI](https://img.shields.io/badge/Backend-FastAPI%20%2B%20WebSockets-green.svg)](https://fastapi.tiangolo.com/)
[![ASR Engine](https://img.shields.io/badge/ASR-SenseVoice--Small%20(ONNX)-red.svg)](https://github.com/FunAudioLLM/SenseVoice)
[![VAD](https://img.shields.io/badge/VAD-Silero--VAD%20v5-purple.svg)](https://github.com/snakers4/silero-vad)
[![License](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)

一个基于 **ESP32-S3** 和 **INMP441 硅麦** 的高可靠软硬一体化音频系统。通过 WiFi WebSocket 将微秒级硬件采样的 16kHz 16-bit 音频流实时推送至本地服务端，实现：

- 🎧 **超低延迟实时监听**：基于 Web Audio API 实现浏览器端 50~80ms 超低延迟收听，配备实时动态波形示波器。
- 🎙️ **智能分段与长音频归档**：会话级连续录音为标准 `.wav` 格式，保障原始音频无损存档。
- ⚡ **毫秒级离线 ASR 识别**：采用阿里巴巴 **SenseVoice-Small** (ONNX 量化版) + **Silero-VAD** 神经网络断句，推理延迟 < 100ms，识别率远超同量级模型。
- 🌐 **多语种识别与同传翻译**：支持中、英、日、韩、粤多语种混合识别，并支持实时同传翻译为中文。
- 📂 **网页端录音与文件管理中心**：支持音频与转写文本联动重命名、在线查看完整转写、一键复制、单条/批量删除与磁盘看板。
- 🛡️ **高可用自愈通信**：内置 WebSocket 双向心跳（Ping/Pong）、TCP Keep-Alive、意外断连看门狗自动重连，服务端重启无感秒级恢复。

---

## 🏗️ 系统架构与数据流

```text
  ┌─────────────────────────────────────────────────────────────┐
  │                    ESP32-S3 + INMP441                       │
  │  • I2S 硬件 DMA (原生单声道左槽，彻底杜绝悬空总线爆音)       │
  │  • 浮点高通去直流滤波 (消除低频底噪漂移)                      │
  │  • WebSocket 客户端 (心跳保活 + 自动断线重连看门狗)           │
  └──────────────────────────────┬──────────────────────────────┘
                                 │ WiFi (WebSocket Binary PCM 16kHz 16bit)
                                 ▼
  ┌─────────────────────────────────────────────────────────────┐
  │                   Python FastAPI 本地服务端                  │
  │                                                             │
  │  [Track 1: 实时监听广播] ──► Web Audio API 浏览器播放 (~50ms)│
  │                                                             │
  │  [Track 2: 长音频落盘]   ──► session_YYYYMMDD_HHMMSS.wav     │
  │                                                             │
  │  [Track 3: AI 语音识别]                                      │
  │      │                                                      │
  │      ├─► Silero-VAD 神经网络实时静音检测与断句               │
  │      ├─► SenseVoice-Small ONNX 毫秒级语音转文字             │
  │      ├─► 实时同传翻译引擎 (可选)                            │
  │      └─► WebSocket 广播实时字幕 + 同步追加 .txt 归档        │
  └──────────────────────────────┬──────────────────────────────┘
                                 │ HTTP / WebSockets
                                 ▼
  ┌─────────────────────────────────────────────────────────────┐
  │                   现代 Web 响应式交互界面                   │
  │  • 实时听音控制开关 & 音量调节                               │
  │  • 实时音频示波器 (Canvas 60fps)                            │
  │  • 实时滚动字幕瀑布流 (带时间戳、情感标签与翻译)            │
  │  • 录音文件管理中心 (在线查看转写、一键复制、重命名联动、批量删除)│
  └─────────────────────────────────────────────────────────────┘
```

---

## 🔌 硬件接线表 (INMP441 ➔ ESP32-S3)

本工程针对标准 **INMP441 I2S 全数字麦克风** 优化，默认引脚与小智 AI 硬件兼容：

| INMP441 麦克风引脚 | ESP32-S3 引脚 | 功能说明 |
| :--- | :--- | :--- |
| **WS** (Word Select) | **GPIO 4** | 声道选择时钟 / 帧时钟 (LRCLK) |
| **SCK** (Serial Clock) | **GPIO 5** | 串行位时钟 (BCLK) |
| **SD** (Serial Data) | **GPIO 6** | 串行音频数据输出 (DIN) |
| **L/R** (Left/Right) | **GND** | **选左声道（必须接 GND，硬件只采集左声道）** |
| **VDD** | **3.3V** | ⚠️ **严禁接 5V**，该传感器额定电压 3.3V |
| **GND** | **GND** | 系统电源地 |

> 💡 **提示**：引脚可在固件配置菜单 `idf.py menuconfig` 中任意重新映射，无需修改源代码。

---

## 📦 目录结构

```text
esp32TranslationAudio/
├── config.env.example          # 统一系统配置模板（复制为 config.env 使用）
├── .gitignore                  # Git 防泄露规则配置
├── README.md                   # 项目使用与架构文档
├── firmware/                   # ESP32-S3 ESP-IDF 固件工程
│   ├── CMakeLists.txt
│   ├── sdkconfig.defaults      # 默认配置模板（已剔除私人密码）
│   └── main/
│       ├── CMakeLists.txt
│       ├── idf_component.yml   # 官方 esp_websocket_client 组件依赖
│       ├── Kconfig.projbuild   # 图形化配置菜单项定义
│       └── main.c              # 固件主源码（DMA采集、去直流、WS自愈）
├── server/                     # Python 后端与 Web 前端
│   ├── app.py                  # FastAPI 主服务（三轨音频管道 + 管理 API）
│   ├── pyproject.toml          # uv / pip 依赖管理配置
│   ├── requirements.txt        # 传统 pip 依赖清单
│   ├── download_models.py      # 一键模型下载脚本
│   ├── models/                 # AI 模型存放目录（gitignore 忽略）
│   │   ├── silero_vad.onnx     # Silero VAD 模型权重
│   │   └── sensevoice/         # SenseVoice-Small ONNX 模型目录
│   ├── recordings/             # 录音与文本存储目录（自动创建）
│   └── static/
│       └── index.html          # 单文件现代化 Web 客户端界面
```

---

---

## 🚀 快速上手与运行指引

### 准备环境
1. **硬件**：ESP32-S3 开发板一台，INMP441 数字麦克风一个，Type-C 数据线。
2. **PC/Mac**：
   - Python 3.11 或 3.12（推荐使用极速包管理器 [`uv`](https://github.com/astral-sh/uv)）
   - ESP-IDF v5.1+ 或 v6.x 工具链

---

### 第一步：创建并填写统一配置文件（极简开箱）

在工程根目录下，从模板创建 `config.env`（该文件已在 `.gitignore` 中，不会泄露到 GitHub）：
```bash
cp config.env.example config.env
```
用任意文本编辑器打开 `config.env`，填入您的 WiFi 和电脑局域网 IP 即可：
```ini
# 1. 路由器 WiFi
WIFI_SSID=你的WiFi名称
WIFI_PASSWORD=你的WiFi密码

# 2. 电脑局域网 IP (macOS 运行 ifconfig 查看)
SERVER_HOST=192.168.1.100
SERVER_PORT=8000
```
> ✨ **核心便利**：只要在 `config.env` 中填写一次，**固件 CMake 编译** 与 **Python 后端** 会自动读取生效，**完全无需手动执行 menuconfig 或修改任何 C 语言源码！**

---

### 第二步：启动 Python 本地服务端

1. **进入服务端目录并安装依赖**：
   ```bash
   cd server
   uv sync
   ```

2. **自动下载 AI 识别模型**（内置一键下载脚本）：
   ```bash
   uv run python download_models.py
   ```

3. **启动后端服务**：
   ```bash
   uv run python app.py
   ```
   终端显示如下即启动成功：
   ```text
   [Config] ⚙️  Loaded configuration from: .../config.env
   [VAD] 🧠 Silero VAD active!
   [ASR] 🚀 Loading Alibaba SenseVoice-Small...
   [Server] 🌐 Starting server at http://0.0.0.0:8000
   ```

---

### 第三步：编译并烧录 ESP32-S3 固件

1. **进入固件目录并激活 IDF 环境**：
   ```bash
   cd ../firmware
   # 请根据你的 IDF 路径激活（例如：. $HOME/esp/esp-idf/export.sh）
   ```

2. **一键编译并烧录**（CMake 会自动将 `config.env` 编译进固件）：
   ```bash
   idf.py flash monitor
   ```

3. **观察开机连接日志**：
   ```text
   I (xxxx) AUDIO_STREAMER: WiFi Connected! IP Address: 192.168.1.123
   I (xxxx) AUDIO_STREAMER: Initializing WebSocket connection to: ws://192.168.1.100:8000/ws/audio
   I (xxxx) AUDIO_STREAMER: WebSocket connected to Mac server!
   I (xxxx) AUDIO_STREAMER: Audio streaming task running (Gain factor: 2x)...
   ```

---

### 第三步：体验 Web 端全功能

在浏览器打开：`http://localhost:8000`（或局域网内其他设备访问 `http://<你的电脑IP>:8000`）：

1. **状态自动变绿**：右上角显示 `ESP32 设备：已连接`，画布上的音频波形开始实时跳动。
2. **开启实时监听**：点击 **【▶ 开启实时收听】**，可以通过电脑扬声器/耳机听到极低延迟的现场声音。
3. **实时语音识别与同传**：对着麦克风说话，AI 自动切除停顿静音，实时将文字追加到右侧字幕列表。
4. **录音与文本管理**：
   - 滚动到底部进入 **“录音与转文字文件管理”**。
   - 点击 **“查看文字”**：浮窗显示全文，支持 **“一键复制”**。
   - 点击 **“重命名”**：输入新名称，`.wav` 音频文件和 `.txt` 转写记录**自动同步改名**。
   - 支持多选**批量删除**或**单条下载**。

---

## 🛠️ 关键技术特性与避坑指南

### 1. 彻底根除麦克风“啪啪”规律性刺耳爆音
- **问题根因**：INMP441 配置为单声道时，右声道引脚高阻悬空（总线电容残留电平接近 `0x7FFFFFFF`）。很多开源代码在声音过零点（`sample == 0`）时误切换到右声道，导致产生高达 `+32000` 的满量程削波尖峰！
- **本方案解法**：采用原生 `I2S_SLOT_MODE_MONO` 与 `I2S_STD_SLOT_LEFT`，硬件 DMA 仅绑定左槽，完全无视悬空引脚；配合浮点 IIR 高通滤波消除整型截断振荡，波形纯净平滑。

### 2. 服务端重启无感秒级自愈
- **问题根因**：ESP-IDF 官方 WebSocket 客户端默认 `enable_close_reconnect = false`，一旦服务端重启，ESP32 判定为正常关闭并彻底停止任务。
- **本方案解法**：启用自动复连选项，配置 3 秒主动 Ping 心跳与 4 秒超时判定，并在推流任务中加入自愈看门狗，服务端无论是热重载还是崩溃重启，ESP32 均在 1~3 秒内自动复连。

---

## 📄 开源许可证

本项目采用 [MIT License](LICENSE) 开源许可。欢迎提交 Issue 与 Pull Request！
