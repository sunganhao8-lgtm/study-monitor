
def http_get_if_running(url):
    try:
        r = urlopen(url, timeout=1)
        return r.status == 200
    except Exception:
        return None

def open_rtsp():
    try:
        import cv2
        cap = cv2.VideoCapture("rtsp://localhost:8554/xiaomi_camera4")
        ok = cap.isOpened() and cap.read()[0]
        cap.release()
        return ok
    except Exception:
        return False

"""
Study Monitor - Web UI 后端
===========================
单文件 Flask 服务，提供：
- GET  /              → UI 页面
- GET  /api/status    → 实时状态
- GET  /api/logs      → 历史日志
- GET  /api/config    → 当前配置
- POST /api/config    → 更新配置
- POST /api/start     → 启动监控
- POST /api/stop      → 停止监控
- GET  /api/snapshot  → 当前画面截图
- GET  /api/stats     → 统计概览
"""

import json
import os
import time
import threading
import subprocess
import signal
import struct
import socket
import hashlib
from datetime import datetime, timedelta
from pathlib import Path
from flask import Flask, jsonify, request, send_from_directory, Response

SCRIPT_DIR = Path(__file__).parent
CONFIG_PATH = SCRIPT_DIR / "config.json"
LOG_PATH = SCRIPT_DIR / "logs" / "states.jsonl"
ALERT_PATH = SCRIPT_DIR / "logs" / "alerts.jsonl"
RECORDING_DIR = SCRIPT_DIR / "recordings"
HTML_PATH = SCRIPT_DIR / "ui.html"

# 确保目录存在
RECORDING_DIR.mkdir(exist_ok=True)
(SCRIPT_DIR / "logs").mkdir(exist_ok=True)

app = Flask(__name__, static_folder=str(SCRIPT_DIR))

# ─── 状态 ─────────────────────────────────────────────
class AppState:
    def __init__(self):
        self.process = None
        self.started_at = None
        self.last_state = None
        self.lock = threading.Lock()

    def load_config(self):
        with open(CONFIG_PATH, "r", encoding="utf-8") as f:
            return json.load(f)

    def save_config(self, cfg):
        with open(CONFIG_PATH, "w", encoding="utf-8") as f:
            json.dump(cfg, f, ensure_ascii=False, indent=2)

    def read_latest_log(self):
        """读取最新一条日志"""
        if not LOG_PATH.exists():
            return None
        try:
            # 从文件末尾读取最后一行（高效）
            with open(LOG_PATH, "rb") as f:
                f.seek(0, 2)
                size = f.tell()
                # 最多读最后 4KB
                f.seek(max(0, size - 4096))
                lines = f.read().decode("utf-8", errors="ignore").strip().split("\n")
                if lines and lines[-1]:
                    return json.loads(lines[-1])
        except Exception:
            pass
        return None

    def read_log_lines(self, limit=100):
        """读取最近 N 条日志"""
        if not LOG_PATH.exists():
            return []
        try:
            with open(LOG_PATH, "r", encoding="utf-8") as f:
                lines = f.readlines()
            return [json.loads(l) for l in lines[-limit:] if l.strip()]
        except Exception:
            return []

    def compute_stats(self, hours=1):
        """统计最近 N 小时的数据"""
        from datetime import datetime, timedelta
        cutoff = datetime.now() - timedelta(hours=hours)
        entries = self.read_log_lines(limit=10000)
        if not entries:
            return {}

        recent = []
        for e in entries:
            try:
                ts = datetime.strptime(e["timestamp"], "%Y-%m-%d %H:%M:%S")
                if ts >= cutoff:
                    recent.append(e)
            except Exception:
                continue

        if not recent:
            return {}

        # 按状态聚合
        status_count = {}
        status_duration = {}
        prev_status = None
        prev_ts = None

        for e in recent:
            s = e["status"]
            status_count[s] = status_count.get(s, 0) + 1

            ts = datetime.strptime(e["timestamp"], "%Y-%m-%d %H:%M:%S")
            if prev_status == s and prev_ts:
                delta = (ts - prev_ts).total_seconds()
                status_duration[s] = status_duration.get(s, 0) + delta
            prev_status = s
            prev_ts = ts

        total = len(recent)
        total_duration_sec = sum(status_duration.values())

        # 切换次数
        transitions = 0
        prev = None
        for e in recent:
            if e["status"] != prev:
                transitions += 1
                prev = e["status"]

        return {
            "total_entries": total,
            "total_duration_sec": round(total_duration_sec),
            "by_status": {k: {"count": v, "duration_sec": round(status_duration.get(k, 0))}
                          for k, v in status_count.items()},
            "focus_score": round((status_count.get("studying", 0) / max(total, 1)) * 100),
            "transitions": transitions,
            "time_range": {
                "from": recent[0]["timestamp"],
                "to": recent[-1]["timestamp"],
            }
        }


state = AppState()


# ─── 路由 ─────────────────────────────────────────────
@app.route("/")
def index():
    if HTML_PATH.exists():
        return send_from_directory(str(SCRIPT_DIR), "ui.html")
    return "<h1>ui.html not found</h1>", 404


@app.route("/api/status")
def api_status():
    """实时状态"""
    latest = state.read_latest_log()
    cfg = state.load_config()
    is_running = state.process is not None and state.process.poll() is None

    return jsonify({
        "running": is_running,
        "pid": state.process.pid if is_running else None,
        "started_at": state.started_at,
        "current_state": latest,
        "rtsp": cfg["monitor"]["rtsp"],
    })


@app.route("/api/config", methods=["GET", "POST"])
def api_config():
    if request.method == "GET":
        return jsonify(state.load_config())

    new_cfg = request.json
    state.save_config(new_cfg)
    return jsonify({"ok": True})


@app.route("/api/logs")
def api_logs():
    limit = int(request.args.get("limit", 100))
    return jsonify(state.read_log_lines(limit=limit))


@app.route("/api/alerts")
def api_alerts():
    """列出告警记录，按 status_ 过滤（默认全部）"""
    status_filter = request.args.get("status", "all")  # pending/confirmed/dismissed/all
    limit = int(request.args.get("limit", 50))

    if not ALERT_PATH.exists():
        return jsonify([])

    try:
        with open(ALERT_PATH, "r", encoding="utf-8") as f:
            lines = f.readlines()
    except Exception:
        return jsonify([])

    alerts = []
    for line in lines[-500:]:  # 先取最近 500 条
        line = line.strip()
        if not line:
            continue
        try:
            a = json.loads(line)
            if status_filter == "all" or a.get("status_") == status_filter:
                alerts.append(a)
        except Exception:
            continue

    return jsonify(alerts[-limit:])


@app.route("/api/alerts/<alert_id>/confirm", methods=["POST"])
def api_alert_confirm(alert_id):
    """确认告警 → 写入文件状态 + 触发语音"""
    if not ALERT_PATH.exists():
        return jsonify({"ok": False, "message": "no alerts"})

    # 读所有行，修改对应那一条
    updated = None
    new_lines = []
    with open(ALERT_PATH, "r", encoding="utf-8") as f:
        for line in f:
            try:
                a = json.loads(line)
                if a.get("id") == alert_id:
                    a["status_"] = "confirmed"
                    a["confirmed_at"] = datetime.now().isoformat()
                    updated = a
                new_lines.append(json.dumps(a, ensure_ascii=False))
            except Exception:
                new_lines.append(line.strip())

    with open(ALERT_PATH, "w", encoding="utf-8") as f:
        f.write("\n".join(new_lines) + "\n")

    if updated is None:
        return jsonify({"ok": False, "message": "not found"})

    # 触发语音播报（在后台跑）
    import threading
    import asyncio

    def _speak():
        try:
            import edge_tts
            import pygame
            import tempfile

            async def run():
                pygame.mixer.init()
                comm = edge_tts.Communicate(updated["alert_msg"], "zh-CN-XiaoxiaoNeural")
                tmp = tempfile.NamedTemporaryFile(suffix=".mp3", delete=False).name
                await comm.save(tmp)
                pygame.mixer.music.load(tmp)
                pygame.mixer.music.play()
                while pygame.mixer.music.get_busy():
                    import time as _t
                    _t.wait(100)
                pygame.mixer.music.unload()
                import os as _os
                try:
                    _os.unlink(tmp)
                except Exception:
                    pass

            asyncio.run(run())
        except Exception as e:
            print(f"语音播报失败: {e}")

    threading.Thread(target=_speak, daemon=True).start()
    return jsonify({"ok": True, "alert": updated})


@app.route("/api/alerts/<alert_id>/dismiss", methods=["POST"])
def api_alert_dismiss(alert_id):
    """忽略告警"""
    if not ALERT_PATH.exists():
        return jsonify({"ok": False, "message": "no alerts"})

    new_lines = []
    found = False
    with open(ALERT_PATH, "r", encoding="utf-8") as f:
        for line in f:
            try:
                a = json.loads(line)
                if a.get("id") == alert_id:
                    a["status_"] = "dismissed"
                    a["dismissed_at"] = datetime.now().isoformat()
                    found = True
                new_lines.append(json.dumps(a, ensure_ascii=False))
            except Exception:
                new_lines.append(line.strip())

    with open(ALERT_PATH, "w", encoding="utf-8") as f:
        f.write("\n".join(new_lines) + "\n")

    return jsonify({"ok": found})


@app.route("/api/recordings")
def api_recordings():
    """列出所有录制的视频"""
    if not RECORDING_DIR.exists():
        return jsonify([])
    files = []
    for f in sorted(RECORDING_DIR.glob("*.mp4"), key=lambda x: x.stat().st_mtime, reverse=True):
        files.append({
            "filename": f.name,
            "size_mb": round(f.stat().st_size / 1024 / 1024, 2),
            "modified": datetime.fromtimestamp(f.stat().st_mtime).isoformat(),
            "url": f"/recordings/{f.name}",
        })
    return jsonify(files[:50])


@app.route("/recordings/<path:filename>")
def api_recording_file(filename):
    """提供录制的视频（支持 Range 请求，浏览器可以用 seek）"""
    from flask import send_file, Response, request as flask_request
    
    path = RECORDING_DIR / filename
    if not path.exists():
        return "Not found", 404
    
    range_header = flask_request.headers.get("Range", "")
    if range_header:
        file_size = path.stat().st_size
        start, end = 0, file_size - 1
        try:
            parts = range_header.replace("bytes=", "").split("-")
            start = int(parts[0])
            end = int(parts[1]) if parts[1] else file_size - 1
        except (ValueError, IndexError):
            pass
        
        if start >= file_size:
            return Response("", 416)
        
        length = end - start + 1
        with open(path, "rb") as f:
            f.seek(start)
            data = f.read(length)
        
        resp = Response(data, 206, mimetype="video/mp4",
                        direct_passthrough=True)
        resp.headers.add("Content-Range", f"bytes {start}-{end}/{file_size}")
        resp.headers.add("Accept-Ranges", "bytes")
        resp.headers.add("Content-Length", str(length))
        resp.headers.add("Cache-Control", "no-cache")
        return resp
    
    return send_file(path, mimetype="video/mp4",
                     as_attachment=False)


@app.route("/api/stats")
def api_stats():
    hours = float(request.args.get("hours", 1))
    return jsonify(state.compute_stats(hours=hours))


@app.route("/api/snapshot/auto")
def api_snapshot_auto():
    """持续推送截图（服务器推送）"""
    import cv2
    import base64

    cfg = state.load_config()
    rtsp = cfg["monitor"]["rtsp"]

    def generate():
        cap = cv2.VideoCapture(rtsp)
        try:
            while True:
                ret, frame = cap.read()
                if ret:
                    _, buf = cv2.imencode(".jpg", frame, [cv2.IMWRITE_JPEG_QUALITY, 75])
                    yield (b"--frame\r\n"
                           b"Content-Type: image/jpeg\r\n\r\n" + buf.tobytes() + b"\r\n")
                else:
                    cap.release()
                    import time
                    time.sleep(2)
                    cap = cv2.VideoCapture(rtsp)
        finally:
            cap.release()

    return Response(generate(),
                    mimetype="multipart/x-mixed-replace; boundary=frame")


@app.route("/api/start", methods=["POST"])
def api_start():
    """启动监控进程"""
    with state.lock:
        if state.process and state.process.poll() is None:
            return jsonify({"ok": False, "message": "已经在运行"})

        # 启动 study_monitor.py
        state.process = subprocess.Popen(
            ["python", "study_monitor.py", "--no-display"],
            cwd=str(SCRIPT_DIR),
            creationflags=subprocess.CREATE_NEW_PROCESS_GROUP if os.name == "nt" else 0,
        )
        state.started_at = datetime.now().isoformat()
        return jsonify({"ok": True, "pid": state.process.pid})


@app.route("/api/stop", methods=["POST"])
def api_stop():
    """停止监控进程"""
    with state.lock:
        if not state.process or state.process.poll() is not None:
            return jsonify({"ok": False, "message": "未运行"})

        try:
            if os.name == "nt":
                state.process.terminate()
                try:
                    state.process.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    state.process.kill()
            else:
                state.process.send_signal(signal.SIGTERM)
        except Exception as e:
            return jsonify({"ok": False, "message": str(e)})

        state.process = None
        return jsonify({"ok": True})


@app.route("/api/snapshot")
def api_snapshot():
    """从 RTSP 抓取一帧"""
    try:
        import cv2
        cfg = state.load_config()
        rtsp = cfg["monitor"]["rtsp"]
        cap = cv2.VideoCapture(rtsp)
        ret, frame = cap.read()
        cap.release()
        if not ret:
            return jsonify({"ok": False, "message": "无法读取画面"})

        # 编码为 JPEG
        import base64
        _, buf = cv2.imencode(".jpg", frame, [cv2.IMWRITE_JPEG_QUALITY, 80])
        b64 = base64.b64encode(buf.tobytes()).decode()
        return jsonify({"ok": True, "image": f"data:image/jpeg;base64,{b64}"})
    except Exception as e:
        return jsonify({"ok": False, "message": str(e)})


@app.route("/api/go2rtc/status")
def api_go2rtc():
    """检查 go2rtc 是否在跑"""
    import requests
    try:
        r = requests.get("http://localhost:1984/api/streams", timeout=2)
        return jsonify({"running": True, "streams": r.json()})
    except Exception as e:
        return jsonify({"running": False, "message": str(e)})


@app.route("/api/camera/ptz", methods=["POST"])
def api_camera_ptz():
    """控制摄像头云台转动"""
    data = request.json
    direction = data.get("direction", "stop")  # up/down/left/right/stop
    angle = data.get("angle", 5) 
    try:
        result = control_camera_ptz(direction, angle)
        return jsonify({"ok": True, "direction": direction, "result": result})
    except Exception as e:
        return jsonify({"ok": False, "message": str(e)})


def _miio_encrypt(token_bytes, payload):
    """miio 协议加密"""
    magic = bytes.fromhex("21310020ffffffffffffffffffffffffffffffffffffffffffffffffffffffff")
    length = len(payload) + 32
    header = magic[:2] + struct.pack(">H", length) + magic[4:8]
    device_id = bytes.fromhex("ffffffff")
    stamp = struct.pack(">I", int(time.time()))
    token_encrypted = hashlib.md5(token_bytes).digest()
    checksum_data = header + device_id + stamp + token_bytes + payload
    checksum = hashlib.md5(checksum_data).digest()
    return header + device_id + stamp + checksum + payload


def _miio_send_command(ip, token_hex, method, params):
    """发送 miio 命令"""
    token = bytes.fromhex(token_hex)
    payload = json.dumps({
        "id": int(time.time() * 1000) % 100000,
        "method": method,
        "params": params,
    }).encode("utf-8")
    packet = _miio_encrypt(token, payload)

    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.settimeout(3)
    sock.sendto(packet, (ip, 54321))
    try:
        data, _ = sock.recvfrom(4096)
        body = data[32:]
        return json.loads(body)
    except socket.timeout:
        return {"error": "timeout", "result": []}
    finally:
        sock.close()


def control_camera_ptz(direction="stop", angle=5):
    """控制摄像头云台"""
    ip = "192.168.1.159"
    token = "65657353534a73626155626663655675"
    direction_map = {
        "up": ("set_motor", [0, angle]),
        "down": ("set_motor", [1, angle]),
        "left": ("set_motor", [2, angle]),
        "right": ("set_motor", [3, angle]),
        "stop": ("set_motor", [4, 0]),
    }
    method, params = direction_map.get(direction, ("set_motor", [4, 0]))
    return _miio_send_command(ip, token, method, params)




@app.route("/api/health")
def api_health():
    """健康检查"""
    return jsonify({
        "ok": True,
        "go2rtc": bool(http_get_if_running("http://localhost:1984/api/streams")),
        "rtsp": bool(open_rtsp()),
    })


# ─── 启动 ─────────────────────────────────────────────
if __name__ == "__main__":
    print("=" * 60)
    print("  Study Monitor Web UI")
    print("=" * 60)
    print(f"  访问: http://localhost:8765")
    print(f"  配置: {CONFIG_PATH}")
    print(f"  日志: {LOG_PATH}")
    print("=" * 60)
    app.run(host="0.0.0.0", port=8765, debug=False)