#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
标定各作物的 gdd_maturity（从播种到成熟所需有效积温）。

【为什么需要这个脚本】
最初 CROPS 里的 gdd_maturity 是直接抄文献的（玉米 2400℃·d 之类）。但那些值
对应的是**另一套基数温度和更长的生育期**，放到本项目的气候和播期上完全不匹配：
实测各作物的"实际积温 / 所需积温"中位数只有 0.1 ~ 0.7，也就是**所有作物在
有生之年都积累不到成熟所需积温**。

后果是 f_gdd 长期卡在 0.45 的保底区间剧烈波动（番茄 CV 达 25%），
把"施肥量—单产"的真实信号完全淹没，相关系数掉到 0.2 甚至负数。

【正确做法】
按实际气候标定 —— 这也是真实农艺的做法：**用多年平均积温确定品种熟期**。
对每个作物，在其适宜地区、典型播期窗口内，采样生育期长度上能积累的 GDD，
取中位数作为基准，再乘以 0.95：使典型年份刚好达到成熟、冷年份略不足、
暖年份有余 —— 这样"积温充足度"才是一个有区分度、有故事的变量。

⚠️ 本脚本只用于**一次性标定**，标定结果已硬编码进 seed.py 的 CROPS 表。
   日常造数不需要跑它。若改了生育期天数或气候参数，才需要重新标定。
"""

import random
import statistics as st
import sys
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.stdout.reconfigure(encoding='utf-8', errors='replace')

import seed as S  # noqa: E402


def main():
    rng = random.Random(S.SEED)
    windex = {}
    for region in S.REGIONS:
        for r in S.gen_weather(rng, region):
            windex[(r[0], date.fromisoformat(r[1]))] = (r[2], r[3], r[5])

    print(f"{'作物':<6}{'旧值':>9}{'新标定值':>11}{'实积累中位':>12}{'样本':>6}")
    print('-' * 48)
    calib = {}
    for c in S.CROPS:
        code, name, _v, _cat, base_temp, old_gdd, growth, _by, _bp, sow_win, regions = c
        vals = []
        for year in (2022, 2023, 2024):
            for season, (wm, wd, wspan) in sow_win.items():
                for ri in regions:
                    rc = S.REGIONS[ri][0]
                    for _ in range(6):
                        sow = date(year, wm, wd) + timedelta(days=rng.randint(0, wspan))
                        grow = int(growth * rng.uniform(0.92, 1.12))
                        gdd, d = 0.0, sow
                        while d <= sow + timedelta(days=grow):
                            w = windex.get((rc, d))
                            if w:
                                gdd += max(0.0, (w[0] + w[1]) / 2.0 - base_temp)
                            d += timedelta(days=1)
                        vals.append(gdd)
        med = st.median(vals)
        new = round(med * 0.95 / 10) * 10
        calib[code] = new
        print(f'{name:<6}{old_gdd:>9.0f}{new:>11.0f}{med:>12.0f}{len(vals):>6}')

    print('\n可直接替换 CROPS 表中对应行的 gdd_maturity 列：')
    for c in S.CROPS:
        print(f'    {c[1]:<4} -> {calib[c[0]]:>7.0f}')


if __name__ == '__main__':
    main()
