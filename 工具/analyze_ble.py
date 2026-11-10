# -*- coding: utf-8 -*-
"""分析 ble_packets.txt，反推 KSEEG102 的 BLE 数据格式"""
from pathlib import Path

p = Path(r"D:\workspace\脑机接口相关\数据\captures\ble_packets.txt")
lines = [l for l in p.read_text(encoding="utf-8").splitlines() if l.strip()]
print(f"包数: {len(lines)}")

pkts = []
for l in lines:
    parts = l.split()
    if len(parts) < 3:
        continue
    ts = float(parts[0]); uuid = parts[1]; hexs = parts[2]
    pkts.append((ts, uuid, bytes.fromhex(hexs)))

# 1) 长度分布
from collections import Counter
lens = Counter(len(d) for _, _, d in pkts)
print(f"包长分布: {dict(lens)}")

# 2) 时间间隔
dts = [pkts[i+1][0] - pkts[i][0] for i in range(len(pkts)-1)]
dts.sort()
if dts:
    print(f"包间隔: 中位 {dts[len(dts)//2]*1000:.0f} ms, 最小 {dts[0]*1000:.0f}, 最大 {dts[-1]*1000:.0f}")

# 3) 前几包结构
print("\n前 4 包字节:")
for ts, _, d in pkts[:4]:
    print(f"  len={len(d):2d}  {d.hex(' ')}")

# 4) 序列号是否递增
seq = [d[0] for _, _, d in pkts[:20]]
print(f"\n前 20 包首字节: {seq}")

# 5) 固定头部？
heads = Counter(d[:3].hex() for _, _, d in pkts)
print(f"\n前 3 字节的取值分布（前 5）: {heads.most_common(5)}")

# 6) 尝试 16 位大端交替双通道
d = pkts[0][2]
body = d[4:-1] if len(d) > 6 else d[4:]
print(f"\n第 1 包按 16 位大端解读（body {len(body)} 字节）:")
vals = [int.from_bytes(body[i:i+2], 'big') for i in range(0, len(body)-1, 2)]
print("  全部:", vals)
if len(vals) >= 4:
    a = vals[0::2]; b = vals[1::2]
    print(f"  偶数位(通道A): {a}  均值 {sum(a)/len(a):.0f}")
    print(f"  奇数位(通道B): {b}  均值 {sum(b)/len(b):.0f}")

# 7) 跨包连续性：把所有包的偶数位拼起来，看相邻差
allA, allB = [], []
for _, _, d in pkts:
    body = d[4:-1] if len(d) > 6 else d[4:]
    v = [int.from_bytes(body[i:i+2], 'big') for i in range(0, len(body)-1, 2)]
    allA += v[0::2]; allB += v[1::2]
import statistics as st
def jumps(x):
    dif = [abs(x[i+1]-x[i]) for i in range(len(x)-1)]
    if not dif: return 0, 0
    return st.median(dif), max(dif)
print(f"\n拼接后 通道A: {len(allA)} 点, 范围 {min(allA)}~{max(allA)}, 相邻差中位/最大 {jumps(allA)}")
print(f"拼接后 通道B: {len(allB)} 点, 范围 {min(allB)}~{max(allB)}, 相邻差中位/最大 {jumps(allB)}")

# 8) 每秒采样点数估计
if len(pkts) > 2:
    dur = pkts[-1][0] - pkts[0][0]
    n_per = len(allA) / max(1e-9, dur)
    print(f"\n时间跨度 {dur:.2f} s，通道A 总点数 {len(allA)} → 约 {n_per:.0f} 点/秒")
