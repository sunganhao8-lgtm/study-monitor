"""
Study Monitor v2 - 多维度学习状态监控
========================================
基于 MediaPipe Pose + Face Landmarker 的多维度行为分析

检测维度：
  1. 身体姿态（Pose）— 趴着/坐着/离开
  2. 头部朝向（Face）— 转头看别处
  3. 眼睛开合度（EAR）— 打瞌睡/闭眼
  4. 微动/静止检测 — 写字 vs 发呆
  5. 眨眼频率 — 困倦检测

用法：
  python study_monitor.py                # 后台运行
  python study_monitor.py --debug        # 带画面调试
"""

import cv2
import mediapipe as mp
import time
import argparse
import json
import os
import urllib.request
import math
import threading
from datetime import datetime
from collections import deque
from dataclasses import dataclass, asdict, field
from typing import Optional, List


# ─── 配置 ───────────────────────────────────────────────
DEFAULT_RTSP = "rtsp://localhost:8554/xiaomi_camera4"

# MediaPipe 模型路径
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
POSE_MODEL_PATH = os.path.join(SCRIPT_DIR, "pose_landmarker.task")
FACE_MODEL_PATH = os.path.join(SCRIPT_DIR, "face_landmarker.task")
POSE_MODEL_URL = "https://storage.googleapis.com/mediapipe-models/pose_landmarker/pose_landmarker_heavy/float16/latest/pose_landmarker_heavy.task"
FACE_MODEL_URL = "https://storage.googleapis.com/mediapipe-models/face_landmarker/face_landmarker/float16/latest/face_landmarker.task"

# ─── 姿态判断阈值 ───
HEAD_DOWN_THRESHOLD = 0.65        # 鼻子Y > 此值 = 低头/趴桌
BODY_GONE_THRESHOLD = 0.15        # 肩膀Y < 此值 = 离开画面
HAND_BELOW_DESK = 0.75           # 手腕Y > 此值 = 手在桌下
MOVEMENT_THRESHOLD = 0.08         # 关键点帧间位移 > 此值 = 在动

# ─── 头部朝向阈值 ───
HEAD_YAW_THRESHOLD = 0.10         # 左右转头超过此值 = 看别处
HEAD_PITCH_DOWN = 0.55            # 低头角度（鼻子Y 接近此值）

# ─── 眼睛开合度阈值 (EAR: Eye Aspect Ratio) ───
EYE_CLOSED_THRESHOLD = 0.15       # EAR < 此值 = 闭眼
EYE_DROWSY_THRESHOLD = 0.22       # EAR 在 0.15-0.22 之间 = 半闭眼/困倦
BLINK_NORMAL_MIN = 0.3           # 正常眨眼 EAR 峰值

# ─── 状态判断时间阈值（秒）───
SLEEP_ALERT_AFTER = 30           # 趴着超过30秒
DROWSY_ALERT_AFTER = 5           # 闭眼超过5秒（打瞌睡）
LOOK_AWAY_ALERT_AFTER = 10       # 看别处超过10秒
PHONE_ALERT_AFTER = 20           # 玩手机超过20秒
AWAY_ALERT_AFTER = 30            # 离开超过30秒告警
IDLE_ALERT_AFTER = 60            # 静止不动超过60秒（发呆）
MOVING_ALERT_AFTER = 120         # 动来动去超过2分钟告警
CHECK_INTERVAL = 2               # 每2秒分析一次

# ─── 语音配置 ───
VOICE_ENABLED = True
VOICE_NAME = "zh-CN-XiaoxiaoNeural"
TTS_COOLDOWN = 30                 # 同类提醒冷却时间（秒）

# ─── 云台自动控制 ───
PTZ_AUTO_SCAN_ENABLED = True      # 低置信度时自动转动摄像头
PTZ_LOW_CONFIDENCE_THRESHOLD = 0.50  # 置信度低于此值触发扫描
PTZ_SCAN_INTERVAL = 60            # 每60秒最多扫一次
PTZ_CAMERA_IP = "192.168.1.159"
PTZ_CAMERA_TOKEN = "65657353534a73626155626663655675"

# 扫描序列（正常位 → 左看 → 右看 → 下看 → 回正）
PTZ_SCAN_SEQUENCE = [
    ("left", 5),
    ("right", 5),
    ("right", 5),
    ("down", 3),
    ("up", 3),
    ("left", 3),
    ("stop", 0),
]

# ─── 日志 ───
LOG_DIR = os.path.join(SCRIPT_DIR, "logs")


# ─── 工具函数 ─────────────────────────────────────────
def download_if_missing(url: str, path: str):
    """下载模型（如果不存在）"""
    if not os.path.exists(path):
        print(f"📥 下载模型: {os.path.basename(path)}")
        urllib.request.urlretrieve(url, path)
        print(f"✅ 完成: {os.path.getsize(path) // 1024} KB")


class AudioMonitor:
    """通过 ffmpeg 监听 RTSP 音频流的音量/人声检测
    不依赖 pyaudio/VAD 等外部库，直接用 ffmpeg 解 OPUS 音频
    """
    def __init__(self, rtsp_url: str):
        self.rtsp_url = rtsp_url
        self.process = None
        self.running = False
        self.speech_detected = False
        self.last_speech_time = 0
        self.audio_level = 0.0
        self._lock = threading.Lock()

    def start(self):
        """启动 ffmpeg 音频解码线程"""
        if self.running:
            return
        self.running = True
        thread = threading.Thread(target=self._loop, daemon=True)
        thread.start()

    def stop(self):
        self.running = False
        if self.process:
            self.process.terminate()
            self.process = None

    def _loop(self):
        """主循环：ffmpeg 读音频 → 计算 RMS 音量 → 判断人声"""
        # ffmpeg 命令：从 RTSP 取音频，转 PCM 16bit 单声道，输出到管道
        cmd = [
            "ffmpeg",
            "-loglevel", "error",
            "-i", self.rtsp_url,
            "-vn",                # 不要视频
            "-acodec", "pcm_s16le",
            "-ac", "1",           # 单声道
            "-ar", "16000",       # 16kHz
            "-f", "s16le",
            "pipe:1",
        ]
        import subprocess as sp
        self.process = sp.Popen(cmd, stdout=sp.PIPE, stderr=sp.DEVNULL, bufsize=0)
        chunk_size = 1600  # 100ms @ 16kHz 单声道
        num_chunks_in_window = 10  # 1秒窗口

        rms_window = deque(maxlen=num_chunks_in_window)
        while self.running:
            try:
                data = self.process.stdout.read(chunk_size * 2)  # 16bit = 2 bytes per sample
                if len(data) < chunk_size * 2:
                    break

                # 计算 RMS
                samples = []
                for i in range(0, len(data), 2):
                    val = int.from_bytes(data[i:i+2], byteorder='little', signed=True)
                    samples.append(val)

                if len(samples) > 0:
                    sq = sum(s**2 for s in samples) / len(samples)
                    rms = sq ** 0.5
                    rms_window.append(rms)

                # 每 1 秒判断一次
                if len(rms_window) == num_chunks_in_window:
                    avg_rms = sum(rms_window) / len(rms_window)
                    with self._lock:
                        self.audio_level = avg_rms
                        # 安静的环境里 RMS 一般很低（< 500）
                        # 人大声说话/读书时 RMS 会跳起来（> 2000）
                        self.speech_detected = avg_rms > 1500
                        if self.speech_detected:
                            self.last_speech_time = time.time()
                    rms_window.clear()

            except (BrokenPipeError, OSError):
                break

        self.process = None

    @property
    def is_speaking_now(self) -> bool:
        """最近 3 秒内有没有检测到人声"""
        return time.time() - self.last_speech_time < 3.0


class VideoRecorder:
    """环形视频缓存 + 告警触发的视频保存"""
    def __init__(self, rtsp_url: str, pre_seconds=15, post_seconds=15,
                 fps=10, output_dir="recordings"):
        self.rtsp_url = rtsp_url
        self.pre_seconds = pre_seconds
        self.post_seconds = post_seconds
        self.fps = fps
        self.output_dir = os.path.join(SCRIPT_DIR, output_dir)
        os.makedirs(self.output_dir, exist_ok=True)

        # 环形缓冲区（保存最近 pre_seconds 秒的帧）
        self.buffer = deque(maxlen=pre_seconds * fps)
        self.recording = False
        self.post_frames_remaining = 0
        self.current_writer = None
        self.current_path = None
        self.current_metadata = {}

    def push_frame(self, frame):
        """持续往缓冲区塞帧"""
        self.buffer.append(frame.copy())
        if self.recording and hasattr(self, "_writer") and self._writer is not None:
            try:
                self._writer.write(frame)
                self.post_frames_remaining -= 1
                if self.post_frames_remaining <= 0:
                    self._stop_recording()
            except Exception as e:
                print(f"\n[录像] 写帧失败: {e}")
                self._stop_recording()

    def trigger(self, alert_type: str, alert_msg: str, study_state: dict):
        """触发录像：把缓冲区写入 + 继续录 post_seconds"""
        if self.recording:
            return None  # 已经在录了

        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        filename = f"{ts}_{alert_type}.mp4"
        path = os.path.join(self.output_dir, filename)

        h, w = self.buffer[0].shape[:2] if self.buffer else (480, 848)
        # 用 mp4v（兼容性最好），浏览器录播 HEVC 需要转码
        # 优先尝试 H.264 (avc1)，fallback 到 mp4v
        fourcc = cv2.VideoWriter_fourcc(*'avc1')
        writer = cv2.VideoWriter(path, fourcc, self.fps, (w, h))
        if not writer.isOpened():
            # cv2 的 mp4v 写的也是 H.264 兼容帧，浏览器通常能播
            fourcc = cv2.VideoWriter_fourcc(*'mp4v')
            writer = cv2.VideoWriter(path, fourcc, self.fps, (w, h))
        self._path = path
        self._writer = writer

        # 先写缓冲区
        for f in self.buffer:
            writer.write(f)

        self.current_writer = writer
        self.current_path = path
        self.current_metadata = {
            "filename": filename,
            "path": path,
            "alert_type": alert_type,
            "alert_msg": alert_msg,
            "triggered_at": datetime.now().isoformat(),
            "pre_seconds": self.pre_seconds,
            "post_seconds": self.post_seconds,
            "study_state": study_state,
        }
        self.recording = True
        self.post_frames_remaining = self.post_seconds * self.fps
        return self.current_metadata

    def _stop_recording(self):
        if self.current_writer:
            self.current_writer.release()
            self.current_writer = None
        self.recording = False
        return self.current_metadata


def compute_eye_aspect_ratio(eye_landmarks: List) -> float:
    """
    计算眼睛宽高比 (EAR)
    eye_landmarks: 6个点 [p1, p2, p3, p4, p5, p6]
      p1, p4: 眼角左右
      p2, p6: 上下眼睑
      p3, p5: 上下眼睑中间

    EAR = (|p2-p6| + |p3-p5|) / (2 * |p1-p4|)
    睁眼 ≈ 0.3, 闭眼 ≈ 0.05
    """
    if len(eye_landmarks) < 6:
        return 0.3

    p1, p2, p3, p4, p5, p6 = eye_landmarks[:6]

    def dist(a, b):
        return math.hypot(a.x - b.x, a.y - b.y)

    vertical = (dist(p2, p6) + dist(p3, p5)) / 2.0
    horizontal = dist(p1, p4) + 1e-6

    return vertical / horizontal


def compute_head_yaw(face_landmarks) -> float:
    """
    估算头部左右偏转角度
    用左右眼内角的水平距离来判断
    返回值：正数 = 向右转，负数 = 向左转
    """
    # MediaPipe Face Landmarker 关键点索引
    # 33 = 右眼内角，263 = 左眼内角
    # 1 = 鼻尖，10 = 额头中点
    try:
        right_eye_inner = face_landmarks[33]
        left_eye_inner = face_landmarks[263]
        nose_tip = face_landmarks[1]
        forehead = face_landmarks[10]

        # 双眼距离作为参考
        eye_dist = math.hypot(right_eye_inner.x - left_eye_inner.x,
                             right_eye_inner.y - left_eye_inner.y) + 1e-6

        # 鼻尖相对两眼中心的水平偏移
        eye_center_x = (right_eye_inner.x + left_eye_inner.x) / 2
        nose_offset = nose_tip.x - eye_center_x

        return nose_offset / eye_dist
    except (IndexError, AttributeError):
        return 0.0


def compute_head_pitch(face_landmarks) -> float:
    """
    估算头部俯仰角（低头/抬头）
    用额头到下巴的距离与左右脸颊宽度的比值
    """
    try:
        # 10=额头, 152=下巴, 234=左脸颊, 454=右脸颊
        forehead = face_landmarks[10]
        chin = face_landmarks[152]
        left_cheek = face_landmarks[234]
        right_cheek = face_landmarks[454]

        face_height = math.hypot(forehead.x - chin.x, forehead.y - chin.y) + 1e-6
        face_width = math.hypot(left_cheek.x - right_cheek.x,
                                left_cheek.y - right_cheek.y) + 1e-6

        # 低头时这个比值会显著降低
        ratio = face_height / face_width
        return ratio
    except (IndexError, AttributeError):
        return 1.0  # 默认正常


# ─── 数据结构 ─────────────────────────────────────────
@dataclass
class StudyState:
    status: str
    confidence: float
    head_y: float
    body_visible: bool
    left_hand_y: float
    right_hand_y: float
    head_yaw: float               # 头部左右偏转
    head_pitch_ratio: float       # 头部俯仰比值
    eye_ear: float                # 眼睛开合度
    eyes_closed: bool             # 是否闭眼
    movement_level: float         # 整体运动量
    hand_movement: float          # 手部运动量（写字/翻书）
    blink_rate: int               # 眨眼频率（次/分钟）
    timestamp: str
    duration_seconds: int
    debug_info: dict = field(default_factory=dict)


# ─── 多维度分析器 ──────────────────────────────────────
class BehaviorAnalyzer:
    """综合姿态 + 面部 + 运动量的多维度分析器"""

    def __init__(self):
        download_if_missing(POSE_MODEL_URL, POSE_MODEL_PATH)
        download_if_missing(FACE_MODEL_URL, FACE_MODEL_PATH)

        # Pose 检测器
        pose_options = mp.tasks.vision.PoseLandmarkerOptions(
            base_options=mp.tasks.BaseOptions(model_asset_path=POSE_MODEL_PATH),
            running_mode=mp.tasks.vision.RunningMode.VIDEO,
            num_poses=1,
            min_pose_detection_confidence=0.5,
            min_pose_presence_confidence=0.5,
            min_tracking_confidence=0.5,
        )
        self.pose_landmarker = mp.tasks.vision.PoseLandmarker.create_from_options(pose_options)

        # Face 检测器（用于眼睛和头部朝向）
        face_options = mp.tasks.vision.FaceLandmarkerOptions(
            base_options=mp.tasks.BaseOptions(model_asset_path=FACE_MODEL_PATH),
            running_mode=mp.tasks.vision.RunningMode.VIDEO,
            num_faces=1,
            min_face_detection_confidence=0.5,
            min_face_presence_confidence=0.5,
            min_tracking_confidence=0.5,
        )
        self.face_landmarker = mp.tasks.vision.FaceLandmarker.create_from_options(face_options)

        # 状态追踪
        self.prev_pose_landmarks = None
        self.prev_face_landmarks = None
        self.state_start_time = time.time()
        self.current_status = "init"

        # 滑动窗口：运动量历史
        self.movement_history = deque(maxlen=30)  # 最近60秒（每2秒一个）

        # 眼部追踪
        self.eye_history = deque(maxlen=30)  # 最近60秒的 EAR 值
        self.last_blink_time = time.time()
        self.blink_count_window = 0  # 60秒内的眨眼次数
        self.eyes_closed_start = None  # 闭眼起始时间
        self.baseline_ear = 0.35      # 睁眼时 EAR 基线（动态校准）
        self.pose_idle_counter = 0    # 姿态稳定帧计数
        self.ear_stable_counter = 0   # EAR 持续闭眼帧计数

    def analyze(self, frame, timestamp_ms: int) -> StudyState:
        """综合分析一帧"""
        h, w = frame.shape[:2]
        rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
        mp_image = mp.Image(image_format=mp.ImageFormat.SRGB, data=rgb)

        # Pose 检测
        pose_results = self.pose_landmarker.detect_for_video(mp_image, timestamp_ms)

        # Face 检测
        face_results = self.face_landmarker.detect_for_video(mp_image, timestamp_ms)

        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        duration = int(time.time() - self.state_start_time)

        # 默认值（人不在画面里）
        head_y = 0.5
        body_visible = False
        left_hand_y = right_hand_y = 0.5
        head_yaw = 0.0
        head_pitch_ratio = 1.0
        eye_ear = 0.3
        eyes_closed = False
        movement = 0.0
        hand_movement = 0.0  # 手部单独运动量

        # ── 1. 姿态分析 ──
        if pose_results.pose_landmarks:
            lm = pose_results.pose_landmarks[0]
            nose_y = lm[0].y
            shoulder_y = (lm[11].y + lm[12].y) / 2
            left_wrist_y = lm[15].y
            right_wrist_y = lm[16].y

            head_y = nose_y
            body_visible = 0.05 < shoulder_y < 0.95
            left_hand_y = left_wrist_y
            right_hand_y = right_wrist_y

            # 整体运动量
            if self.prev_pose_landmarks:
                for i in [0, 11, 12, 15, 16]:
                    dx = lm[i].x - self.prev_pose_landmarks[i].x
                    dy = lm[i].y - self.prev_pose_landmarks[i].y
                    movement += math.hypot(dx, dy)
                movement /= 5
                # 手部运动量（双手腕平均）— 用来识别"在动笔/翻书"
                lw_dx = lm[15].x - self.prev_pose_landmarks[15].x
                lw_dy = lm[15].y - self.prev_pose_landmarks[15].y
                rw_dx = lm[16].x - self.prev_pose_landmarks[16].x
                rw_dy = lm[16].y - self.prev_pose_landmarks[16].y
                hand_movement = (math.hypot(lw_dx, lw_dy) + math.hypot(rw_dx, rw_dy)) / 2

            self.prev_pose_landmarks = lm

        # ── 2. 面部分析（眼睛 + 头部朝向）──
        eyes_closed_now = False
        if face_results.face_landmarks:
            face_lm = face_results.face_landmarks[0]

            # 左眼 EAR（关键点 33, 160, 158, 133, 153, 144）
            left_eye_pts = [face_lm[i] for i in [33, 160, 158, 133, 153, 144]]
            left_ear = compute_eye_aspect_ratio(left_eye_pts)

            # 右眼 EAR（关键点 362, 385, 387, 263, 373, 380）
            right_eye_pts = [face_lm[i] for i in [362, 385, 387, 263, 373, 380]]
            right_ear = compute_eye_aspect_ratio(right_eye_pts)

            eye_ear = (left_ear + right_ear) / 2

            # ── 动态校准基线 ──
            # 检测到 Face 的前 10 帧收集基线 EAR
            if len(self.eye_history) < 10 and eye_ear > 0.25:
                self.baseline_ear = eye_ear if eye_ear < self.baseline_ear else self.baseline_ear * 0.7 + eye_ear * 0.3

            # 闭眼判定：相对于个人基线的 EAR 下降超过 50%
            relative_drop = eye_ear / max(self.baseline_ear, 0.01)
            eyes_closed_now = relative_drop < EYE_CLOSED_THRESHOLD / 0.22  # 0.15/0.22 ≈ 0.68

            # ── 稳定帧计数：避免单帧误判 ──
            if eyes_closed_now:
                self.ear_stable_counter += 1
            else:
                self.ear_stable_counter = max(0, self.ear_stable_counter - 2)

            eyes_closed_now = self.ear_stable_counter >= 3  # 连续 3 帧才算

            # 头部朝向
            head_yaw = compute_head_yaw(face_lm)
            head_pitch_ratio = compute_head_pitch(face_lm)

        # ── 3. 闭眼状态追踪 ──
        if eyes_closed_now:
            if self.eyes_closed_start is None:
                self.eyes_closed_start = time.time()
            eyes_closed_duration = time.time() - self.eyes_closed_start
            eyes_closed = eyes_closed_duration > 1.0  # 持续闭眼 > 1 秒才算
        else:
            # 检测到眨眼（从闭到开）
            if self.eyes_closed_start is not None:
                closed_duration = time.time() - self.eyes_closed_start
                if 0.05 < closed_duration < 0.5:  # 正常眨眼时长
                    self.blink_count_window += 1
                self.eyes_closed_start = None

        # ── 4. 眨眼频率（最近60秒）──
        self.eye_history.append(eye_ear)
        blink_rate = self.blink_count_window  # 每分钟的眨眼次数（窗口正好60秒）

        # ── 5. 运动量历史 ──
        self.movement_history.append(movement)
        avg_movement = sum(self.movement_history) / max(len(self.movement_history), 1)

        # ── 6. 综合状态判断 ──
        status, confidence, debug_info = self._judge_status(
            body_visible=body_visible,
            head_y=head_y,
            hands_below=left_hand_y > HAND_BELOW_DESK and right_wrist_y > HAND_BELOW_DESK,
            head_yaw=head_yaw,
            head_pitch_ratio=head_pitch_ratio,
            eye_ear=eye_ear,
            eyes_closed=eyes_closed,
            avg_movement=avg_movement,
            hand_movement=hand_movement,
            blink_rate=self.blink_count_window,
        )

        self._update_state(status)

        return StudyState(
            status=status,
            confidence=confidence,
            head_y=head_y,
            body_visible=body_visible,
            left_hand_y=left_hand_y,
            right_hand_y=right_hand_y,
            head_yaw=head_yaw,
            head_pitch_ratio=head_pitch_ratio,
            eye_ear=eye_ear,
            eyes_closed=eyes_closed,
            movement_level=avg_movement,
            hand_movement=hand_movement,
            blink_rate=self.blink_count_window,
            timestamp=now,
            duration_seconds=int(time.time() - self.state_start_time),
            debug_info=debug_info,
        )

    def _judge_status(self, body_visible, head_y, hands_below,
                      head_yaw, head_pitch_ratio, eye_ear,
                      eyes_closed, avg_movement, hand_movement=0.0,
                      blink_rate=0):
        """综合判断状态
        优先级：离开 > 看别处 > 玩手机 > 闭眼(分情况) > 趴着 > 发呆 > 学习中
        关键修复：闭眼时如果有手部动作或正常眨眼 → 不是打瞌睡（可能是眨眼/思考）
        """
        debug = {
            "head_y": round(head_y, 3),
            "head_yaw": round(head_yaw, 3),
            "head_pitch": round(head_pitch_ratio, 3),
            "eye_ear": round(eye_ear, 3),
            "movement": round(avg_movement, 4),
            "hand_movement": round(hand_movement, 4),
            "blink_rate": blink_rate,
        }

        # 优先级 1: 离开
        if not body_visible:
            return "away", 0.90, {**debug, "reason": "no body"}

        # 优先级 2: 看别处（转头幅度大）
        if abs(head_yaw) > HEAD_YAW_THRESHOLD:
            return "look_away", 0.80, {**debug, "reason": "head turned"}

        # 优先级 3: 玩手机（手在桌下 + 头略低）
        if hands_below and head_y > 0.50:
            return "phone", 0.75, {**debug, "reason": "hands+head"}

        # 优先级 4: 闭眼 — 但要排除"眨眼中"和"在动笔"
        if eyes_closed:
            # 排除条件 0: 刚刚检测到人脸，还在校准基线
            if len(self.eye_history) < 5:
                return "studying", 0.60, {**debug, "reason": "calibrating"}
            # 排除条件 1: 手部在动（说明在写字/翻书）
            if hand_movement > 0.03:
                return "studying", 0.75, {**debug, "reason": "eyes closed but hand active"}
            # 排除条件 2: 眨眼频率正常（>2 次/分钟 = 正常眨眼，不是闭眼）
            if blink_rate >= 2:
                return "studying", 0.70, {**debug, "reason": "normal blinking"}
            # 排除条件 3: 整体运动量大
            if avg_movement > MOVEMENT_THRESHOLD * 0.7:
                return "studying", 0.70, {**debug, "reason": "eyes closed but moving"}
            # 排除条件 4: 头部在动（说明是正常低头思考）
            if hand_movement < 0.03 and avg_movement > 0.01:
                return "studying", 0.65, {**debug, "reason": "thinking posture"}
            # 否则：打瞌睡
            return "drowsy", 0.90, {**debug, "reason": "eyes closed (drowsy)"}

        # 优先级 5: 趴着（头低 + 身体可见 + 没动）
        if head_y > HEAD_DOWN_THRESHOLD and avg_movement < MOVEMENT_THRESHOLD / 2:
            return "sleeping", 0.85, {**debug, "reason": "head down + still"}

        # 优先级 6: 低头但有动作 → 可能是玩手机或看桌面
        if head_y > HEAD_DOWN_THRESHOLD:
            return "looking_down", 0.70, {**debug, "reason": "head down but moving"}

        # 优先级 7: 长时间不动 → 发呆
        if avg_movement < MOVEMENT_THRESHOLD / 3 and len(self.movement_history) > 10:
            return "idle", 0.60, {**debug, "reason": "no movement"}

        # 优先级 8: 在动 → 动来动去
        if avg_movement > MOVEMENT_THRESHOLD:
            return "moving", 0.65, {**debug, "reason": "moving"}

        # 默认: 学习中
        return "studying", 0.85, {**debug, "reason": "normal"}

    def _update_state(self, new_status: str):
        if new_status != self.current_status:
            self.current_status = new_status
            self.state_start_time = time.time()
            # 状态变化时清空部分历史
            if new_status in ["studying", "away"]:
                self.movement_history.clear()

    def should_alert(self, state: StudyState) -> Optional[str]:
        """根据状态和持续时间判断是否需要提醒"""
        d = state.duration_seconds
        s = state.status

        if s == "drowsy" and d > DROWSY_ALERT_AFTER:
            return "弟弟，你眼睛闭上了，要打瞌睡了吗？赶紧起来活动一下。"
        elif s == "sleeping" and d > SLEEP_ALERT_AFTER:
            return "弟弟，你趴着睡觉了，起来清醒一下再继续学习吧。"
        elif s == "look_away" and d > LOOK_AWAY_ALERT_AFTER:
            return "弟弟，你的注意力不在学习上，专心一点。"
        elif s == "looking_down" and d > LOOK_AWAY_ALERT_AFTER:
            return "弟弟，你在看什么呢？请抬起头专心学习。"
        elif s == "phone" and d > PHONE_ALERT_AFTER:
            return "弟弟，现在是学习时间，不要玩手机了。"
        elif s == "idle" and d > IDLE_ALERT_AFTER:
            return "弟弟，你发呆了很久了，要继续学习哦。"
        elif s == "moving" and d > 120:
            return "弟弟，坐好专心学习。"
        elif s == "away" and d > AWAY_ALERT_AFTER:
            return "弟弟，你离开座位太久了，快回来。"
        return None


# ─── 语音提醒器（保持不变）────────────────────────────
class VoiceAlerter:
    def __init__(self):
        self.enabled = VOICE_ENABLED
        self.voice = VOICE_NAME
        self.last_alert_time = {}
        self._mixer_initialized = False
        self._tts_in_progress = False

    def _init_mixer(self):
        if not self._mixer_initialized:
            try:
                import pygame
                pygame.mixer.init()
                self._mixer_initialized = True
            except Exception as e:
                print(f"⚠️ pygame mixer 初始化失败: {e}")

    async def _speak_async(self, text: str):
        import edge_tts
        import pygame
        import tempfile

        try:
            self._init_mixer()
            communicate = edge_tts.Communicate(text, self.voice)
            tmp_path = tempfile.NamedTemporaryFile(suffix=".mp3", delete=False).name
            await communicate.save(tmp_path)

            pygame.mixer.music.load(tmp_path)
            pygame.mixer.music.play()
            while pygame.mixer.music.get_busy():
                pygame.time.wait(100)
            pygame.mixer.music.unload()
            try:
                os.unlink(tmp_path)
            except:
                pass
        except Exception as e:
            print(f"⚠️ TTS 错误: {e}")
        finally:
            self._tts_in_progress = False

    def speak(self, text: str, status: str):
        if not self.enabled or self._tts_in_progress:
            return

        now = time.time()
        last = self.last_alert_time.get(status, 0)
        if now - last < TTS_COOLDOWN:
            return

        self.last_alert_time[status] = now
        self._tts_in_progress = True

        import asyncio
        import threading

        def runner():
            try:
                asyncio.run(self._speak_async(text))
            except:
                self._tts_in_progress = False

        threading.Thread(target=runner, daemon=True).start()


# ─── 日志和可视化 ─────────────────────────────────────
def log_state(state: StudyState, log_file: str):
    os.makedirs(os.path.dirname(log_file), exist_ok=True)
    with open(log_file, "a", encoding="utf-8") as f:
        d = asdict(state)
        f.write(json.dumps(d, ensure_ascii=False) + "\n")


def draw_debug(frame, state: StudyState, ear_left=0.3, ear_right=0.3):
    """可视化调试信息"""
    h, w = frame.shape[:2]

    status_info = {
        "studying": ("✅ 学习中", (0, 255, 0)),
        "looking_down": ("👀 看桌面", (0, 200, 255)),
        "look_away": ("🙄 看别处", (255, 200, 0)),
        "drowsy": ("😴 打瞌睡/闭眼", (0, 0, 255)),
        "sleeping": ("😪 趴着睡觉", (0, 0, 200)),
        "phone": ("📱 玩手机", (0, 140, 255)),
        "idle": ("😐 发呆", (200, 200, 0)),
        "moving": ("🪑 动来动去", (255, 255, 0)),
        "away": ("🚶 离开", (128, 128, 128)),
    }
    text, color = status_info.get(state.status, (state.status, (255, 255, 255)))

    # 主状态
    cv2.putText(frame, text, (20, 40), cv2.FONT_HERSHEY_SIMPLEX, 1.2, color, 3)

    # 持续时间
    dur = f"持续: {state.duration_seconds}s"
    cv2.putText(frame, dur, (20, 75), cv2.FONT_HERSHEY_SIMPLEX, 0.7, (255, 255, 255), 2)

    # 详细指标
    y0 = 110
    metrics = [
        f"眼睛 EAR: {state.eye_ear:.3f} {'闭眼!' if state.eyes_closed else ''}",
        f"头部朝向 yaw: {state.head_yaw:+.3f}",
        f"头部俯仰: {state.head_pitch_ratio:.3f}",
        f"运动量: {state.movement_level:.4f}",
        f"置信度: {state.confidence:.0%}",
    ]
    for i, m in enumerate(metrics):
        cv2.putText(frame, m, (20, y0 + i * 25), cv2.FONT_HERSHEY_SIMPLEX, 0.55, (200, 200, 200), 1)

    # 调试原因
    if state.debug_info.get("reason"):
        cv2.putText(frame, f"[{state.debug_info['reason']}]", (20, h - 20),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.5, (150, 150, 150), 1)

    return frame


# ─── 主循环 ───────────────────────────────────────────
def log_alert(alert_record: dict, log_file: str):
    """追加告警事件到 alerts.jsonl"""
    os.makedirs(os.path.dirname(log_file), exist_ok=True)
    with open(log_file, "a", encoding="utf-8") as f:
        f.write(json.dumps(alert_record, ensure_ascii=False) + "\n")


def main():
    parser = argparse.ArgumentParser(description="学习状态监控 v2")
    parser.add_argument("--rtsp", default=DEFAULT_RTSP, help="RTSP 流地址")
    parser.add_argument("--debug", action="store_true", help="显示可视化调试窗口")
    parser.add_argument("--no-display", action="store_true", help="不显示窗口（后台）")
    parser.add_argument("--log", default=os.path.join(LOG_DIR, "states.jsonl"))
    parser.add_argument("--alert-log", default=os.path.join(LOG_DIR, "alerts.jsonl"))
    parser.add_argument("--human-approve", action="store_true", default=True,
                        help="人工审核模式：告警先入队，等 UI 确认才播报")
    args = parser.parse_args()

    print(f"📡 连接摄像头: {args.rtsp}")
    cap = cv2.VideoCapture(args.rtsp)
    if not cap.isOpened():
        print("❌ 无法连接摄像头！请检查 go2rtc 是否在运行 (http://localhost:1984)")
        return
    print("✅ 摄像头连接成功！")
    print(f"📝 日志: {args.log}")
    print(f"🔊 语音提醒: {'开启' if VOICE_ENABLED else '关闭'}")
    print(f"👤 审核模式: {'开启（告警需 UI 确认）' if args.human_approve else '关闭（自动播报）'}")
    print("🔄 开始监控... (Ctrl+C 停止)\n")

    analyzer = BehaviorAnalyzer()
    voice_alerter = VoiceAlerter()
    recorder = VideoRecorder(args.rtsp, pre_seconds=15, post_seconds=15, fps=10)
    audio_monitor = AudioMonitor(args.rtsp)
    audio_monitor.start()
    print(f"🎤 音频监听: 已启动（人声检测）")

    frame_count = 0
    last_log_time = 0
    last_alert_status = None
    last_alert_trigger_time = 0
    last_ptz_scan_time = 0
    start_time = time.time()
    ptz_scan_step = 0  # 当前扫描步骤
    state = StudyState(
        status="init", confidence=0, head_y=0.5, body_visible=False,
        left_hand_y=0, right_hand_y=0, head_yaw=0, head_pitch_ratio=1.0,
        eye_ear=0.3, eyes_closed=False, movement_level=0,
        hand_movement=0.0, blink_rate=0,
        timestamp="", duration_seconds=0, debug_info={}
    )

    try:
        while True:
            ret, frame = cap.read()
            if not ret:
                print("⚠️ 读帧失败，重连...")
                cap.release()
                time.sleep(3)
                cap = cv2.VideoCapture(args.rtsp)
                continue

            frame_count += 1
            now = time.time()

            # 每帧都 push 给录像器（环形缓冲）
            recorder.push_frame(frame)

            if now - last_log_time >= CHECK_INTERVAL:
                timestamp_ms = int((now - start_time) * 1000)
                state = analyzer.analyze(frame, timestamp_ms)

                # ── 音频检测修正 ──
                if audio_monitor.is_speaking_now and state.status in ["look_away", "looking_down", "idle"]:
                    # 人在读书/讨论，但不是 face/pause 级别的统计
                    state.status = "studying"
                    state.confidence = max(state.confidence, 0.75)
                    state.debug_info["audio_fix"] = True

                log_state(state, args.log)

                eye_str = "闭眼" if state.eyes_closed else f"EAR={state.eye_ear:.2f}"
                speaking = "🔊" if audio_monitor.is_speaking_now else "🔇"
                print(f"[{state.timestamp}] {state.status:12s} 持续:{state.duration_seconds:4d}s "
                      f"眼:{eye_str} 头:{state.head_yaw:+.2f} 手:{state.hand_movement:.3f} {speaking}")

                alert = analyzer.should_alert(state)
                if alert:
                    # 同类告警 5 秒冷却（避免刷屏）
                    if (state.status != last_alert_status or
                            time.time() - last_alert_trigger_time > 5):
                        print(f"  🚨 {alert}")
                        last_alert_status = state.status
                        last_alert_trigger_time = time.time()

                        # 触发录像
                        study_state_snapshot = asdict(state)
                        recording = recorder.trigger(state.status, alert, study_state_snapshot)

                        # 写告警记录
                        alert_record = {
                            "id": f"alert_{int(time.time() * 1000)}",
                            "timestamp": state.timestamp,
                            "status": state.status,
                            "alert_msg": alert,
                            "duration_seconds": state.duration_seconds,
                            "study_state": study_state_snapshot,
                            "recording": recording,
                            "status_": "pending",   # pending / confirmed / dismissed
                            "created_at": datetime.now().isoformat(),
                        }
                        log_alert(alert_record, args.alert_log)

                        # 审核模式：先把告警入库，等 UI 确认才播语音
                        if args.human_approve:
                            print(f"     ⏳ 待审核（去 Web UI 确认或忽略）")
                        else:
                            voice_alerter.speak(alert, state.status)

                # ── 自动云台扫描：低置信度持续30秒不改善 → 轻轻转一下看 ──
                if PTZ_AUTO_SCAN_ENABLED:
                    if state.confidence < PTZ_LOW_CONFIDENCE_THRESHOLD:
                        if time.time() - last_ptz_scan_time > PTZ_SCAN_INTERVAL and not recorder.recording:
                            direction, angle = PTZ_SCAN_SEQUENCE[ptz_scan_step % len(PTZ_SCAN_SEQUENCE)]
                            print(f"     📹 低置信度({state.confidence:.0%})，自动转动摄像头: {direction} {angle}°")
                            try:
                                import threading as th
                                def _ptz():
                                    from urllib.request import Request, urlopen
                                    import json as _json
                                    req = Request("http://localhost:8765/api/camera/ptz",
                                                data=_json.dumps({"direction": direction, "angle": angle}).encode(),
                                                headers={"Content-Type": "application/json"},
                                                method="POST")
                                    urlopen(req, timeout=3)
                                th.Thread(target=_ptz, daemon=True).start()
                            except Exception:
                                pass
                            ptz_scan_step += 1
                            last_ptz_scan_time = time.time()

                last_log_time = now

            if args.debug and not args.no_display:
                debug_frame = draw_debug(frame, state)
                cv2.imshow("Study Monitor v2", debug_frame)
                if cv2.waitKey(1) & 0xFF == ord("q"):
                    break
            elif not args.no_display:
                cv2.imshow("Study Monitor v2 - q to quit", frame)
                if cv2.waitKey(1) & 0xFF == ord("q"):
                    break

    except KeyboardInterrupt:
        print("\n⏹ 监控已停止")
    finally:
        audio_monitor.stop()
        cap.release()
        cv2.destroyAllWindows()
        print(f"📊 共分析 {frame_count} 帧")


if __name__ == "__main__":
    main()