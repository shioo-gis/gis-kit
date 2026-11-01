# 矢量工作流约定

## 数据库与批量处理

- `gpkg.py sql --query` 接收完整 SQL，例如 `SELECT * FROM parcels WHERE area > 1000`；GDB 优先 OGR SQL，不将 query 当作 where 条件。
- File GDB 使用 GDAL OpenFileGDB 驱动；写入需 GDAL ≥ 3.6，受限功能先输出 GPKG，不冒充写入成功。
- `gpkg.py` 只读 File GDB：`import`、`copy`、`export`、`sql --output` 写到 `.gdb` 以及对 `.gdb` 的 `drop`、`rename` 都会拒绝，报错写明运行时 GDAL 的 File GDB 能力和改用 `.gpkg` 的做法。gis-kit 写出的 GDB 经 ArcGIS 验收之前不放开。
- `batch.py --script` 用于简单单输入、`--output` 形式的脚本；带子命令或多参数布局使用 `--command` 及 `{input}` / `{output}`。必要时用 `--runner` 选择已有解释器，不假设所有脚本兼容简单模式。
- 大文件优先选脚本已提供的数据裁剪或分块能力：`clip.py` 的普通 bbox 裁剪可下推；`merge.py --chunk-size` 顺序读写。分块本身不保证全局运算等价。

## 缓冲距离

`buffer.py` 的 `--distance`、`--field` 与 `--distances` 均以米为单位；投影 CRS 按水平轴单位换算，输出保留输入 CRS。经纬度数据会使用局部 UTM 近似后再投回原 CRS；跨 UTM 带、超出可靠局部范围、缺失或无法识别的 CRS 会失败，不写缓冲结果。

同心圈（服务范围、步行圈）用 `--distances 400,800,1200`：每个输入要素按距离各出一个完整圆面，带 `buffer_m`（米）和 `ring`（1 为最内圈），按输入顺序、由内到外排列；`--dissolve` 时按圈合并，多个中心的重叠范围逐圈并成一片。输入已有 `buffer_m` 或 `ring` 字段时拒绝。圈内要素的距离用 `join.py --predicate nearest --max-distance <最大圈> --distance-field distance_m` 标注，交给 Composer 的 `rings` 编码块按距离过滤高亮。

```bash
python3 gis-kit/scripts/buffer.py stations.geojson --distances 400,800,1200 --output inputs/walk/rings.geojson
```

## 大数据基准

先小数据回归，再把必须保持精确语义的最慢操作设为 `--gate`。时间与 RSS 达标时不引入 Rust；超过门槛为 `RUST_CANDIDATE`，普通非零退出或 RSS 不可采样为 `INCONCLUSIVE`，先解决环境/命令问题。

```bash
python3 gis-kit/scripts/benchmark.py \
  --max-seconds 900 --max-rss-mb 12288 \
  --case "clip=python3 gis-kit/scripts/clip.py /path/data.shp --bbox=10,50,20,60 --output /tmp/clip.gpkg" \
  --case "dissolve=python3 gis-kit/scripts/dissolve.py /path/data.shp --output /tmp/dissolved.gpkg" \
  --gate dissolve --report /tmp/gis-benchmark.json
```

报告不写原始命令或子进程日志；macOS/Linux RSS 为脚本主进程采样值。输出放临时目录。

`dissolve.py --method coverage` 仅用于有效且无面积重叠的面，脚本检查几何有效性和坐标平面面积守恒，失败则拒绝写出；它仍需全量载入。`--no-global-merge` 可降低分块结果内存开销，但同组可能重复，不能作为全局精确 dissolve 的替代基准。

## 模板与配置

`plot.py --category ... --style ...` 使用 YAML 模板：`templates/location/`、`landuse/`；`plot-chart.py` 使用 `chart/` 和 `templates/colors/` 中的色板。模板定义颜色、线宽、字号和标注规则。`plot.py` 的地图模板已冻结，只作为没有 gis-composer 时的兜底出图：不指定 `--style` 时，区位图用 `location/default`，用地图用 `landuse/standard`；正式地图交给 Composer（见 [Composer 显示交付](composer-display.md)）。

`config/user.yaml` 保存默认 CRS、字体、配色和输出格式；不得用默认 CRS 覆盖来源已明确的坐标参考。

## 计量、拓扑与可靠执行

- [计量与空间关联](vector-metrics.md)：米/平方米、分析 CRS、稳定来源 ID、建筑归属及统计边界。
- [拓扑诊断](vector-topology.md)：覆盖依据、排除区、阈值单位、结构化报告及检查失败退出。
- [大数据执行与输出安全](vector-execution.md)：过滤下推、全局/分块语义、安全写出与逐项批处理记录。

真实 CLI/GPKG 回归入口：`PYTHONDONTWRITEBYTECODE=1 python3 -m pytest gis-kit/tests/test_vector_*.py gis-kit/tests/test_buffer.py gis-kit/tests/test_topology.py -p no:cacheprovider --basetemp=/path/outside/repository/tests`。集成链可设置 `GIS_EVIDENCE_DIR=/path/outside/repository/deliverables` 保留最终 GPKG 与命令证据；不将运行产物放进 skill。
