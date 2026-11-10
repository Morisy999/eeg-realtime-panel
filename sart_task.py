import csv
import ctypes
import random
import time
import tkinter as tk
from contextlib import ExitStack
from datetime import datetime
from pathlib import Path

clock = time.perf_counter_ns


class Recording:
    def __init__(self, root, demo, fs):
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
        self.path = Path(root) / (("demo_" if demo else "eeg_") + stamp)
        self.path.mkdir(parents=True, exist_ok=False)
        self.files = ExitStack()
        self.error = None
        self.step = round(1e9 / fs)
        self.first = self.last_score = self.valid_end = None
        self.last_rx = self.seq = self.gap = None
        try:
            self.eeg = self.writer("eeg", "sample packet device_seq offset rx_ns sample_est_ns ch_a ch_b")
            self.events = self.writer("events", "time_ns block trial digit event value rt_ms prior_score_end_sample")
            self.scores = self.writer("scores", "computed_ns start_sample end_sample focus_raw focus_score quality")
        except BaseException:
            self.files.close()
            raise

    def writer(self, name, fields):
        f = self.files.enter_context((self.path / (name + ".csv")).open("x", newline="", encoding="utf-8"))
        writer = csv.writer(f)
        writer.writerow(fields.split())
        return writer

    def packet(self, a, b, first, packet, seq, rx):
        if self.first is None:
            self.first = first
        if self.last_rx is not None and (rx - self.last_rx > 150_000_000 or
                (seq is not None and self.seq is not None and seq != (self.seq + 1) % 256)):
            self.gap = first
        self.last_rx, self.seq = rx, seq
        for i, (x, y) in enumerate(zip(a, b)):
            self.eeg.writerow((first+i, packet, seq, i, rx, rx-(len(a)-1-i)*self.step, x, y))

    def score(self, start, end, raw, value, quality, rx, gap):
        now = clock()
        if self.first is None or start < self.first:
            return
        if now - rx > 500_000_000:
            self.valid_end = None
            quality = "stale"
        if end == self.last_score:
            return
        if gap is not None and start < gap <= end:
            quality = "gap"
        self.last_score = end
        self.valid_end = (end, rx) if quality == "ok" else None
        try:
            self.scores.writerow((now, start, end, raw, value if quality == "ok" else None, quality))
        except OSError as exc:
            self.error = str(exc)

    def event(self, block, trial, digit, kind, value="", rt="", at=None):
        at = clock() if at is None else at
        prior = None
        if kind in ("stimulus_on", "probe_on") and self.valid_end:
            end, rx = self.valid_end
            if 0 <= at-rx <= 500_000_000:
                prior = end
        try:
            self.events.writerow((at, block, trial, digit, kind, value, rt, prior))
        except OSError as exc:
            self.error = str(exc)


class DigitTask:
    trials_per_half = 60
    trials_per_block = 2 * trials_per_half
    blocks = 3

    def __init__(self, parent, record, finish):
        self.record, self.finish = record, finish
        self.win = tk.Toplevel(parent)
        self.win.title("持续注意实验")
        self.win.attributes("-fullscreen", True)
        self.label = tk.Label(self.win, bg="#181818", fg="white", font=("Microsoft YaHei", 23))
        self.label.pack(fill="both", expand=True)
        self.phase = "ready"
        self.trial = self.block = 0
        self.digit = self.timer = None
        self.held = set()
        self.label.config(text="看到 3 时不按键，看到其他数字按一次空格。\n尽量又快又准。\n\n回车开始第一组，Esc 结束并保存。")
        self.win.bind("<KeyPress>", self.press)
        self.win.bind("<KeyRelease>", lambda e: self.held.discard(e.keysym))
        self.win.protocol("WM_DELETE_WINDOW", lambda: self.stop("aborted"))
        self.win.grab_set()
        self.win.focus_force()
        self.mark("session_start")

    def mark(self, kind, value="", rt="", at=None):
        self.record.event(self.block, self.trial, self.digit, kind, value, rt, at)

    def show(self, text, size):
        self.label.config(text=text, font=("Microsoft YaHei", size))
        self.label.update_idletasks()
        # Desktop compositor synchronization; timestamp is a host estimate.
        try:
            ctypes.windll.dwmapi.DwmFlush()
        except (AttributeError, OSError):
            pass
        return clock()

    def press(self, e):
        at = clock()
        key = e.keysym
        if key in self.held:
            return
        self.held.add(key)
        if key == "Escape":
            self.stop("aborted")
        elif self.phase == "ready" and key == "Return":
            self.block += 1
            self.deck = self.make_half() + self.make_half()
            no_go_count = self.deck.count(3)
            self.phase = "fixation"
            self.mark("block_start", f"no_go={no_go_count}/{self.trials_per_block}")
            self.show("+", 42)
            self.timer = self.win.after(5000, self.next_digit)
            self.next_probe = clock() + random.randint(50, 80)*10**9
        elif self.phase == "trial" and key == "space":
            rt = (at-self.onset)/1e6
            if 0 <= rt < 1150 and self.rt is None:
                self.rt = rt
                self.mark("key_down", key, rt, at)
            else:
                self.mark("extra_press", key, rt, at)
        elif self.phase == "probe" and key in ("1", "2"):
            self.mark("probe_answer", "on_task" if key == "1" else "mind_wandering", at=at)
            self.next_probe = at + random.randint(45, 75)*10**9
            self.next_digit()

    def make_half(self):
        go_digits = [1, 2, 4, 5, 6, 7, 8, 9]
        no_go_count = round(self.trials_per_half * 0.20)
        repeats, extras = divmod(self.trials_per_half - no_go_count, len(go_digits))
        digits = [3] * no_go_count + go_digits * repeats + random.sample(go_digits, extras)
        random.shuffle(digits)
        return digits

    def resume_half(self):
        now = clock()
        self.next_probe += now - self.rest_start
        self.mark("rest_end", at=now)
        self.next_digit()

    def next_digit(self):
        self.trial += 1
        self.digit = self.deck.pop()
        self.phase, self.rt = "trial", None
        self.onset = self.show(str(self.digit), 74)
        self.mark("stimulus_on", at=self.onset)
        self.timer = self.win.after(250, self.mask)

    def mask(self):
        self.mark("stimulus_off", at=self.show("+", 42))
        remaining_ms = (self.onset + 1_150_000_000 - clock())/1e6
        self.timer = self.win.after(max(1, int(remaining_ms)+1), self.end_trial)

    def end_trial(self):
        if self.digit == 3:
            outcome = "correct_withhold" if self.rt is None else "commission"
        else:
            outcome = "omission" if self.rt is None else "hit"
        self.mark("trial_end", outcome, self.rt)
        if not self.deck:
            self.mark("block_end")
            if self.block == self.blocks:
                self.stop("completed")
            else:
                self.phase = "ready"
                self.show("本组结束，请休息。\n按回车开始下一组。", 23)
        elif len(self.deck) == self.trials_per_half:
            self.phase = "rest"
            self.digit = None
            self.rest_start = self.show("请放松休息 15 秒\n保持舒适坐姿，稍后自动继续", 23)
            self.mark("rest_start", at=self.rest_start)
            self.timer = self.win.after(15000, self.resume_half)
        elif clock() >= self.next_probe:
            self.phase = "probe"
            self.mark("probe_on", at=self.show("刚才你的注意力在哪里？\n1  在任务上       2  在想别的事", 23))
        else:
            self.next_digit()

    def stop(self, reason):
        if self.phase == "closed":
            return
        self.phase = "closed"
        if self.timer:
            self.win.after_cancel(self.timer)
        try:
            self.mark("session_end", reason)
        finally:
            self.win.destroy()
            self.finish()

