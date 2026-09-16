#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
合成地块边界 GeoJSON，供 ECharts 地图渲染单产热力图。

【为什么不需要下载任何 shp / 栅格数据】
地图上显示的地块边界，在这个系统里是**业务数据**（"我们农场自己的地块"），
不是底图数据。所以它本来就应该由系统自己产生。

用真实研究区的 shp 反而不对：
  1. 那是"研究区"，不是"某合作社的地块"，语义不匹配；
  2. 涉及数据归属和授权问题，作品集项目不应依赖来源不明的矢量数据；
  3. 演示时需要能一键重建，外部数据依赖会破坏这一点。

【地块形状怎么来的】
对每个地块，在其中心点周围生成一个带随机扰动的多边形（4~7 个顶点），
再按面积缩放，使多边形面积恰好等于该地块的 area_mu。
顶点半径和角度都加抖动，所以看起来像真实田块而不是整齐的正方形。

【想换成自己研究区的真实边界怎么办】
用 scripts/from_shp.py —— 它读 shp 转 GeoJSON。注意它需要 geopandas，
本项目刻意不把 geopandas 装进 .venv（GDAL 系依赖太重），
用你已有的 Python 环境跑即可。
"""

import argparse
import json
import math
import random
import sys
from pathlib import Path

import pymysql

BASE_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE_DIR))
from config import DB_CONFIG  # noqa: E402

if hasattr(sys.stdout, 'reconfigure'):
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')

SEED = 42
MU_TO_SQM = 2000.0 / 3.0     # 1 亩 = 666.667 平方米（1 公顷 = 15 亩）
DEG_LAT_M = 111320.0         # 1 度纬度约 111.32 公里


def polygon_area(coords):
    """鞋带公式（Shoelace formula）计算多边形面积。

    对顶点 (x_i, y_i)：A = |Σ(x_i·y_{i+1} − x_{i+1}·y_i)| / 2
    要求顶点按顺序排列（顺/逆时针均可，取绝对值）。
    这里用来把生成的多边形缩放到目标面积。
    """
    s = 0.0
    n = len(coords)
    for i in range(n):
        x1, y1 = coords[i]
        x2, y2 = coords[(i + 1) % n]
        s += x1 * y2 - x2 * y1
    return abs(s) / 2.0


def make_plot_polygon(rng, lon, lat, area_mu):
    """
    在 (lon, lat) 周围生成一个面积等于 area_mu 的不规则多边形。

    步骤：
      1. 在"单位空间"里生成一个半径带抖动的多边形（顶点数 4~7）
      2. 用鞋带公式算它的单位面积
      3. 缩放因子 = sqrt(目标面积 / 单位面积)  —— 面积按平方缩放
      4. 把缩放后的局部坐标（米）换算成经纬度偏移
         （经度方向要除以 cos(lat)，因为经线随纬度收窄）
    """
    n = rng.randint(4, 7)
    # 顶点角度加抖动，避免出现规则多边形
    angles = sorted(rng.uniform(0, 2 * math.pi) for _ in range(n))
    radii = [rng.uniform(0.72, 1.28) for _ in range(n)]

    unit = [(r * math.cos(a), r * math.sin(a)) for r, a in zip(radii, angles)]
    ua = polygon_area(unit)
    if ua <= 0:
        ua = 1.0

    target_sqm = area_mu * MU_TO_SQM
    scale = math.sqrt(target_sqm / ua)

    # 米 → 度。经度方向除以 cos(纬度) 修正
    cos_lat = max(math.cos(math.radians(lat)), 0.1)
    ring = [[round(lon + (x * scale) / (DEG_LAT_M * cos_lat), 6),
             round(lat + (y * scale) / DEG_LAT_M, 6)] for x, y in unit]
    ring.append(ring[0])          # GeoJSON 的环必须闭合（首尾点相同）
    return ring


def main():
    ap = argparse.ArgumentParser(description='合成地块边界 GeoJSON')
    ap.add_argument('--out', default=str(BASE_DIR / 'app' / 'static' / 'geo' / 'plots.geojson'))
    args = ap.parse_args()

    rng = random.Random(SEED)
    conn = pymysql.connect(database='smart_agri', **DB_CONFIG)
    cur = conn.cursor()
    cur.execute("""
        SELECT p.plot_no, p.name, p.longitude, p.latitude, p.area_mu,
               f.name, f.province, f.city, f.region_code,
               p.fertility_level, p.soil_type, p.irrigation_type
        FROM plot p JOIN farm f ON f.id = p.farm_id
        ORDER BY p.id
    """)
    rows = cur.fetchall()

    if not rows:
        print('❌ plot 表里没有地块数据。')
        print('   本脚本要从数据库读地块坐标，必须**在造数之后**执行。')
        print('   正确顺序：')
        print('       python scripts/init_db.py --force   # 建库建表')
        print('       python scripts/seed.py              # 造数（含地块坐标）')
        print('       python scripts/gen_geojson.py       # 再生成地块边界')
        cur.close()
        conn.close()
        return 1

    features = []
    for (plot_no, name, lon, lat, area_mu, farm_name, prov, city,
         region_code, fertility, soil, irrigation) in rows:
        if not lon or not lat:
            print(f'  跳过 {plot_no}：缺少经纬度')
            continue
        ring = make_plot_polygon(rng, float(lon), float(lat), float(area_mu))
        features.append({
            'type': 'Feature',
            'properties': {
                'plot_no': plot_no,
                'name': name,
                'farm': farm_name,
                'province': prov,
                'city': city,
                'region_code': region_code,
                'area_mu': float(area_mu),
                'fertility_level': fertility,
                'soil_type': soil,
                'irrigation_type': irrigation,
            },
            'geometry': {'type': 'Polygon', 'coordinates': [ring]},
        })

    geojson = {
        'type': 'FeatureCollection',
        'features': features,
        # 附带一个建议的初始视野（各点经纬度范围外扩 10%）
        'bbox_hint': {
            'min_lon': min(f['geometry']['coordinates'][0][i][0]
                           for f in features for i in range(len(f['geometry']['coordinates'][0]))),
            'max_lon': max(f['geometry']['coordinates'][0][i][0]
                           for f in features for i in range(len(f['geometry']['coordinates'][0]))),
            'min_lat': min(f['geometry']['coordinates'][0][i][1]
                           for f in features for i in range(len(f['geometry']['coordinates'][0]))),
            'max_lat': max(f['geometry']['coordinates'][0][i][1]
                           for f in features for i in range(len(f['geometry']['coordinates'][0]))),
        },
    }

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(geojson, ensure_ascii=False), encoding='utf-8')

    total_mu = sum(f['properties']['area_mu'] for f in features)
    print(f'已生成 {len(features)} 个地块边界 -> {out}')
    print(f'地块总面积 {total_mu:,.1f} 亩 ({total_mu / 15:,.1f} 公顷)')
    print(f'经度范围 {geojson["bbox_hint"]["min_lon"]:.4f} ~ {geojson["bbox_hint"]["max_lon"]:.4f}')
    print(f'纬度范围 {geojson["bbox_hint"]["min_lat"]:.4f} ~ {geojson["bbox_hint"]["max_lat"]:.4f}')
    print('文件大小 %.1f KB' % (out.stat().st_size / 1024))

    cur.close()
    conn.close()
    return 0


if __name__ == '__main__':
    sys.exit(main())
