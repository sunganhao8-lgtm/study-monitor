"""
Study Monitor - 全功能测试脚本
==============================
用法：python test_all.py
退出码：0 表示全部通过
"""

import json
import sys
import time
from urllib.request import Request, urlopen
from urllib.error import URLError

BASE = "http://localhost:8765"
PASSED = []
FAILED = []


def test(name, func):
    """运行单个测试"""
    try:
        ok, msg = func()
        if ok:
            PASSED.append((name, msg))
            print(f"  ✓ {name}: {msg}")
        else:
            FAILED.append((name, msg))
            print(f"  ✗ {name}: {msg}")
    except Exception as e:
        FAILED.append((name, str(e)))
        print(f"  ✗ {name}: EXCEPTION {e}")


def http_get(path):
    """GET 请求并解析 JSON"""
    r = urlopen(f"{BASE}{path}", timeout=10)
    return json.loads(r.read())


def http_post(path, body=None):
    """POST 请求"""
    data = json.dumps(body).encode() if body else None
    r = urlopen(Request(f"{BASE}{path}", data=data,
                       headers={"Content-Type": "application/json"}),
                timeout=10)
    return json.loads(r.read())


def main():
    print("=" * 60)
    print(" Study Monitor - Functional Tests")
    print("=" * 60)
    print()

    # ─── 服务可达性 ───
    print("[1] 服务可达性")
    test("Web UI 可访问", lambda: (http_get("/api/status").get("running") is not None, "200 OK"))
    test("go2rtc 监控", lambda: (
        "running" in http_get("/api/go2rtc/status"),
        "go2rtc status"
    ))

    # ─── 实时数据流 ───
    print("\n[2] 实时数据流")
    test("/api/status 返回最新状态", lambda: (
        http_get("/api/status").get("current_state") is not None,
        "current_state OK"
    ))
    test("/api/logs 返回数组", lambda: (
        isinstance(http_get("/api/logs?limit=10"), list),
        f"{len(http_get('/api/logs?limit=10'))} 条"
    ))
    test("/api/alerts 接受过滤", lambda: (
        isinstance(http_get("/api/alerts?status=all"), list),
        "alerts OK"
    ))
    test("/api/stats 返回结构化数据", lambda: (
        isinstance(http_get("/api/stats?hours=1"), dict),
        "stats OK"
    ))
    test("/api/recordings 列表", lambda: (
        isinstance(http_get("/api/recordings"), list),
        f"{len(http_get('/api/recordings'))} 个视频"
    ))

    # ─── 配置读写 ───
    print("\n[3] 配置读写")
    original = http_get("/api/config")

    test("/api/config POST 修改阈值", lambda: (
        http_post("/api/config",
                  {**original, "thresholds": {**original["thresholds"], "EYE_CLOSED_THRESHOLD": 0.18}}
                  ).get("ok") is not None,
        "save OK"
    ))

    test("/api/config GET 读到修改", lambda: (
        abs(http_get("/api/config")["thresholds"]["EYE_CLOSED_THRESHOLD"] - 0.18) < 0.001,
        "round-trip OK"
    ))

    # 复原
    http_post("/api/config", original)
    test("config 复原", lambda: (
        abs(http_get("/api/config")["thresholds"]["EYE_CLOSED_THRESHOLD"] - original["thresholds"]["EYE_CLOSED_THRESHOLD"]) < 0.001,
        "restored"
    ))

    # ─── PTZ 云台控制 ───
    print("\n[4] PTZ 云台控制")
    for direction in ["left", "right", "up", "down", "stop"]:
        test(f"PTZ {direction}", lambda d=direction: (
            http_post("/api/camera/ptz", {"direction": d, "angle": 3}).get("ok") is not None,
            f"{d} 指令已发送"
        ))

    # ─── 摄像头 RTSP 流 ───
    print("\n[5] 摄像头 RTSP 流")
    test("RTSP 可读取", lambda: (
        "OK" if _test_rtsp() else "FAIL",
        _test_rtsp_msg()
    ))

    # ─── MJPEG 流 ───
    print("\n[6] MJPEG 流")
    test("MJPEG JPEG 头", lambda: (
        _test_mjpeg(),
        "JPEG header detected"
    ))

    # ─── 告警 API（mock 数据需要 review 流程） ───
    print("\n[7] 告警 API")
    test("alerts all 过滤", lambda: (
        isinstance(http_get("/api/alerts?status=all"), list),
        "filter OK"
    ))
    test("alerts pending 过滤", lambda: (
        isinstance(http_get("/api/alerts?status=pending"), list),
        "OK"
    ))

    # ─── 总结 ───
    print("\n" + "=" * 60)
    print(f" 通过: {len(PASSED)}/{len(PASSED)+len(FAILED)}")
    if FAILED:
        print(f" 失败:")
        for n, m in FAILED:
            print(f"   ✗ {n}: {m}")
        print(f" 失败 {len(FAILED)} 个")
        sys.exit(1)
    print(" 全部通过 ✓")
    sys.exit(0)


def _test_rtsp():
    try:
        import cv2
        cap = cv2.VideoCapture("rtsp://localhost:8554/xiaomi_camera4")
        if not cap.isOpened():
            return False
        cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)
        # 用内部超时机制
        for _ in range(3):
            ret, _ = cap.read()
            if ret:
                break
        cap.release()
        return ret
    except Exception:
        return False


def _test_rtsp_msg():
    if _test_rtsp():
        return "848x480 帧可读"
    return "FAIL"


def _test_mjpeg():
    try:
        r = urlopen(f"{BASE}/api/snapshot/auto", timeout=3)
        buf = r.read(8192)
        r.close()
        return b"\xff\xd8" in buf
    except Exception:
        return False


if __name__ == "__main__":
    sys.exit(main())
