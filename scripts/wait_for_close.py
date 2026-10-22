#!/usr/bin/env python3
"""睡到美股收盘之后再继续，用等待吸收 GitHub 的队列延迟。

为什么需要这个
--------------
GitHub Actions 原生 cron 是 best-effort，实测（本仓库 + trend-dashboard +
a-share-brief 的完整调度历史）延迟区间是 0.5 到 5.5 小时，且没有哪个
UTC 时段是可靠的。

如果 cron 直接设在收盘后（北京 04:10），一旦延迟 2 小时就变成 06:10，
延迟 4 小时就彻底跨过早上 8 点。cron 越靠近送达时限，容错空间越小。

反过来做：cron 设在收盘前几个小时（北京 01:15 / 02:15），job 一旦被调度
起来就先睡到收盘。这样「队列延迟」和「睡眠」互相抵消：

    北京 01:15 准点起跑    -> 睡 4h15m（夏令时）-> 05:30 开始干活 -> 05:35 送达
    北京 01:15 延迟 3 小时  -> 04:15 起跑 -> 睡 1h15m -> 05:30 干活 -> 05:35 送达
    北京 01:15 延迟 5 小时  -> 06:15 起跑 -> 已收盘，不睡 -> 06:15 干活 -> 06:20 送达

也就是说：只要 job 在收盘后 3 小时内被调度起来，送达时间都是「收盘后
5 分钟」。这是把「延迟 20 分钟就错过」变成「延迟 5 小时仍赶得上」。

为什么敢睡这么久
----------------
仓库是 public，GitHub Actions 分钟数完全免费且不限量（私有仓库才有
2000 分钟/月 的限制）。每天最多睡 5.5 小时不产生任何费用。

时序保护
--------
美股 16:00 ET 收盘，但腾讯美股的行情时间戳明显滞后（10-21 收盘，
日志里的时间戳是 17:15:59）。等到 16:30 ET 再抓，给数据源留出落库
时间，避免把 15:50 的盘中价当成收盘价写进简报。
"""

import os
import sys
import time
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

ET = ZoneInfo("America/New_York")

# 等到收盘后 90 分钟（美东 17:30）。
#
# 为什么不是收盘就走：实测腾讯美股的行情时间戳滞后得厉害。2026-10-21 收盘
# 那天，日志里抓到的行情时间是 17:15:59，说明数据要到收盘后一个多小时
# 才完整。若在 16:05 抓，拿到的很可能是 15:50 的盘中价——那就不是「收盘
# 简报」。留 90 分钟给数据源落库，送达仍在早上 5:30~6:30，离 8 点有余量。
CLOSE_GRACE = timedelta(minutes=90)

# 单个 job 的硬上限是 6 小时。留出抓数（约 2 分钟）+ checkout/setup 的余量，
# 睡眠最长按 5h30m 控制。超过这个量说明 cron 设得太早，直接跳过交给后面的
# 触发点，而不是把 job 拖到超时（超时会被判失败并误发告警）。
MAX_WAIT_SECONDS = 5 * 3600 + 30 * 60

# 心跳间隔：让日志能看出它是在睡而不是卡死
HEARTBEAT_SECONDS = 15 * 60


def gh_output(**kv):
    """写 GitHub Actions 的 step output（本地直接跑时静默跳过）。"""
    path = os.environ.get("GITHUB_OUTPUT")
    if not path:
        for k, v in kv.items():
            print(f"[OUTPUT] {k}={v}")
        return
    with open(path, "a", encoding="utf-8") as f:
        for k, v in kv.items():
            f.write(f"{k}={v}\n")


def main():
    now = datetime.now(ET)
    print(f"[INFO] 美东当前时间 {now.strftime('%Y-%m-%d %H:%M:%S %Z')}"
          f"（UTC{now.utcoffset().total_seconds() / 3600:+.0f}）")

    # 周末不等待：cron 已限定美东工作日，这里只是防御
    if now.weekday() >= 5:
        print("[INFO] 美东今天是周末，不等待。")
        gh_output(waited="0", reason="weekend")
        return 0

    target = now.replace(hour=16, minute=0, second=0, microsecond=0) + CLOSE_GRACE
    delta = (target - now).total_seconds()

    if delta <= 0:
        print(f"[INFO] 已过收盘时刻（{target.strftime('%H:%M')} ET），"
              f"无需等待，直接继续。")
        gh_output(waited="0", reason="already-closed")
        return 0

    if delta > MAX_WAIT_SECONDS:
        # cron 设得太早，睡下去会撑爆 6 小时上限。跳过本次，交给后面的触发点。
        print(f"[WARN] 距收盘还有 {delta / 3600:.2f} 小时，超过单 job 可承受的 "
              f"{MAX_WAIT_SECONDS / 3600:.1f} 小时。本次跳过，等后续触发点。")
        gh_output(waited="0", reason="too-early")
        return 0

    print(f"[INFO] 距收盘（{target.strftime('%H:%M')} ET）还有 {delta / 60:.0f} 分钟，"
          f"开始等待。这是一次「提前排队」——把 cron 的队列延迟用睡眠吸收掉。")

    slept = 0
    next_beat = HEARTBEAT_SECONDS
    while slept < delta:
        chunk = min(30, delta - slept)
        time.sleep(chunk)
        slept += chunk
        if slept >= next_beat:
            remain = (delta - slept) / 60
            print(f"[INFO] 已等待 {slept / 60:.0f} 分钟，"
                  f"距收盘还有 {remain:.0f} 分钟")
            next_beat += HEARTBEAT_SECONDS

    after = datetime.now(ET)
    print(f"[INFO] 等待结束，美东时间 {after.strftime('%H:%M:%S')}，开始抓取收盘数据。")
    gh_output(waited=f"{slept:.0f}", reason="waited")
    return 0


if __name__ == "__main__":
    sys.exit(main())
