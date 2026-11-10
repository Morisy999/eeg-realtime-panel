# -*- coding: utf-8 -*-
"""KSEEG102 协议确认：采样率、包头、校验、信号质量"""
from pathlib import Path
from collections import Counter
import statistics as st

p = Path(r"D:\workspace\脑机接口相关\数据\captures\ble_packets.txt")
lines = [l for l in p.read_text(encoding="utf-8").splitlines() if l.strip()]

pkts = []
for l in lines:
    a = l.split()
    if len(a) >= 3:
        pkts.append((float(a[0]), bytes.fromhex(a[2])))

data69 = [(t, d) for t, d in pkts if len(d) == 69]
print(f"总包 {len(pkts)}，其中 69 字节数据包 {len(data69)}")

# 包内结构
t0, d0 = data69[0]
print(f"\n包头: {d0[:4].hex(' ')}  包尾: {d0[-1]:02x}")
print("字节1-3 取值:", Counter(d[1:4].hex() for _, d in data69).most_common(3))
print("包尾字节分布:", Counter(d[-1] for _, d in data69).most_common(5))
print("序号范围:", min(d[0] for _, d in data69), "~", max(d[0] for _, d in data69))

# 校验假设：所有字节和 mod 256 == 包尾？
ok = 0
for _, d in data69[:50]:
    s = sum(d[:-1]) % 256
    if s == d[-1]:
        ok += 1
print(f"校验假设(前 50 包, 和 mod 256 == 尾字节): {ok}/50 命中")

# 采样率
dts = [data69[i+1][0] - data69[i][0] for i in range(len(data69)-1)]
dts = [x for x in dts if x > 0]
if dts:
    med = st.median(dts)
    span = data69[-1][0] - data69[0][0]
    n_total = len(data69) * 16
    print(f"\n相邻数据包间隔: 中位 {med*1000:.1f} ms")
    print(f"时间跨度 {span:.2f} s，总点数 {n_total} → 采样率约 {n_total/span:.0f} Hz")
    print(f"（若每包 16 点、间隔 {med*1000:.0f} ms → {16/med:.0f} Hz）")

# 解析为两个通道
A, B = [], []
for _, d in data69:
    body = d[4:-1]                      # 64 字节
    v = [int.from_bytes(body[i:i+2], 'big') for i in range(0, 64, 2)]
    A += v[0::2]
    B += v[1::2]

def stats(name, x):
    dif = [abs(x[i+1]-x[i]) for i in range(len(x)-1)]
    print(f"  {name}: {len(x)} 点, 范围 {min(x)}~{max(x)}, 均值 {sum(x)/len(x):.0f}, "
          f"相邻差中位 {st.median(dif) if dif else 0:.0f}")

print("\n两通道原始值：")
stats("通道A", A)
stats("通道B", B)

# 去慢漂移后的波动幅度（判断是否像生理信号）
def hp(x, win=64):
    out = []
    acc = []
    for v in x:
        acc.append(v)
        if len(acc) > win:
            acc.pop(0)
        out.append(v - sum(acc)/len(acc))
    return out

hA, hB = hp(A), hp(B)
print("\n去 1 秒慢漂移后的波动（标准差的 10% 分位/中位/90% 分位）：")
for name, h in (("A", hA), ("B", hB)):
    seg = [h[i:i+64] for i in range(0, len(h)-64, 64)]
    s = sorted(st.pstdev(x) for x in seg)
    print(f"  {name}: {s[0]:.1f} / {s[len(s)//2]:.1f} / {s[-1]:.1f} 计数")

# 两通道相关性
n = min(len(hA), len(hB))
ma, mb = sum(hA[:n])/n, sum(hB[:n])/n
cov = sum((hA[i]-ma)*(hB[i]-mb) for i in range(n))/n
sa = (sum((hA[i]-ma)**2 for i in range(n))/n)**0.5
sb = (sum((hB[i]-mb)**2 for i in range(n))/n)**0.5
print(f"\n两通道相关系数: {cov/(sa*sb):.2f}")
