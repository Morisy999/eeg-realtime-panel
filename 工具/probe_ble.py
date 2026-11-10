# -*- coding: utf-8 -*-
"""
BLE 探测脚本 —— 摸清 KSEEG102 的蓝牙协议
=====================================
在你自己的 PowerShell 里运行（不要在我这边跑）：

    pip install bleak -i https://pypi.tuna.tsinghua.edu.cn/simple
    python probe_ble.py

做的事：
  1. 扫描 10 秒，列出所有 BLE 设备（名字 + 地址）
  2. 找名字含 KSEEG 的设备（或命令行指定地址）并连接
  3. 打印所有服务 / 特征 / 属性
  4. 订阅所有 notify / indicate 特征，把收到的十六进制打印出来
  5. 把前 300 个包存成 txt，方便后续分析

用法：
    python probe_ble.py                 # 自动找 KSEEG
    python probe_ble.py AA:BB:CC:DD:EE:FF   # 直接指定地址
"""

import asyncio
import sys
import time

try:
    from bleak import BleakScanner, BleakClient
except ImportError:
    print("缺少 bleak，请先运行：")
    print("  pip install bleak -i https://pypi.tuna.tsinghua.edu.cn/simple")
    raise SystemExit(1)

TARGET_KEYWORD = "KSEEG"
LISTEN_SECONDS = 10
MAX_PRINT = 60


def hexdump(data: bytes, limit: int = 64) -> str:
    b = data[:limit]
    return " ".join(f"{x:02x}" for x in b)


def ascii_of(data: bytes, limit: int = 96) -> str:
    b = data[:limit]
    return "".join(chr(x) if 32 <= x < 127 else "." for x in b)


async def main():
    addr_arg = sys.argv[1] if len(sys.argv) > 1 else None

    if not addr_arg:
        print("扫描 10 秒，请确保设备已开机、没连手机…")
        devices = await BleakScanner.discover(timeout=10, return_adv=True)
        print("\n发现的设备：")
        target = None
        for addr, (dev, adv) in devices.items():
            name = dev.name or adv.local_name or "(无名)"
            print(f"  {addr}   rssi={adv.rssi:4d}   {name}")
            if TARGET_KEYWORD.lower() in str(name).lower():
                target = addr
        if not target:
            print(f"\n没找到名字含 {TARGET_KEYWORD} 的设备。")
            print("可以手动指定地址：python probe_ble.py <地址>")
            return 1
        addr = target
    else:
        addr = addr_arg

    print(f"\n连接 {addr} …")
    try:
        async with BleakClient(addr, timeout=20) as cli:
            print("已连接:", cli.is_connected)

            print("\n===== 服务与特征 =====")
            for s in cli.services:
                print(f"[服务] {s.uuid}  {s.description or ''}")
                for c in s.characteristics:
                    print(f"    [特征] {c.uuid}  props={list(c.properties)}")
                    for d in c.descriptors:
                        print(f"        [描述符] {d.uuid}")

            # 尝试读一次可读特征
            print("\n===== 读取可读特征（每个前 32 字节）=====")
            for s in cli.services:
                for c in s.characteristics:
                    if "read" in c.properties:
                        try:
                            val = await cli.read_gatt_char(c)
                            print(f"  {c.uuid}  len={len(val)}  {hexdump(val, 32)}  |{ascii_of(val, 32)}|")
                        except Exception as e:
                            print(f"  {c.uuid}  读取失败: {e}")

            # 订阅所有通知
            packets = []
            counts = {}

            def make_cb(uuid):
                nonlocal packets
                def cb(_sender, data: bytearray):
                    counts[uuid] = counts.get(uuid, 0) + 1
                    n = counts[uuid]
                    if n <= MAX_PRINT:
                        print(f"  [{uuid[-8:]}] #{n:<4} len={len(data):3d}  {hexdump(data)}  |{ascii_of(data, 48)}|")
                    if len(packets) < 300:
                        packets.append(f"{time.time():.3f} {uuid} {data.hex()}")
                return cb

            subs = 0
            for s in cli.services:
                for c in s.characteristics:
                    props = [p.lower() for p in c.properties]
                    if "notify" in props or "indicate" in props:
                        try:
                            await cli.start_notify(c, make_cb(c.uuid))
                            print(f"  订阅成功: {c.uuid}")
                            subs += 1
                        except Exception as e:
                            print(f"  订阅失败: {c.uuid} {e}")

            if subs == 0:
                print("\n没有可订阅的特征 —— 设备可能要先写入启动命令才推数据。")
                print("下一步：把上面的服务/特征列表贴给我，我判断往哪个特征写什么。")
                return 0

            print(f"\n监听 {LISTEN_SECONDS} 秒…（现在用手指碰碰电极，看数值是否变化）")
            await asyncio.sleep(LISTEN_SECONDS)

            print("\n===== 汇总 =====")
            for uuid, n in counts.items():
                print(f"  {uuid}: {n} 包")
            if not counts:
                print("  没有收到任何数据包。")

            out = "ble_packets.txt"
            with open(out, "w", encoding="utf-8") as f:
                f.write("\n".join(packets))
            print(f"\n前 {len(packets)} 个包已存到 {out}")
    except Exception as e:
        print(f"\n连接失败：{e}")
        print("常见原因：")
        print("  1. 设备已被手机 App 占用 → 关掉手机蓝牙或退出 App")
        print("  2. 距离太远 / 电量低")
        print("  3. Windows 蓝牙驱动问题 → 设备管理器里检查")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
