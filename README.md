# Study Monitor 📚

基于小米摄像头 + MediaPipe Pose/Face + Audio VAD 的多维度学习状态监控系统。

## 功能

- 实时读取小米摄像头视频流（通过 go2rtc cs2+tcp 协议）
- **9 种行为状态识别**：学习中 / 打瞌睡/闭眼 / 趴着睡 / 看别处 / 看桌面 / 玩手机 / 发呆 / 动来动去 / 离开
- **多维度检测**：
  - 🧍 身体姿态（MediaPipe Pose，33关键点）
  - 👁 眼睛开合度（动态EAR基线校准）
  - 🧠 头部朝向（yaw/pitch）
  - ✋ 手部运动量（写字/翻书检测）
  - 🔊 人声检测（ffmpeg RMS VAD，读题目 = 学习中）
- **人工审核告警**：每个告警需要你确认才播报语音（edge-tts 微软晓晓）
- **自动视频录制**：告警触发前后各 15 秒存档
- **云台控制**：低置信度自动扫描 + Web UI 手动方向按钮
- **Web 配置面板**：所有参数可视化调整（阈值/时长/声优/提醒文案）
- **统计概览**：各状态时长条形图 + 次数分布 + 专注度评分

## 架构

```
小米摄像头(192.168.1.159)
    ↓ cs2+tcp P2P
go2rtc (Windows 原生)
    ↓ RTSP
study_monitor.py ─┬─ MediaPipe Pose → posture
                  ├─ MediaPipe Face → eyes/yaw/pitch
                  ├─ ffmpeg VAD → speech
                  ├─ VideoRecorder → MP4
                  └─ AudioMonitor → speech detection
    ↓ HTTP API
web_ui.py (Flask) + ui.html → 浏览器控制面板
```

## 快速开始

### 前置条件

- Windows 10+ / macOS / Linux
- Python 3.11+
- ffmpeg + ffplay（在 PATH 中）
- go2rtc（Windows：`D:\study-monitor\go2rtc_bin\go2rtc.exe`）
- 小米账号（用于 go2rtc 登录）
- 摄像头和电脑在同一局域网

### 安装

```bash
cd D:\study-monitor
pip install -r requirements.txt
```

### 启动

1. 启动 go2rtc：
```bash
./go2rtc_bin/go2rtc.exe -config go2rtc.yaml
```

2. 打开 go2rtc WebUI `http://localhost:1984`，用小米账号登录，加载摄像头

3. 启动 Web 面板：
```bash
python web_ui.py
```

4. 浏览器访问 `http://localhost:8765`，点击 **▶ 启动监控**

或者双击 `start.bat` 一键启动。

## 文件说明

| 文件 | 作用 |
|---|---|
| `study_monitor.py` | 核心监控脚本（姿态/面部/音频分析） |
| `web_ui.py` | Flask Web 后端（API + 告警审核 + 云台控制） |
| `ui.html` | 单文件 Web 前端（仪表盘/配置/日志） |
| `go2rtc.yaml` | go2rtc 流媒体配置 |
| `config.json` | UI 配置（阈值/告警/声优/提醒文案） |
| `.env` | 摄像头 DID 和密码（私密，不上传 GitHub） |

## 注意事项

- `.env` 中的 `CAMERA_ID` 和 `MILOCO_PASSWORD` 是私密信息
- 录制视频存于 `recordings/` 目录（每个文件约 3-5 MB）
- 日志存于 `logs/` 目录（JSONL 格式）
- 首次启动 MediaPipe 需要下载模型文件（~8 MB，自动下载）
