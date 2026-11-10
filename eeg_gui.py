# -*- coding: utf-8 -*-
"""
KSEEG102 实时脑电面板（桌面程序，仿厂商界面）
=============================================
左边：原始信号 / 频带能量 / 专注指数 / 压力与放松
右边：设备状态、实时指数、基线参数、按钮

协议：69 字节包 = 序号 + 06 04 64 + 64B 数据 + 01；16 位大端；偶=A 奇=B；约 500 Hz

用法：
    python eeg_gui.py                  # 连设备
    python eeg_gui.py --demo           # 无设备，用合成数据演示界面
    python eeg_gui.py --csv a.csv      # 同时保存原始数据
    python eeg_gui.py --warmup 30      # 校准 30 秒

打包成 exe（可选）：
    pip install pyinstaller -i https://pypi.tuna.tsinghua.edu.cn/simple
    pyinstaller --noconsole --onefile eeg_gui.py
"""

import argparse
import asyncio
import csv
from datetime import datetime
from pathlib import Path

# 声明 DPI 感知（必须在创建窗口前）：否则 Windows 缩放会让界面被放大、超出屏幕
try:
    import ctypes
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)
    except Exception:
        ctypes.windll.user32.SetProcessDPIAware()
except Exception:
    pass
import threading
import time
import tkinter as tk
from collections import deque

import numpy as np
from scipy import signal as sps

from eye_events import EventDetector
from score_udp import ScoreSender
from sart_task import DigitTask, Recording, clock
from tkinter import messagebox

DATA_UUID = "8653000b-43e6-47b7-9cb0-5fc21d4ae340"
DEVICE_HINT = "KSEEG"
FS = 500.0

BANDS = {"theta": (4, 8), "alpha": (8, 13), "beta": (13, 30)}

# 配色（深色，仿厂商界面）
# —— 最初那套深色配色 ——
BG = "#0d1b2a"          # 页面底色
PAGE = BG
PANEL = "#122b3f"       # 面板
CARD = PANEL
BORDER = "#1d3b52"
GRID = "#1d3b52"        # 网格
FG = "#c9d6e2"          # 主文字
MUTED = "#8aa4b8"       # 次要文字
ACCENT = "#39d1b4"      # 主强调（青绿）
C_FOCUS = "#ffd166"     # 专注（黄）
C_STRESS = "#ef476f"    # 压力（红）
C_RELAX = "#4cc9f0"     # 放松（蓝）
C_CH = ("#39d1b4", "#7b9fe0")   # 两条波形
CHIP_WARN = ("#FEE2E2", "#991B1B")
CHIP_FOCUS = ("#FFF3D6", "#8A6D1F")
CHIP_MOVE = ("#DDF3FD", "#1B6A8A")
CHIP_IDLE = ("#122b3f", "#8aa4b8")   # 空闲：浅灰底


# ---------------------------------------------------------------- 数据源
class Shared:
    def __init__(self):
        self.lock = threading.Lock()
        self.ch = [deque(maxlen=int(FS * 20)), deque(maxlen=int(FS * 20))]
        self.n_pkt = 0
        self.n_bad = 0
        self.t_first = None
        self.t_last = None
        self.addr = "-"
        self.name = "-"
        self.connected = False
        self.status = "启动中…"
        self.rssi = None
        self.err = ""
        self.csv = None
        self.record = None
        self.samples = self.rx = 0
        self.record_error = None

    def push(self, a, b, now, rx=None, seq=None):
        rx = clock() if rx is None else rx
        with self.lock:
            if self.t_first is None:
                self.t_first = now
            self.t_last = now
            self.ch[0].extend(a)
            self.ch[1].extend(b)
            self.n_pkt += 1
            self.rx = rx
            self.samples += len(a)
            if self.record and not self.record_error:
                try:
                    self.record.packet(a, b, self.samples-len(a), self.n_pkt, seq, rx)
                except OSError as exc:
                    self.record_error = str(exc)
            if self.csv:
                for i in range(len(a)):
                    self.csv.write(f"{now:.4f},{a[i]},{b[i]}\n")

    def snapshot(self, n):
        with self.lock:
            a = np.fromiter(list(self.ch[0])[-n:], dtype=float)
            b = np.fromiter(list(self.ch[1])[-n:], dtype=float)
            return (a, b, self.n_pkt, self.n_bad, self.t_first, self.t_last,
                    self.samples, self.rx, self.record.gap if self.record else None)


SHARED = Shared()


class BleSource(threading.Thread):
    """后台线程：连接设备、订阅通知、解析数据"""

    def __init__(self, demo=False):
        super().__init__(daemon=True)
        self.demo = demo
        self.stop_flag = False

    def run(self):
        if self.demo:
            self._demo_loop()
        else:
            try:
                asyncio.run(self._ble_loop())
            except Exception as e:
                print("BLE 线程异常:", e)

    def _demo_loop(self):
        """合成数据：10 Hz alpha + 噪声 + 缓慢调制"""
        SHARED.name = "DEMO"
        SHARED.addr = "synthetic"
        SHARED.connected = True
        rng = np.random.default_rng(0)
        while not self.stop_flag:
            n = 16
            t = np.arange(n) / FS
            tt = time.time()
            mod = 0.5 + 0.5 * np.sin(tt / 12)          # 缓慢调制，模拟状态起伏
            a_amp, t_amp, b_amp = 18 * (1.2 - mod), 10 * mod, 5
            def gen(off, k):
                return (off
                        + a_amp * k * np.sin(2 * np.pi * 10 * t + tt)
                        + t_amp * k * np.sin(2 * np.pi * 6 * t + tt * 0.7)
                        + b_amp * k * np.sin(2 * np.pi * 20 * t + tt * 1.3)
                        + rng.normal(0, 4, n))
            a = gen(8700, 1.0)
            b = gen(9400, 0.8)
            # 演示：每 5 秒注入一次眨眼（两导同向），每 11 秒注入一次眼动（两导反向）
            now = time.time()
            if now - getattr(self, "_lb", 0) > 5:
                self._lb = now
            if now - getattr(self, "_lm", 0) > 11:
                self._lm = now
            if now - self._lb < 0.2:
                bump = 70 * np.sin(np.pi * (0.2 - (now - self._lb)) / 0.2)
                a = a + bump
                b = b + bump
            if now - self._lm < 0.35:
                # 真实扫视是"阶跃"，不是脉冲：给定恒定偏移
                a = a + 45
                b = b - 45
            SHARED.push(a, b, time.time())
            time.sleep(0.032)

    async def _ble_loop(self):
        from bleak import BleakClient, BleakScanner

        addr = None
        scan_round = 0
        while not self.stop_flag and not addr:
            scan_round += 1
            SHARED.status = f"扫描蓝牙设备（第 {scan_round} 次）…"
            try:
                devs = await BleakScanner.discover(timeout=8, return_adv=True)
            except Exception as e:
                SHARED.err = f"扫描失败: {e}"
                await asyncio.sleep(3)
                continue
            for a, (d, adv) in devs.items():
                nm = d.name or adv.local_name or ""
                if DEVICE_HINT.lower() in str(nm).lower():
                    addr = a
                    SHARED.name = nm
                    SHARED.addr = a
                    SHARED.rssi = adv.rssi
                    break
            if not addr:
                SHARED.status = f"没找到名字含 {DEVICE_HINT} 的设备（看到 {len(devs)} 个设备）"
                await asyncio.sleep(2)

        def on_data(_s, data: bytearray):
            rx = clock()
            if len(data) != 69:
                with SHARED.lock:
                    SHARED.n_bad += 1
                return
            body = bytes(data[4:-1])
            v = [int.from_bytes(body[i:i + 2], "big") for i in range(0, 64, 2)]
            SHARED.push(np.array(v[0::2], float), np.array(v[1::2], float), time.time(), rx, data[0])

        while not self.stop_flag:
            try:
                SHARED.status = f"连接 {addr} …"
                async with BleakClient(addr, timeout=20) as cli:
                    SHARED.connected = True
                    SHARED.status = "已连接，订阅数据中…"
                    subs = 0
                    for svc in cli.services:
                        for ch in svc.characteristics:
                            props = [x.lower() for x in ch.properties]
                            if "notify" in props or "indicate" in props:
                                try:
                                    await cli.start_notify(ch, on_data)
                                    subs += 1
                                except Exception as e:
                                    SHARED.err = f"{ch.uuid[-8:]}: {e}"
                    SHARED.status = f"已订阅 {subs} 个特征，等待数据…"
                    while cli.is_connected and not self.stop_flag:
                        await asyncio.sleep(0.5)
                    SHARED.connected = False
            except Exception as e:
                SHARED.connected = False
                SHARED.err = f"{type(e).__name__}: {e}"
                SHARED.status = "连接失败，2 秒后重试"
                print("连接断开:", e)
                await asyncio.sleep(2)


# ---------------------------------------------------------------- 指数
class Tracker:
    """一个生理指数：校准 → z → 0-100（可选自适应锚定）"""

    def __init__(self, name, adaptive=False):
        self.name = name
        self.adaptive = adaptive
        self.vals = []
        self.done = False
        self.m0 = self.s0 = self.b0 = None
        self.m = self.s = None
        self.hist = deque(maxlen=4000)      # (t, v, art)
        self.last_upd = 0.0
        self.score = 50.0
        self.z = 0.0

    def update(self, t, v, art):
        if not self.done:
            self.vals.append(v)
            return None
        self.hist.append((t, v, art))
        if self.adaptive and t - self.last_upd >= 30:
            cut = t - 300
            vals = [x for (tt, x, a) in self.hist if tt >= cut and not a]
            if len(vals) >= 40:
                b_now = float(np.percentile(vals, 20))
                target = self.m0 + (b_now - self.b0)
                self.m += float(np.clip(target - self.m, -0.1 * self.s, 0.1 * self.s))
            self.last_upd = t
        self.z = (v - self.m) / max(self.s, 1e-9)
        self.score = 50 + 50 * np.tanh(self.z / 2.5)
        return self.score

    def finish(self):
        if len(self.vals) < 8:
            return False
        arr = np.array(self.vals)
        self.m0 = float(np.median(arr))
        self.s0 = float(np.median(np.abs(arr - self.m0)) * 1.4826) or 0.35
        self.b0 = float(np.percentile(arr, 20))
        self.m, self.s = self.m0, self.s0
        self.done = True
        return True


# ---------------------------------------------------------------- KSS 自评
class KSSDialog(tk.Toplevel):
    """9 级困倦自评（Karolinska Sleepiness Scale）"""

    def __init__(self, master, on_pick, timeout_s=90):
        super().__init__(master)
        self.on_pick = on_pick
        self.title("困倦自评 KSS")
        self.configure(bg=BG)
        self.attributes("-topmost", True)
        self.resizable(False, False)
        self.protocol("WM_DELETE_WINDOW", lambda: self._pick(None))

        self.update_idletasks()
        w = max(460, self.winfo_reqwidth())
        h = self.winfo_reqheight()
        sw = self.winfo_screenwidth()
        self.geometry(f"{w}x{h}+{sw - w - 40}+70")

        tk.Label(self, text="现在有多困？", bg=BG, fg=FG,
                 font=("Microsoft YaHei", 13, "bold")).pack(pady=(14, 4))
        tk.Label(self, text="最近 5–10 分钟的感受，选一个数字", bg=BG, fg=MUTED,
                 font=("Microsoft YaHei", 9)).pack()

        row = tk.Frame(self, bg=BG)
        row.pack(pady=10)
        for i in range(1, 10):
            tk.Button(row, text=str(i), width=3, bg=PANEL, fg=FG, relief="flat",
                      font=("Consolas", 12, "bold"),
                      command=lambda v=i: self._pick(v)).pack(side="left", padx=2)

        tk.Label(self, text="1 = 极度清醒                        9 = 极困", bg=BG, fg=MUTED,
                 font=("Microsoft YaHei", 9)).pack(pady=(0, 10))

        self._timer = self.after(int(timeout_s * 1000), lambda: self._pick(None))

    def _pick(self, v):
        try:
            self.after_cancel(self._timer)
        except Exception:
            pass
        cb = self.on_pick
        self.on_pick = None
        self.destroy()
        if cb:
            cb(v)


# ---------------------------------------------------------------- 界面
class App(tk.Tk):
    def __init__(self, warmup=60.0, demo=False, kss_interval=300.0, kss_file="kss_log.csv"):
        super().__init__()
        self.kss_interval = kss_interval
        self.kss_file = kss_file
        self.last_kss_time = time.time()
        self.last_kss = None
        self.kss_dialog = None
        self.kss_count = 0
        self.title("KSEEG102 实时脑电面板")
        self.configure(bg=BG)
        # Tk 在高 DPI 下会把屏幕尺寸按缩放比放大，这里取 Windows 的真实像素
        try:
            import ctypes
            sw = ctypes.windll.user32.GetSystemMetrics(0)
            sh = ctypes.windll.user32.GetSystemMetrics(1)
        except Exception:
            sw, sh = self.winfo_screenwidth(), self.winfo_screenheight()
        w, h = int(sw * 0.92), int(sh * 0.90)
        self.geometry(f"{w}x{h}+{int(sw*0.04)}+{int(sh*0.04)}")
        self.minsize(900, 640)

        self.warmup = warmup
        self.t_start = time.time()
        self.cal_start = None
        self.focus = Tracker("专注", adaptive=True)
        self.stress = Tracker("压力")
        self.relax = Tracker("放松")
        self.trend = deque(maxlen=3000)     # (t, focus, stress, relax)
        self.det = EventDetector(fs=FS)
        self._det_t = None
        self.marks = deque(maxlen=80)
        self._frozen = False          # 大幅波动期间冻结
        self._big_last = 0.0
        self._big_pending = False     # 波动结束后要提示一次
        self._alert_until = 0.0
        self.ev_counts = {"blink": 0, "move": 0, "vmove": 0}
        self.ev_last = {"blink": None, "move": None, "vmove": None, "dir": 0}
        self.score_sender = ScoreSender()
        self._last_send = 0.0
        self._score_artifact = False
        self._pending_event = None
        self._event_id = 0

        self.task = None
        self._build()
        self.src = BleSource(demo=demo)
        self.src.start()
        self.protocol("WM_DELETE_WINDOW", self._on_close)
        self._after_id = self.after(200, self.tick)

    def _on_close(self):
        """关闭时取消定时器并停掉蓝牙线程，避免退出报错"""
        if self.task:
            self.task.stop('aborted')
        self.score_sender.close()
        try:
            if getattr(self, "_after_id", None):
                self.after_cancel(self._after_id)
        except Exception:
            pass
        try:
            self.src.stop_flag = True
        except Exception:
            pass
        self.destroy()

    def start_task(self):
        if self.task:
            return
        if self.kss_dialog or not self.focus.done or not SHARED.connected or clock()-SHARED.rx > 500_000_000:
            messagebox.showinfo("持续注意实验", "请先完成校准和当前自评，并确认设备正在接收数据。", parent=self)
            return
        root = Path(__file__).resolve().parent.parent / "数据" / "持续注意实验"
        record = None
        try:
            record = Recording(root, self.src.demo, FS)
            with SHARED.lock:
                SHARED.record_error = None
                SHARED.record = record
            self.task = DigitTask(self, record, self.finish_task)
        except Exception as exc:
            with SHARED.lock:
                SHARED.record = None
            if record:
                record.files.close()
            messagebox.showerror("实验启动失败", str(exc), parent=self)

    def finish_task(self):
        with SHARED.lock:
            record, SHARED.record = SHARED.record, None
        self.task = None
        self.last_kss_time = time.time()
        record.files.close()
        self.lb_msg.config(text=f"实验记录目录：{record.path}")

    # ---------- 布局 ----------
    def _build(self):
        self.grid_rowconfigure(0, weight=1)
        self.grid_columnconfigure(0, weight=1)
        self.grid_columnconfigure(1, weight=0, minsize=350)

        left = tk.Frame(self, bg=BG)
        left.grid(row=0, column=0, sticky="nsew", padx=(10, 6), pady=10)
        left.grid_columnconfigure(0, weight=1)

        self.cv_raw = self._panel(left, "原始信号（两通道）", 5, 0)
        self.cv_band = self._panel(left, "频带能量", 2, 1)
        self.cv_focus = self._panel(left, "专注指数", 3, 2)
        self.cv_mood = self._panel(left, "压力 / 放松", 3, 3)

        right = tk.Frame(self, bg=BG, width=340)
        right.grid(row=0, column=1, sticky="nsew", padx=(0, 14), pady=10)

        tk.Button(right, text="开始持续注意实验", command=self.start_task).pack(fill="x", pady=(0, 4))
        tk.Label(right, text="设备", bg=BG, fg=ACCENT, font=("Microsoft YaHei", 11, "bold")).pack(anchor="w")
        self.lb_dev = tk.Label(right, text="-", bg=BG, fg=FG, font=("Consolas", 10), justify="left", anchor="w")
        self.lb_dev.pack(fill="x", pady=(2, 10))

        tk.Label(right, text="实时指数", bg=BG, fg=ACCENT, font=("Microsoft YaHei", 11, "bold")).pack(anchor="w", pady=(14, 4))
        self.big = {}
        for key, color in (("focus", C_FOCUS), ("stress", C_STRESS), ("relax", C_RELAX)):
            row = tk.Frame(right, bg=BG)
            row.pack(fill="x", pady=3)
            tk.Label(row, text={"focus": "专注", "stress": "压力", "relax": "放松"}[key],
                     bg=BG, fg=FG, font=("Microsoft YaHei", 10), width=5, anchor="w").pack(side="left")
            lb = tk.Label(row, text="--", bg=BG, fg=color, font=("Consolas", 26, "bold"))
            lb.pack(side="right")
            self.big[key] = lb

        tk.Label(right, text="动作识别", bg=BG, fg=MUTED,
                 font=("Microsoft YaHei", 10, "bold")).pack(anchor="w", pady=(14, 4))
        self.lb_event = tk.Label(right, text="—", bg=BG, fg="#7f97a8",
                                 font=("Microsoft YaHei", 17, "bold"), anchor="w")
        self.lb_event.pack(fill="x")
        self.lb_counts = tk.Label(right, text="眨眼 0 次    眼动 0 次", bg=BG, fg=FG,
                                  font=("Consolas", 10), anchor="w", justify="left")
        self.lb_counts.pack(fill="x", pady=(2, 2))

        sens_row = tk.Frame(right, bg=BG)
        sens_row.pack(fill="x")
        tk.Label(sens_row, text="灵敏度", bg=BG, fg=MUTED,
                 font=("Microsoft YaHei", 9)).pack(side="left")
        self.lb_sens = tk.Label(sens_row, text="3.6σ", bg=BG, fg=FG, font=("Consolas", 9))
        self.lb_sens.pack(side="right")
        self.sl_sens = tk.Scale(right, from_=6.0, to=2.0, resolution=0.2, orient="horizontal",
                                bg=BG, fg=FG, troughcolor="#E2E8F0", highlightthickness=0,
                                showvalue=False, sliderlength=18, width=14, command=self._on_sens)
        self.sl_sens.set(3.6)
        self.sl_sens.pack(fill="x", pady=(0, 6))

        tk.Label(right, text="困倦自评（KSS）", bg=BG, fg=ACCENT,
                 font=("Microsoft YaHei", 11, "bold")).pack(anchor="w", pady=(14, 2))
        self.lb_kss = tk.Label(right, text="尚未自评", bg=BG, fg=FG,
                               font=("Microsoft YaHei", 10), anchor="w", justify="left")
        self.lb_kss.pack(fill="x")
        tk.Button(right, text="现在就评一次", command=self._ask_kss_now,
                  bg=PANEL, fg=FG, activebackground="#1b3a52", relief="flat",
                  font=("Microsoft YaHei", 9)).pack(fill="x", pady=(4, 6))

        tk.Label(right, text="基线", bg=BG, fg=ACCENT, font=("Microsoft YaHei", 11, "bold")).pack(anchor="w", pady=(14, 4))
        self.lb_base = tk.Label(right, text="-", bg=BG, fg=FG, font=("Consolas", 10), justify="left", anchor="w")
        self.lb_base.pack(fill="x")

        self.cal = tk.Canvas(right, height=10, bg=PANEL, highlightthickness=0)
        self.cal.pack(fill="x", pady=8)

        self.lb_msg = tk.Label(right, text="", bg=BG, fg=MUTED, font=("Microsoft YaHei", 9),
                               wraplength=250, justify="left")
        self.lb_msg.pack(fill="x")

        btn = tk.Frame(right, bg=BG)
        btn.pack(side="bottom", fill="x", pady=6)
        tk.Button(btn, text="重新校准", command=self.recal, bg=PANEL, fg=FG, activebackground="#1b3a52", relief="flat",
                  font=("Microsoft YaHei", 10)).pack(fill="x", pady=2)
        tk.Button(btn, text="退出", command=self._on_close, bg=PANEL, fg=FG, activebackground="#1b3a52", relief="flat",
                  font=("Microsoft YaHei", 10)).pack(fill="x", pady=2)

    def _panel(self, parent, title, weight, row, color=None):
        tk.Label(parent, text=title, bg=BG, fg=MUTED,
                 font=("Microsoft YaHei", 9)).grid(row=row * 2, column=0,
                                                   sticky="w", pady=(6, 0))
        cv = tk.Canvas(parent, bg=PANEL, highlightthickness=0, height=40)
        cv.grid(row=row * 2 + 1, column=0, sticky="nsew", pady=(0, 4))
        parent.grid_rowconfigure(row * 2 + 1, weight=weight)
        return cv

    # ---------- 主循环 ----------
    def tick(self):
        try:
            self._update()
            self._send_control()
            if not self.task:
                self._maybe_ask_kss()
            elif self.task.record.error or SHARED.record_error or not SHARED.connected or clock()-SHARED.rx > 1_000_000_000:
                reason = self.task.record.error or SHARED.record_error or "脑电连接中断"
                self.task.stop("acquisition_error")
                messagebox.showerror("实验已中止", reason, parent=self)
        except Exception as e:
            self.lb_msg.config(text=f"错误: {e}")
        self._after_id = self.after(200, self.tick)

    def _send_control(self):
        now = time.monotonic()
        if now - self._last_send < 1.0:
            return
        self._last_send = now
        fresh = (SHARED.connected and SHARED.t_last is not None
                 and time.time() - SHARED.t_last < 1.0)
        if not fresh:
            status = "disconnected"
        elif not self.focus.done:
            status = "calibrating"
        elif self._frozen or self._score_artifact:
            status = "artifact"
        else:
            status = "ok"
        valid = status == "ok"
        event = self._pending_event if valid else None
        self._pending_event = None
        self.score_sender.send(self.focus.score if valid else None, valid, status, event)

    def _update(self):
        n = int(FS * 8)
        a, b, npkt, nbad, t_first, t_last, samples, rx, gap = SHARED.snapshot(n)
        elapsed = time.time() - self.t_start

        # 设备信息
        pkt_rate = npkt / max(1e-9, (t_last - t_first)) if t_first else 0
        buf_s = len(a) / FS
        self.lb_dev.config(
            text=f"{SHARED.name}\n{SHARED.addr}\n"
                 f"状态: {'已连接' if SHARED.connected else '未连接'}\n"
                 f"{SHARED.status}\n"
                 f"缓冲: {buf_s:.1f} 秒 / 需 5.0 秒\n"
                 f"信号: {SHARED.rssi if SHARED.rssi is not None else '?'} dBm\n"
                 f"包数: {npkt}   包率: {pkt_rate:.1f}/s   丢包: {nbad}"
                 + (f"\n错误: {SHARED.err}" if SHARED.err else ""))

        if len(a) < int(FS * 5):
            self.cal.delete('all')
            self.lb_msg.config(text=f"等待数据…（需至少 5 秒，当前 {len(a)/FS:.1f} 秒）")
            return

        # 滤波（零相位，整段处理）
        sos = sps.butter(4, [1, 45], btype="bandpass", fs=FS, output="sos")
        bn, an = sps.iirnotch(50, 30, fs=FS)
        y = np.stack([a, b])
        y = sps.filtfilt(bn, an, y, axis=1)
        y = sps.sosfiltfilt(sos, y, axis=1)

        # 伪迹
        art = False
        for ch in range(2):
            seg = y[ch][-int(FS):]
            med = np.median(seg)
            mad = np.median(np.abs(seg - med)) * 1.4826 or 1e-9
            if np.any(np.abs(seg - med) / mad >= 6):
                art = True
        self._score_artifact = art

        # ---- 大幅波动检测：超过 12σ 视为电极移动/接触突变 ----
        s_ch = max(np.median(np.abs(y[i] - np.median(y[i]))) * 1.4826 for i in range(2)) + 1e-9
        peak = float(np.max(np.abs(y[:, -int(0.5 * FS):])))
        big = peak > 12.0 * s_ch
        now_ts = time.time()
        if big:
            self._big_last = now_ts
            self._big_pending = True
            self._frozen = True
        elif self._frozen and now_ts - self._big_last > 1.0:
            self._frozen = False
            if self._big_pending:
                self._alert_until = now_ts + 3.0     # 稳定后才提示
                self._big_pending = False

        # ---- 动作检测：单独用 0.3–12 Hz（保留阶跃平台，1 Hz 高通会把它削掉）----
        sos_e = sps.butter(2, [0.3, 12], btype="bandpass", fs=FS, output="sos")
        ye = sps.sosfiltfilt(sos_e, np.stack([a, b]), axis=1)
        if self._frozen:
            self._det_t = t_last          # 冻结期间不检测、不记录动作
            new_n = 0
        if self._det_t is None:
            self._det_t = t_last - 0.5
        new_n = int((t_last - self._det_t) * FS)
        if new_n > 0:
            new_n = min(new_n, ye.shape[1])
            self.det.push(t_last, ye[0][-new_n:], ye[1][-new_n:])
            self._det_t = t_last
            for (tt, kind, amp, direction) in self.det.detect(t_last):
                self.ev_counts[kind] += 1
                self.ev_last[kind] = time.time()
                if kind in ("move", "vmove"):
                    self.ev_last["dir"] = direction
                self.marks.append((tt, kind, direction, amp))
                if kind == "move":
                    self._event_id += 1
                    self._pending_event = {"id": self._event_id,
                                           "type": "look_right" if direction > 0 else "look_left"}

        # 指标
        win = int(4 * FS)
        seg = y[:, -win:]
        f, pxx = sps.welch(seg, fs=FS, nperseg=win, axis=1)
        p = pxx.mean(axis=0)
        bp = {}
        for name, (lo, hi) in BANDS.items():
            m = (f >= lo) & (f <= hi)
            bp[name] = float(np.trapezoid(p[m], f[m])) + 1e-12

        focus_v = float(np.log(bp["theta"] / bp["alpha"]))
        stress_v = float(np.log(bp["beta"] / (bp["alpha"] + bp["theta"])))
        relax_v = float(np.log(bp["alpha"] / (bp["theta"] + bp["beta"])))

        t = elapsed
        if (not self.focus.done and self.cal_start is None
                and SHARED.connected and t_last is not None
                and time.time() - t_last < 1.0 and not art and not self._frozen):
            self.cal_start = time.time()
        if self.task:
            quality = ("frozen" if self._frozen else "artifact" if art else "ok")
        if self._frozen or (not self.focus.done and self.cal_start is None):
            if self.task:
                self.task.record.score(samples-win, samples-1, focus_v, None, quality, rx, gap)
            sf = ss = sr = None          # 冻结：保留上一次数值，不推进基线
        else:
            sf = self.focus.update(t, focus_v, art)
            ss = self.stress.update(t, stress_v, art)
            sr = self.relax.update(t, relax_v, art)

        # 校准
        if not self.focus.done:
            cal_elapsed = 0.0 if self.cal_start is None else time.time() - self.cal_start
            prog = min(1.0, cal_elapsed / self.warmup)
            self.cal.delete("all")
            w = self.cal.winfo_width() or 240
            self.cal.create_rectangle(0, 0, w * prog, 10, fill=ACCENT, width=0)
            self.lb_msg.config(text=("等待稳定有效的脑电信号…" if self.cal_start is None
                                     else f"校准中… 请保持放松（{prog*100:.0f}%）"))
            if cal_elapsed >= self.warmup:
                ok = all(T.finish() for T in (self.focus, self.stress, self.relax))
                self.lb_msg.config(text="校准完成" if ok else "校准样本不足，已延长")
                if not ok:
                    self.warmup += 10
            self._update_event_label()
            self._draw(y, bp, None)
            return

        if self.task:
            self.task.record.score(samples-win, samples-1, focus_v, sf, quality, rx, gap)

        # 记录趋势
        self.trend.append((t, sf or 50, ss or 50, sr or 50))

        # 数值
        for key, tr in (("focus", self.focus), ("stress", self.stress), ("relax", self.relax)):
            self.big[key].config(text=f"{tr.score:5.1f}")
        self.lb_base.config(text=f"m = {self.focus.m:+.3f}\ns = {self.focus.s:.3f}\n"
                                 f"z = {self.focus.z:+.2f}\n{'伪迹' if art else '信号正常'}")
        self._update_event_label()

        now_t = time.time()
        if now_t < self._alert_until:
            self.lb_msg.config(text="信号已稳定。刚才的波动已跳过，不参与评分与基线更新")
        elif self._frozen:
            self.lb_msg.config(text="检测到大波动：已暂停评分与动作识别，等信号稳定")
        else:
            self.lb_msg.config(text="自适应基线开启（每 30 秒微调）" if not art else "检测到伪迹，本次不更新基线")

        self._draw(y, bp, True)
        self._draw_trend()

    def _update_event_label(self):
        """动作/波动提示（文字着色，无底色）"""
        now_t = time.time()
        if now_t < self._alert_until:
            txt, fg = "大幅波动，请调整电极", C_STRESS
        elif self._frozen:
            txt, fg = "信号波动中…", MUTED
        elif self.ev_last["blink"] and now_t - self.ev_last["blink"] < 2.0:
            txt, fg = "检测到眨眼", C_FOCUS
        elif self.ev_last["vmove"] and now_t - self.ev_last["vmove"] < 2.0:
            arrow = "↑" if self.ev_last["dir"] > 0 else "↓"
            txt, fg = f"检测到眼动 {arrow}", C_RELAX
        elif self.ev_last["move"] and now_t - self.ev_last["move"] < 2.0:
            arrow = "→" if self.ev_last["dir"] > 0 else "←"
            txt, fg = f"检测到眼动 {arrow}", C_RELAX
        else:
            txt, fg = "—", "#7f97a8"

        font = ("Microsoft YaHei", 13, "bold") if now_t < self._alert_until else ("Microsoft YaHei", 17, "bold")
        self.lb_event.config(text=txt, bg=BG, fg=fg, font=font)
        self.lb_counts.config(
            text=f"眨眼 {self.ev_counts['blink']}   水平 {self.ev_counts['move']}   垂直 {self.ev_counts['vmove']}")

    # ---------- KSS ----------
    def _ask_kss_now(self):
        self._open_kss()

    def _maybe_ask_kss(self):
        if self.kss_dialog is not None:
            return
        if time.time() - self.last_kss_time >= self.kss_interval:
            self._open_kss()

    def _open_kss(self):
        if self.kss_dialog is not None:
            return
        self.kss_dialog = KSSDialog(self, self._record_kss)

    def _record_kss(self, value):
        self.kss_dialog = None
        self.last_kss_time = time.time()
        if value is None:
            return
        self.last_kss = value
        self.kss_count += 1

        row = {
            "time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "elapsed_s": f"{time.time() - self.t_start:.0f}",
            "kss": value,
            "focus": f"{self.focus.score:.1f}",
            "stress": f"{self.stress.score:.1f}",
            "relax": f"{self.relax.score:.1f}",
            "blink": self.ev_counts["blink"],
            "move_h": self.ev_counts["move"],
            "move_v": self.ev_counts["vmove"],
            "m": f"{self.focus.m:.3f}" if self.focus.m is not None else "",
            "s": f"{self.focus.s:.3f}" if self.focus.s is not None else "",
            "z": f"{self.focus.z:.2f}" if self.focus.z is not None else "",
            "frozen": int(self._frozen),
        }
        try:
            new = not Path(self.kss_file).exists()
            with open(self.kss_file, "a", newline="", encoding="utf-8-sig") as f:
                w = csv.DictWriter(f, fieldnames=list(row.keys()))
                if new:
                    w.writeheader()
                w.writerow(row)
        except Exception as e:
            self.lb_msg.config(text=f"KSS 写入失败：{e}")

        self.lb_kss.config(
            text=f"上次：{value} 分（{datetime.now().strftime('%H:%M')}）\n"
                 f"已记录 {self.kss_count} 次 → {Path(self.kss_file).name}")

    # ---------- 绘图 ----------
    def _axis(self, cv, w, h, t0, t1, step=None):
        """底部时间轴：t0→t1 秒"""
        span = t1 - t0
        if span <= 0:
            return
        cv.create_line(0, h - 1, w, h - 1, fill=GRID)
        if step is None:
            step = 1 if span <= 10 else (5 if span <= 40 else (10 if span <= 120 else 30))
        t = np.ceil(t0 / step) * step
        while t <= t1 + 1e-9:
            x = (t - t0) / span * w
            cv.create_line(x, h - 13, x, h - 1, fill=GRID)
            lab = "0" if abs(t) < 1e-9 else f"{int(t)}s"
            cv.create_text(x, h - 16, text=lab, fill="#7f97a8",
                           font=("Consolas", 7), anchor="s")
            t += step

    def _bg(self, cv, title=""):
        cv.delete("all")
        w = cv.winfo_width() or 800
        h = cv.winfo_height() or 120
        for i in range(1, 5):
            y = h * i / 5
            cv.create_line(0, y, w, y, fill=GRID)
        return w, h

    def _draw(self, y, bp, metrics):
        # 原始信号
        cv = self.cv_raw
        w, h = self._bg(cv)
        n = min(y.shape[1], int(FS * 8))
        hw = h - 16                      # 绘图区高度（底部留给时间轴）
        for ch in range(2):
            seg = y[ch][-n:]
            span = np.percentile(np.abs(seg), 99) * 1.2 or 1
            xs = np.linspace(0, w, n)
            ys = hw / 2 - (seg - np.mean(seg)) / span * (hw * 0.4) + (ch - 0.5) * hw * 0.15
            pts = []
            for i in range(0, n, max(1, n // 900)):
                pts += [xs[i], ys[i]]
            if len(pts) >= 4:
                cv.create_line(*pts, fill=C_CH[ch], width=2)

        # 动作标记（最近 8 秒内的）
        t_now = self.det.hist[-1][0] if self.det.hist else None
        if t_now:
            for (tt, kind, direction, _amp) in self.marks:
                age = t_now - tt
                if 0 <= age <= 8:
                    x = w * (1 - age / 8.0)
                    col = C_FOCUS if kind == "blink" else C_RELAX
                    cv.create_line(x, 0, x, h - 16, fill=col, dash=(4, 4))
                    if kind == "blink":
                        label = "眨眼"
                    elif kind == "vmove":
                        label = "眼动↑" if direction > 0 else "眼动↓"
                    else:
                        label = "眼动→" if direction > 0 else "眼动←"
                    cv.create_text(x + 4, 12, text=label, anchor="w", fill=col,
                                   font=("Microsoft YaHei", 9))
        self._axis(cv, w, h, -8, 0, step=1)

        # 频带能量：三条等宽轨道，按占比填充
        cv = self.cv_band
        w, h = self._bg(cv)
        total = sum(bp.values()) or 1
        names = [("theta", "#f4a261"), ("alpha", ACCENT), ("beta", "#7b9fe0")]
        track_w = (w - 40) / 3
        for i, (name, color) in enumerate(names):
            frac = bp[name] / total
            x0 = 10 + i * track_w
            y0, y1 = h * 0.55, h * 0.85
            cv.create_rectangle(x0, y0, x0 + track_w - 12, y1, outline=GRID, width=1)
            cv.create_rectangle(x0, y0, x0 + max(2, (track_w - 12) * frac), y1, fill=color, width=0)
            cv.create_text(x0, h * 0.14, text=f"{name} {frac*100:.0f}%", anchor="w",
                           fill=FG, font=("Consolas", 10))
        if self.det.hist:
            el = self.det.hist[-1][0] - self.t_start
            cv.create_text(w - 8, h * 0.14, text=f"t = {el:.0f} s", anchor="e",
                           fill="#7f97a8", font=("Consolas", 9))

        # 专注趋势
        if metrics and len(self.trend) >= 2:
            cv = self.cv_focus
            w, h = self._bg(cv)
            hw = h - 16
            t1 = self.trend[-1][0]
            t0 = max(self.trend[0][0], t1 - 60)
            self._line(cv, w, hw, [(t, f) for (t, f, _, _) in self.trend], C_FOCUS, t0, t1)
            cv.create_text(6, 8, text="100", anchor="w", fill="#7f97a8", font=("Consolas", 8))
            cv.create_text(6, hw - 10, text="0", anchor="w", fill="#7f97a8", font=("Consolas", 8))
            self._axis(cv, w, h, t0, t1)

    def _draw_trend(self):
        cv = self.cv_mood
        w, h = self._bg(cv)
        hw = h - 16
        cv.create_text(6, 8, text="压力", anchor="w", fill=C_STRESS, font=("Microsoft YaHei", 9))
        cv.create_text(60, 8, text="放松", anchor="w", fill=C_RELAX, font=("Microsoft YaHei", 9))
        if len(self.trend) >= 2:
            t1 = self.trend[-1][0]
            t0 = max(self.trend[0][0], t1 - 60)
            self._line(cv, w, hw, [(t, s) for (t, _, s, _) in self.trend], C_STRESS, t0, t1)
            self._line(cv, w, hw, [(t, r) for (t, _, _, r) in self.trend], C_RELAX, t0, t1)
            self._axis(cv, w, h, t0, t1)

    def _line(self, cv, w, h, series, color, t0, t1):
        if len(series) < 2 or t1 <= t0:
            return
        pts = []
        for (t, v) in series:
            if t < t0:
                continue
            x = (t - t0) / (t1 - t0) * w
            y = h - (v / 100.0) * (h * 0.85) - h * 0.07
            pts += [x, y]
        if len(pts) >= 4:
            cv.create_line(*pts, fill=color, width=2.5)

    def _on_sens(self, val):
        """调灵敏度：数值越小越敏感"""
        try:
            k = float(val)
        except Exception:
            return
        self.det.blink_k = k
        self.det.move_k = max(1.2, k - 0.6)
        if hasattr(self, "lb_sens") and self.lb_sens:
            self.lb_sens.config(text=f"{k:.1f}σ")

    def recal(self):
        for T in (self.focus, self.stress, self.relax):
            T.__init__(T.name, T.adaptive)
        self.trend.clear()
        self.t_start = time.time()
        self.cal_start = None
        self._pending_event = None
        self.lb_msg.config(text="已重新开始校准")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--demo", action="store_true")
    ap.add_argument("--warmup", type=float, default=60.0)
    ap.add_argument("--csv", default=None)
    ap.add_argument("--kss-interval", type=float, default=300.0, help="每隔多少秒弹一次自评")
    ap.add_argument("--kss-file", default="kss_log.csv", help="自评记录文件")
    args = ap.parse_args()

    if args.csv:
        f = open(args.csv, "w", encoding="utf-8")
        f.write("t,chA,chB\n")
        SHARED.csv = f

    app = App(warmup=args.warmup, demo=args.demo,
              kss_interval=args.kss_interval, kss_file=args.kss_file)
    try:
        app.mainloop()
    finally:
        SHARED.csv and SHARED.csv.close()


if __name__ == "__main__":
    main()
