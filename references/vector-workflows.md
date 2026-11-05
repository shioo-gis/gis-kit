# 矢量工作流约定

## 数据库与批量处理

- `gpkg.py sql --query` 接收完整 SQL，例如 `SELECT * FROM parcels WHERE area > 1000`；GDB 优先 OGR SQL，不将 query 当作 where 条件。
- File GDB 使用 GDAL OpenFileGDB 驱动；写入需 GDAL ≥ 3.6，受限功能先输出 GPKG，不冒充写入成功。
- `gpkg.py import`、`copy`、`sql --output` 不向已有图层追加，也不替换它：目标图层已存在时（名称不分大小写）拒绝，文件不变。换一个图层名（`--target-layer`、`--output-layer`），先 `gpkg.py drop`，或用 `merge.py` 合并。
- `gpkg.py drop` 在一个事务里删除要素表、它的空间索引和注册信息（`gpkg_*`、`layer_styles`），同名图层可以再写入。
- `gpkg.py` 只读 File GDB：`import`、`copy`、`export`、`sql --output` 写到 `.gdb` 以及对 `.gdb` 的 `drop`、`rename` 都会拒绝，报错写明运行时 GDAL 的 File GDB 能力和改用 `.gpkg` 的做法。gis-kit 写出的 GDB 经 ArcGIS 验收之前不放开。

## File GDB 兼容性实测

2026-11-01 用 GDAL 3.11.4（pyogrio 0.12.1）的 OpenFileGDB 写出一份虚构 `.gdb`：两个图层（`parcels` 面、`设施点` 点），中文字段名和中文值，有空值、带洞面和多部件面，坐标系 EPSG:4547（CGCS2000 3 度带）。分别用 ArcGIS Pro 3.4.3 和 ArcMap 10.8 的 arcpy 打开，结果一致：

- 两边都能识别图层（release `3,0,0`），要素数、字段名与类型（String / Double / Integer / Date）、属性值、坐标、面积合计、EPSG:4547 都与写入时一致；`CheckGeometry` 没有报问题。
- 用 arcpy 复制后插入、更新中文值并 `Compact`，两边都能正常完成；改过的副本 GDAL 也能读回。
- 这只证明 ArcGIS 能打开这份文件，不能代替按交付模板（字段长度、别名、域、要素数据集）所做的验收，所以 `gpkg.py` 仍拒绝写 GDB。

以后放开写入时，要注意 GDAL 写 GDB 的几处默认行为：

- `int64` 列写成 Double（GDAL 会警告）。整数字段先转成 `int32`；只有确定目标是 Pro 3.2 及以上时，才用 `TARGET_ARCGIS_VERSION=ARCGIS_PRO_3_2_OR_LATER` 写成 BigInteger。
- 存放 Python `date` 对象的 object 列写成 String，要先转成 `datetime64` 才是 Date 字段；GDAL 读回时日期带 UTC 时区。
- 同一图层里 Polygon 和 MultiPolygon 混在一起会被拒绝（`Unsupported geometry type`），要用 `promote_to_multi=True` 统一成多部件。
- 文本字段不给宽度时，ArcGIS 里读出来的长度是 65536，不是 ArcGIS 默认的 255；有模板时按模板设定宽度。
- 大文件优先选脚本已提供的数据裁剪或分块能力：`clip.py` 的普通 bbox 裁剪可下推；`merge.py --chunk-size` 顺序读写。分块本身不保证全局运算等价。

## 缓冲距离

`buffer.py` 的 `--distance`、`--field` 与 `--distances` 均以米为单位；投影 CRS 按水平轴单位换算，输出保留输入 CRS。经纬度数据，以及尺度在范围内偏离 1 太多的投影（EPSG:3857 等），会使用局部 UTM 近似后再投回原 CRS；跨 UTM 带、超出可靠局部范围、缺失或无法识别的 CRS 会失败，不写缓冲结果。

同心圈（服务范围、步行圈）用 `--distances 400,800,1200`：每个输入要素按距离各出一个完整圆面，带 `buffer_m`（米）和 `ring`（1 为最内圈），按输入顺序、由内到外排列；`--dissolve` 时按圈合并，多个中心的重叠范围逐圈并成一片。输入已有 `buffer_m` 或 `ring` 字段时拒绝。圈内要素的距离用 `join.py --predicate nearest --max-distance <最大圈> --distance-field distance_m` 标注，交给 Composer 的 `rings` 编码块按距离过滤高亮。

```bash
python3 gis-kit/scripts/buffer.py stations.geojson --distances 400,800,1200 --output inputs/walk/rings.geojson
```

`dissolve.py --method coverage` 仅用于有效且无面积重叠的面，脚本检查几何有效性和坐标平面面积守恒，失败则拒绝写出；它仍需全量载入。`--no-global-merge` 可降低分块结果内存开销，但同组可能重复，不能作为全局精确 dissolve 的替代基准。

## 模板与配置

`plot.py --category ... --style ...` 使用 YAML 模板：`templates/location/`、`landuse/`；`plot-chart.py` 使用 `chart/` 和 `templates/colors/` 中的色板。模板定义颜色、线宽、字号和标注规则。`plot.py` 的地图模板已冻结，只作为没有 gis-composer 时的兜底出图：不指定 `--style` 时，区位图用 `location/default`，用地图用 `landuse/standard`；正式地图交给 Composer（见 [Composer 显示交付](composer-display.md)）。

`config/user.yaml` 保存默认 CRS、字体、配色和输出格式；不得用默认 CRS 覆盖来源已明确的坐标参考。

## 计量、拓扑与可靠执行

- [计量与空间关联](vector-metrics.md)：米/平方米、分析 CRS、稳定来源 ID、建筑归属及统计边界。
- [拓扑诊断](vector-topology.md)：覆盖依据、排除区、阈值单位、结构化报告及检查失败退出。
- [大数据执行与输出安全](vector-execution.md)：过滤下推、全局/分块语义、安全写出与逐项批处理记录。

真实 CLI/GPKG 回归入口：`PYTHONDONTWRITEBYTECODE=1 python3 -m pytest gis-kit/tests/test_vector_*.py gis-kit/tests/test_buffer.py gis-kit/tests/test_topology.py -p no:cacheprovider --basetemp=/path/outside/repository/tests`。集成链可设置 `GIS_EVIDENCE_DIR=/path/outside/repository/deliverables` 保留最终 GPKG 与命令证据；不将运行产物放进 skill。
