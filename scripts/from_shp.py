#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
【可选】用你自己的 shapefile / GeoJSON 替换合成地块边界。

⚠️ 这个脚本需要 geopandas，而**项目 .venv 里刻意没有装它**
   （GDAL 系依赖太重，且本项目其余部分完全不需要）。
   用你已有的、装了 geopandas 的 Python 环境跑即可：

       python scripts/from_shp.py --shp 你的地块.shp --plot-field 地块编号

   比如用你 PyCharm 那个环境：
       C:/Users/pycharm/PycharmProjects/PythonProject1/.venv/Scripts/python.exe \
           scripts/from_shp.py --shp data/my_plots.shp --plot-field PLOTNO

【它做什么】
  1. 读 shp / GeoJSON
  2. 统一重投影到 WGS84（EPSG:4326）—— ECharts 只认经纬度
  3. 用 --plot-field 指定的字段去匹配数据库里的 plot_no，
     把数据库里的业务属性（地块名、农场、面积、地力等级…）合并进来
  4. 写出与 gen_geojson.py 完全相同结构的 GeoJSON

这样前端的 map.html 一行都不用改，直接就能渲染你的真实地块。

【匹配不上怎么办】
  若你的 shp 里没有能对上 plot_no 的字段，可以用 --synthetic-ids 按顺序
  强行赋 PLOT001、PLOT002…（适用于"我就想看形状，不关心业务属性对应"的场景）。
"""

import argparse
import json
import sys
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE_DIR))

if hasattr(sys.stdout, 'reconfigure'):
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')


def main():
    ap = argparse.ArgumentParser(description='用自己的 shp/geojson 替换地块边界')
    ap.add_argument('--shp', required=True, help='输入 shp 或 geojson 路径')
    ap.add_argument('--plot-field', default=None,
                    help='与数据库 plot_no 匹配的字段名')
    ap.add_argument('--synthetic-ids', action='store_true',
                    help='忽略匹配，按顺序赋 PLOT001..PLOTnnn')
    ap.add_argument('--simplify', type=float, default=0.0,
                    help='几何简化容差（度），如 0.0001。0 表示不简化')
    ap.add_argument('--out', default=str(BASE_DIR / 'app' / 'static' / 'geo' / 'plots.geojson'))
    args = ap.parse_args()

    try:
        import geopandas as gpd
    except ImportError:
        print('❌ 没有找到 geopandas。')
        print('   这个脚本需要在装了 geopandas 的 Python 环境里运行，')
        print('   例如：C:/Users/pycharm/PycharmProjects/PythonProject1/.venv/Scripts/python.exe')
        return 1

    import pymysql
    from config import DB_CONFIG

    print(f'读取 {args.shp} ...')
    gdf = gpd.read_file(args.shp)
    print(f'  要素数 {len(gdf)}，原始 CRS {gdf.crs}')

    # --- 统一到 WGS84 ---
    if gdf.crs is None:
        print('  ⚠️ 文件没有 CRS 信息，按 WGS84 处理。若是投影坐标，结果会偏。')
    elif gdf.crs.to_epsg() != 4326:
        print(f'  重投影 {gdf.crs} -> EPSG:4326 ...')
        gdf = gdf.to_crs(epsg=4326)

    if args.simplify > 0:
        before = sum(len(g.exterior.coords) for g in gdf.geometry if g.geom_type == 'Polygon')
        gdf['geometry'] = gdf['geometry'].simplify(args.simplify, preserve_topology=True)
        after = sum(len(g.exterior.coords) for g in gdf.geometry if g.geom_type == 'Polygon')
        print(f'  几何简化：顶点 {before} -> {after}')

    # --- 从数据库取业务属性 ---
    conn = pymysql.connect(database='smart_agri', **DB_CONFIG)
    cur = conn.cursor()
    cur.execute("""SELECT p.plot_no, p.name, p.area_mu, f.name, f.province, f.city,
                          f.region_code, p.fertility_level, p.soil_type, p.irrigation_type
                   FROM plot p JOIN farm f ON f.id = p.farm_id""")
    db = {r[0]: r for r in cur.fetchall()}
    print(f'  数据库中有 {len(db)} 个地块')

    matched = 0
    features = []
    for idx, (_, row) in enumerate(gdf.iterrows(), start=1):
        geom = row.geometry
        if geom is None or geom.is_empty:
            continue
        if geom.geom_type == 'MultiPolygon':
            # 取面积最大的那个多边形
            geom = max(geom.geoms, key=lambda g: g.area)
        if geom.geom_type != 'Polygon':
            continue

        plot_no = f'PLOT{idx:03d}' if args.synthetic_ids or not args.plot_field \
            else str(row.get(args.plot_field, '')).strip()

        rec = db.get(plot_no)
        if rec:
            matched += 1
        ring = [[round(x, 6), round(y, 6)] for x, y in geom.exterior.coords]

        features.append({
            'type': 'Feature',
            'properties': {
                'plot_no': plot_no,
                'name': rec[1] if rec else str(row.get('name', plot_no)),
                'farm': rec[3] if rec else '',
                'province': rec[4] if rec else '',
                'city': rec[5] if rec else '',
                'region_code': rec[6] if rec else '',
                'area_mu': float(rec[2]) if rec else round(geom.area * 111320 ** 2 * 15 / 10000, 2),
                'fertility_level': rec[7] if rec else 2,
                'soil_type': rec[8] if rec else 1,
                'irrigation_type': rec[9] if rec else 1,
            },
            'geometry': {'type': 'Polygon', 'coordinates': [ring]},
        })

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps({'type': 'FeatureCollection', 'features': features},
                              ensure_ascii=False), encoding='utf-8')

    print(f'\n✅ 已写出 {len(features)} 个要素 -> {out}')
    print(f'   与数据库 plot_no 匹配成功 {matched} 个')
    if matched == 0 and not args.synthetic_ids:
        print('   ⚠️ 一个都没匹配上。检查 --plot-field 字段名，')
        print('      或加 --synthetic-ids 只替换几何形状。')
    if matched < len(features) and matched > 0:
        print(f'   ℹ️ {len(features) - matched} 个要素没有对应业务数据，')
        print('      地图上会因缺指标而不着色（但边界正常显示）。')

    cur.close()
    conn.close()
    return 0


if __name__ == '__main__':
    sys.exit(main())
