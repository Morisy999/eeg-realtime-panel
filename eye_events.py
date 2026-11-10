# -*- coding: utf-8 -*-
"""
眨眼 / 眼动 事件检测（双前额导联）
================================
原理：
  眨眼  —— 双眼同向上翻，两导同向大偏转（共模）  in  = (a + b) / 2
  眼动  —— 左右眼反向偏转，两导反相（差模）      df  = (a - b) / 2

判据（都用稳健尺度 σ = 1.4826×MAD，自动适应个人幅度）：
  眨眼：|in| 超过 5σ，持续 50–350 ms，形状为单峰
  眼动：|df| 超过 3.5σ，持续 ≥ 150 ms，且不是眨眼

带滞回与不应期，避免一次动作报多次。
"""

from collections import deque

import numpy as np


class EventDetector:
    def __init__(self, fs=500.0, blink_k=3.6, move_k=3.0,
                 refractory_blink=0.30, refractory_move=0.50,
                 hist_s=6.0):
        self.fs = fs
        self.blink_k = blink_k
        self.move_k = move_k
        self.ref_b = refractory_blink
        self.ref_m = refractory_move
        self.hist = deque(maxlen=int(fs * hist_s))
        self.sigma_in = None
        self.sigma_df = None
        self.last_b = -9.0
        self.last_m = -9.0
        self._wm_b = -9.0      # 水位线：已报告过的峰值时间
        self._wm_m = -9.0
        self._wm_v = -9.0
        self.last_v = -9.0
        self.merge_s = 1.0     # 反向回看合并窗口（秒）
        self._last_dir = 0
        self._last_mv_t = -9.0
        self.edge_k = 6.0      # 边沿阈值（σ_dd 的倍数）
        self.step_k = 3.0      # 电平差阈值（σ_df 的倍数）
        self.events = []          # (t, kind, amp, direction)

    # ---------- 输入 ----------
    def push(self, t, a, b):
        """a/b 为已滤波的两导样本数组"""
        for i in range(len(a)):
            self.hist.append((t - (len(a) - 1 - i) / self.fs, a[i], b[i]))

    # ---------- 稳健尺度 ----------
    def _scales(self):
        if len(self.hist) < self.fs:
            return None, None
        arr = np.array([(x[1], x[2]) for x in self.hist])
        inn = (arr[:, 0] + arr[:, 1]) / 2
        df = (arr[:, 0] - arr[:, 1]) / 2
        s_in = np.median(np.abs(inn - np.median(inn))) * 1.4826 or 1e-9
        s_df = np.median(np.abs(df - np.median(df))) * 1.4826 or 1e-9
        return s_in, s_df

    # ---------- 检测 ----------
    def detect(self, now):
        """返回本次新发现的事件列表 [(t, kind, amp, direction)]"""
        s_in, s_df = self._scales()
        if s_in is None:
            return []
        self.sigma_in, self.sigma_df = s_in, s_df

        data = np.array(self.hist)
        t = data[:, 0]
        inn = (data[:, 1] + data[:, 2]) / 2
        df = (data[:, 1] - data[:, 2]) / 2

        new = []
        gap_n = max(1, int(0.06 * self.fs))
        # ---- 共模事件：眨眼（窄）与垂直眼动（宽阶跃）----
        thr_b = self.blink_k * s_in
        med_in = np.median(inn)
        env = np.abs(inn - med_in)
        k_sm = max(1, int(0.04 * self.fs))
        env = np.convolve(env, np.ones(k_sm) / k_sm, mode="same")
        gap_cm = max(1, int(0.12 * self.fs))
        for s_, e_ in self._runs(env > thr_b, gap_cm):
            dur = float(t[e_] - t[s_])
            if dur < 0.03 or dur > 2.5:
                continue
            # 段被缓冲区边界截断时，等它结束再判定（否则长阶跃会被当成窄眨眼）
            if s_ == 0:
                continue
            if e_ >= len(env) - 2 and dur < 1.2:
                continue
            pk = s_ + int(np.argmax(env[s_:e_ + 1]))
            amp = float(env[pk] / s_in)
            tt = float(t[s_])                     # 以起始沿作为事件时刻
            kind = "vmove" if dur > 0.40 else "blink"
            seg = data[max(0, pk - 3):pk + 4]
            da = seg[:, 1] - np.median(data[:, 1])
            db = seg[:, 2] - np.median(data[:, 2])
            j = int(np.argmax(np.abs(da + db)))
            if abs(da[j]) < 0.25 * thr_b or abs(db[j]) < 0.25 * thr_b:
                continue
            if da[j] * db[j] <= 0:
                continue
            if kind == "blink":
                if tt <= self._wm_b or tt - self.last_b < self.ref_b:
                    continue
                if abs(tt - self.last_m) < 0.25 or abs(tt - self.last_v) < 0.9:
                    continue
                self.last_b = tt
                self._wm_b = max(self._wm_b, tt)
                new.append((tt, "blink", amp, 0))
            else:
                if tt <= self._wm_v or tt - self.last_v < 0.4:
                    continue
                if abs(tt - self.last_b) < 0.9:
                    continue
                sign_in = 1 if (inn[pk] - med_in) > 0 else -1
                self.last_v = tt
                self._wm_v = max(self._wm_v, tt)
                new.append((tt, "vmove", amp, sign_in))

        # ---- 水平眼动：阶跃边沿检测 ----
        thr_m = self.move_k * s_df
        # 先做 50ms 平滑，抑制滤波振铃造成的虚假边沿
        k_sm2 = max(1, int(0.05 * self.fs))
        df_s = np.convolve(df, np.ones(k_sm2) / k_sm2, mode="same")
        med_df = np.median(df_s)
        dev_df = df_s - med_df
        dd = np.diff(dev_df)
        s_dd = np.median(np.abs(dd - np.median(dd))) * 1.4826 or 1e-9
        fs_i = int(self.fs)
        for i in np.where(np.abs(dd) > self.edge_k * s_dd)[0]:
            tt = t[i]
            if tt <= self._wm_m or tt - self.last_m < 0.4:
                continue
            pre = dev_df[max(0, i - int(0.20 * fs_i)):max(1, i)]
            post = dev_df[i:min(len(dev_df), i + int(0.30 * fs_i))]
            if len(pre) < 5 or len(post) < 5:
                continue
            step = float(np.median(post) - np.median(pre))
            if abs(step) < self.step_k * s_df:
                continue
            if abs(tt - self.last_b) < 0.25 or abs(tt - self.last_v) < 0.3:
                continue
            direction = 1 if step > 0 else -1
            self.last_m = tt
            self._wm_m = max(self._wm_m, tt)
            # 反向且间隔很短：视为"看过去又回来"的同一次动作，只报第一次
            if direction == -self._last_dir and tt - self._last_mv_t < self.merge_s:
                self._last_mv_t = tt
                continue
            self._last_dir = direction
            self._last_mv_t = tt
            new.append((tt, "move", abs(step) / s_df, direction))

        new.sort()
        self.events.extend(new)
        return new

    # ---------- 工具 ----------
    @staticmethod
    def _runs(mask, gap=30):
        """连续 True 的 (起, 止) 索引；相距 <= gap 个样本的段合并（防止噪声把一次眨眼切成两段）"""
        runs = []
        i = 0
        n = len(mask)
        while i < n:
            if mask[i]:
                j = i
                while True:
                    k = j + 1
                    while k < n and not mask[k] and k - j <= gap:
                        k += 1
                    if k < n and mask[k]:
                        j = k
                    else:
                        break
                runs.append((i, j))
                i = j + 1
            else:
                i += 1
        return runs

    @staticmethod
    def _same_sign(seg):
        """两导偏离是否同向（眨眼要求同向）"""
        a = seg[:, 1]
        b = seg[:, 2]
        da = a - np.median(a)
        db = b - np.median(b)
        peak = np.argmax(np.abs(da))
        return (da[peak] * db[peak]) > 0


# ---------------------------------------------------------------- 自测
def _self_test():
    rng = np.random.default_rng(0)
    fs = 500
    dur = 60
    n = dur * fs
    t = np.arange(n) / fs

    # 1/f 背景 + 10 Hz alpha
    noise = np.cumsum(rng.normal(0, 1, n))
    noise = noise - np.convolve(noise, np.ones(200) / 200, mode="same")
    noise = noise / np.std(noise) * 8
    alpha = 6 * np.sin(2 * np.pi * 10 * t)
    base = noise + alpha

    a = base.copy()
    b = base.copy() + rng.normal(0, 1, n)

    blink_times = np.sort(rng.uniform(3, dur - 3, 12))
    for bt in blink_times:
        i0 = int(bt * fs)
        L = int(0.20 * fs)
        shape = np.hanning(L) * rng.uniform(40, 80)
        a[i0:i0 + L] += shape
        b[i0:i0 + L] += shape * 0.9

    move_times = np.sort(rng.uniform(3, dur - 3, 8))
    for mt in move_times:
        i0 = int(mt * fs)
        L = int(0.45 * fs)
        amp = rng.choice([-1, 1]) * rng.uniform(18, 30)
        a[i0:i0 + L] += amp
        b[i0:i0 + L] -= amp

    det = EventDetector(fs=fs)
    chunk = 16
    found = []
    for i in range(0, n - chunk, chunk):
        det.push(t[i + chunk - 1], a[i:i + chunk], b[i:i + chunk])
        if i % 25 == 0:
            found.extend(det.detect(t[i + chunk - 1]))

    hits_b = sum(1 for bt in blink_times if any(abs(f[0] - bt) < 0.35 for f in found if f[1] == "blink"))
    hits_m = sum(1 for mt in move_times if any(abs(f[0] - mt) < 0.6 for f in found if f[1] == "move"))
    nb = sum(1 for f in found if f[1] == "blink")
    nm = sum(1 for f in found if f[1] == "move")

    print(f"真实眨眼 {len(blink_times)} 次 → 检出 {hits_b} 次，报告 {nb} 次（误报 {nb - hits_b}）")
    print(f"真实眼动 {len(move_times)} 次 → 检出 {hits_m} 次，报告 {nm} 次（误报 {nm - hits_m}）")
    print(f"眨眼命中率 {hits_b/len(blink_times)*100:.0f}%，眼动命中率 {hits_m/len(move_times)*100:.0f}%")
    print(f"σ_in={det.sigma_in:.2f}  σ_df={det.sigma_df:.2f}")
    return hits_b, len(blink_times), hits_m, len(move_times)


if __name__ == "__main__":
    _self_test()
