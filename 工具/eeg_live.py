# -*- coding: utf-8 -*-
"""
KSEEG102 实时注意力得分（BLE 版）
================================
协议（从抓包反推）：
  69 字节包 = 序号(1) + 06 04 64(3) + 64 字节数据 + 0x01(1)
  64 字节 = 32 个 16 位大端值，偶位=A 通道，奇位=B 通道
  每包 16 点/通道；包头间隔约 29–32 ms → 采样率约 500 Hz

处理链：
  带通 1–45 Hz + 50 Hz 陷波 → 4 秒窗 Welch → θ/α → 差分锚定自适应基线 → 0–100 分

用法（在你自己的 PowerShell 里）：
  pip install bleak scipy numpy -i https://pypi.tuna.tsinghua.edu.cn/simple
  python eeg_live.py
  python eeg_live.py --csv my_session.csv     # 同时存原始数据
"""

import argparse
import asyncio
import statistics as st
import time
from collections import deque

import numpy as np
from scipy import signal as sps

try:
    from bleak import BleakClient, BleakScanner
except ImportError:
    print("缺少 bleak：pip install bleak -i https://pypi.tuna.tsinghua.edu.cn/simple")
    raise SystemExit(1)

DATA_UUID = "8653000b-43e6-47b7-9cb0-5fc21d4ae340"
DEVICE_HINT = "KSEEG"
FS = 500.0                 # 采样率（按 500 Hz 处理）

# 处理参数
WIN_S = 4.0                # 分析窗
HOP_S = 0.5                # 更新间隔
WARMUP_S = 60.0            # 启动期：只积累，不评分
BASE_WIN_S = 300.0         # 自适应基线窗口
BASE_UPDATE_S = 30.0       # 基线更新间隔
Q = 20                     # 低分位
RATE_LIMIT = 0.1           # 单次限速（× s）

BANDS = {"theta": (4, 8), "alpha": (8, 13), "beta": (13, 30)}


class Pipeline:
    def __init__(self):
        self.sos = sps.butter(4, [1, 45], btype="bandpass", fs=FS, output="sos")
        self.zi = np.zeros((self.sos.shape[0], 2, 2))
        self.bn, self.an = sps.iirnotch(50, 30, fs=FS)
        self.zf = np.zeros((2, max(len(self.bn), len(self.an)) - 1))

        self.buf = [deque(maxlen=int(FS * (WIN_S + 2))) for _ in range(2)]
        self.raw_recent = [deque(maxlen=int(FS)) for _ in range(2)]

        self.r_hist = deque(maxlen=200000)      # (时间, r)
        self.art_hist = deque(maxlen=200000)

        self.t0 = None
        self.m0 = None
        self.s0 = None
        self.b0 = None
        self.m = None
        self.s = None
        self.last_base_update = 0.0
        self.last_score_t = 0.0
        self.score = None
        self.z = None
        self.n_pkt = 0
        self.n_bad = 0
        self.warm_vals = []

    # ---------- 数据进入 ----------
    def push(self, a_vals, b_vals, now):
        if self.t0 is None:
            self.t0 = now
        x = np.array([a_vals, b_vals], dtype=float)   # (2, n)
        # 陷波
        x, self.zf = sps.lfilter(self.bn, self.an, x, axis=1, zi=self.zf)
        # 带通（流式，保持状态）
        y, self.zi = sps.sosfilt(self.sos, x, axis=1, zi=self.zi)
        for ch in range(2):
            self.buf[ch].extend(y[ch])
            self.raw_recent[ch].extend(x[ch])
        self.n_pkt += 1

    # ---------- 伪迹判定（最近 1 秒）----------
    def is_artifact(self):
        for ch in range(2):
            seg = np.fromiter(self.raw_recent[ch], dtype=float)[-int(FS):]
            if len(seg) < FS // 2:
                continue
            med = np.median(seg)
            mad = np.median(np.abs(seg - med)) * 1.4826 or 1e-9
            if np.any(np.abs(seg - med) / mad >= 6):
                return True
        return False

    # ---------- 评分 ----------
    def score_once(self, now):
        # 缓冲不够就先返回（校准期也要算 r，用来建立基线）
        if len(self.buf[0]) < int(WIN_S * FS):
            return None

        seg = np.stack([np.fromiter(self.buf[ch], dtype=float)[-int(WIN_S * FS):] for ch in range(2)])
        f, pxx = sps.welch(seg, fs=FS, nperseg=int(WIN_S * FS), axis=1)
        p = pxx.mean(axis=0)
        bp = {}
        for name, (lo, hi) in BANDS.items():
            msk = (f >= lo) & (f <= hi)
            bp[name] = float(np.trapezoid(p[msk], f[msk]))
        if bp["alpha"] <= 1e-12:
            return None
        r = float(np.log(bp["theta"] / bp["alpha"]))

        art = self.is_artifact()

        # 校准期：收集 r 样本，够了就初始化基线
        if self.m0 is None:
            self.warm_vals.append(r)
            elapsed = now - self.t0
            if elapsed >= WARMUP_S and len(self.warm_vals) >= 10:
                arr = np.array(self.warm_vals)
                self.m0 = float(np.median(arr))
                self.s0 = float(np.median(np.abs(arr - self.m0)) * 1.4826) or 0.35
                self.b0 = float(np.percentile(arr, Q))
                self.m, self.s = self.m0, self.s0
                print(f"\n[校准完成] m0={self.m0:.3f}  s0={self.s0:.3f}  b0={self.b0:.3f}  "
                      f"（用了 {len(self.warm_vals)} 个窗）\n", flush=True)
            return None

        self.r_hist.append((now, r))
        self.art_hist.append((now, art))

        # 每 30 秒更新一次基线（差分锚定）
        if now - self.last_base_update >= BASE_UPDATE_S:
            cut = now - BASE_WIN_S
            vals = [v for (t, v), (_, a) in zip(self.r_hist, self.art_hist) if t >= cut and not a]
            if len(vals) >= 40:
                b_now = float(np.percentile(vals, Q))
                target = self.m0 + (b_now - self.b0)
                self.m += float(np.clip(target - self.m, -RATE_LIMIT * self.s, RATE_LIMIT * self.s))
            self.last_base_update = now

        self.z = (r - self.m) / max(self.s, 1e-9)
        self.score = 50 + 50 * np.tanh(self.z / 2.5)
        self.art = art
        return self.score

    def report(self, now, score):
        art = getattr(self, "art", False)
        flag = "伪迹" if art else "   "
        print(f"t={now - self.t0:6.1f}s  得分 {score:5.1f}  z={self.z:+.2f}  "
              f"m={self.m:.3f}  s={self.s:.3f}  包={self.n_pkt}  {flag}", flush=True)


async def find_device():
    print("扫描 BLE 设备（10 秒）…")
    devs = await BleakScanner.discover(timeout=10, return_adv=True)
    for addr, (d, adv) in devs.items():
        name = d.name or adv.local_name or ""
        if DEVICE_HINT.lower() in str(name).lower():
            print(f"找到 {name} @ {addr}")
            return addr
    # 退而求其次：看谁有目标特征
    print("没按名字找到，尝试连接所有设备找目标特征…")
    for addr in devs:
        try:
            async with BleakClient(addr, timeout=8) as cli:
                for s in cli.services:
                    for c in s.characteristics:
                        if c.uuid.lower().startswith(DATA_UUID[:8]):
                            return addr
        except Exception:
            continue
    return None


async def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--csv", default=None)
    ap.add_argument("--addr", default=None)
    ap.add_argument("--warmup", type=float, default=None, help="校准秒数，默认 60")
    args = ap.parse_args()

    global WARMUP_S
    if args.warmup:
        WARMUP_S = args.warmup

    addr = args.addr or await find_device()
    if not addr:
        print("没找到设备。确认它开机、没连手机。")
        return 1

    pipe = Pipeline()
    csv_f = open(args.csv, "w", encoding="utf-8") if args.csv else None
    if csv_f:
        csv_f.write("t,chA,chB\n")

    def on_data(_sender, data: bytearray):
        if len(data) != 69:
            pipe.n_bad += 1
            return
        body = bytes(data[4:-1])
        v = [int.from_bytes(body[i:i+2], "big") for i in range(0, 64, 2)]
        a = v[0::2]
        b = v[1::2]
        now = time.time()
        pipe.push(a, b, now)
        if csv_f:
            for i in range(len(a)):
                csv_f.write(f"{now:.3f},{a[i]},{b[i]}\n")

    print(f"连接 {addr} …")
    async with BleakClient(addr, timeout=20) as cli:
        print("已连接，订阅数据…")
        # 订阅所有可通知特征（与抓包脚本一致）；数据只从 69 字节包里取
        subs = 0
        for svc in cli.services:
            for ch in svc.characteristics:
                props = [x.lower() for x in ch.properties]
                if "notify" in props or "indicate" in props:
                    try:
                        await cli.start_notify(ch, on_data)
                        subs += 1
                        print(f"  订阅 {ch.uuid}")
                    except Exception as e:
                        print(f"  跳过 {ch.uuid}: {e}")
        print(f"共订阅 {subs} 个特征")
        print(f"开始采集。前 {WARMUP_S:.0f} 秒为校准期（请保持放松）…\n")

        t_start = time.time()
        while True:
            await asyncio.sleep(HOP_S)
            now = time.time()
            if now - t_start > WARMUP_S and now - pipe.last_score_t < HOP_S:
                continue
            s = pipe.score_once(now)
            if s is not None:
                pipe.last_score_t = now
                pipe.report(now, s)
            if now - t_start > 3600:
                break
    if csv_f:
        csv_f.close()
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(asyncio.run(main()))
    except KeyboardInterrupt:
        print("\n已停止")
