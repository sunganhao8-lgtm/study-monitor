# Study Monitor 📚

基于小米摄像头 + MediaPipe Pose 的学习状态监控系统。

## 功能

- 实时读取小米摄像头视频流（通过 Micam RTSP 桥接）
- 人体关键点提取（MediaPipe Pose，33个关键点）
- 自动判断学习状态：✅学习 / 😴趴着 / 📱玩手机 / 🚶离开 / 🪑动来动去
- 状态日志记录（JSONL格式）
- 超时告警（趴着>5分钟 / 玩手机>2分钟 / 离开>1分钟）

## 架构

```
小米摄像头 ──WiFi──→ Miloco(拉流) → Micam(桥接) → Go2rtc(RTSP)
                                                        ↓
                                          Python study_monitor.py
                                          MediaPipe Pose → 行为判断 → 日志
```

## 快速开始

### 1. 启动 Docker 服务

```bash
cd D:\study-monitor

# 编辑 .env 填写配置（见下方说明）
notepad .env

# 启动
docker-compose up -d
```

### 2. 获取摄像头 DID

1. 浏览器打开 `https://localhost:8000`（Miloco WebUI，自签证书，点"高级→继续访问"）
2. 设置密码（和 .env 里的 MILOCO_PASSWORD 一致）
3. 绑定你的小米账号
4. 在设备列表里找到摄像头的 DID（数字ID）
5. 把 DID 填入 .env 的 `CAMERA_ID`
6. 重启：`docker-compose restart micam1`

### 3. 验证 RTSP 流

- Go2rtc WebUI: `http://localhost:1984`
- 用 VLC 播放: `rtsp://localhost:8554/stream1`

### 4. 启动监控

```bash
# 安装依赖
pip install -r requirements.txt

# 启动（调试模式，有画面）
python study_monitor.py --debug

# 后台运行（无窗口）
python study_monitor.py --no-display
```

或者直接双击 `start.bat`。

## .env 配置说明

| 变量 | 说明 | 默认值 |
|---|---|---|
| MILOCO_PASSWORD | Miloco WebUI 密码 | study123456 |
| CAMERA_ID | 摄像头DID（数字ID） | 需要填写 |
| RTSP_URL | RTSP推流地址 | rtsp://localhost:8554/stream1 |
| VIDEO_CODEC | 编码格式 | hevc（小米4双摄用H.265） |
| STREAM_CHANNEL | 流通道 | 0=主摄(广角) |

## 双摄版特别说明

小米智能摄像机4双摄版有两个镜头：
- `STREAM_CHANNEL=0` → 主摄（广角，适合看整个房间）
- `STREAM_CHANNEL=1` → 副摄（长焦，适合看桌面细节）

建议先用主摄（0），视角更广。

## 日志格式

每条日志一行 JSON：

```json
{
  "status": "studying",
  "confidence": 0.85,
  "head_y": 0.42,
  "body_visible": true,
  "left_hand_y": 0.55,
  "right_hand_y": 0.58,
  "timestamp": "2026-07-28 15:30:00",
  "duration_seconds": 120
}
```

## 常见问题

**Q: 摄像头连不上？**
- 确认 Docker 三个容器都在运行：`docker-compose ps`
- 确认 Miloco WebUI 能打开：`https://localhost:8000`
- 确认已绑定小米账号且摄像头在线

**Q: RTSP 流卡顿？**
- 局域网内一般 1-3 秒延迟，正常
- 如果很卡，把 VIDEO_CODEC 从 hevc 改成 h264 试试

**Q: 关键点检测不准？**
- 摄像头角度建议斜上方 45° 俯拍，能看到人上半身
- 光线要够，太暗会影响检测
- 模型复杂度可在代码里调（model_complexity: 0=快但不准，2=准但慢）

**Q: 想接飞书通知？**
- 在 `study_monitor.py` 的 `should_alert` 函数里加飞书 Webhook 调用即可
