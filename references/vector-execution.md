# 矢量执行、批处理与安全写出

这些脚本按单一文件输入/输出运行。矢量文件可读格式由 GDAL/pyogrio 决定；安全原子写出目前支持单文件 `.gpkg`、`.geojson`、`.json` 和 `.fgb`。多文件格式（如 Shapefile）不经安全写出器支持，命令会拒绝输出，而不会退回直接覆盖。

## Clip 与属性筛选

```bash
python3 gis-kit/scripts/clip.py parcels.gpkg --layer parcels \
  --clip-layer study-area.gpkg --columns id,class \
  --ogr-where "class IN ('A', 'B')" --output selected.gpkg
```

- `--bbox` 坐标必须使用输入图层 CRS。Clip mask 和 `--filter-layer` 若带 CRS，会先转换到输入 CRS，再计算 bbox 候选与精确空间谓词；一边有 CRS、一边无 CRS 时拒绝运行。两边均无 CRS 时按调用者提供的坐标一致处理。
- `--where` 保留原有 pandas `DataFrame.eval` 语法，兼容现有命令。`--ogr-where` 是 GDAL/OGR SQL 属性过滤，会尽量下推到数据源；它不能与 `--invert` 共用，因为反选必须扫描全部输入候选。
- `--columns` 声明最终保留的属性字段。仅当与 `--ogr-where` 组合且没有 pandas `--where` 时，字段列表才与属性谓词一起下推；字段选择单独使用时只控制输出投影。有 pandas `--where` 时读取完整属性以维持任意 `DataFrame.eval` 表达式兼容。
- 反选不会下推 bbox，避免 bbox 外的要素被提前丢弃。普通选择保留 bbox 候选读取和后续精确几何判断。

## Merge 与 dissolve

```bash
python3 gis-kit/scripts/merge.py --inputs north.gpkg south.gpkg \
  --layers parcels,parcels --align-fields union --chunk-size 10000 \
  --target-crs EPSG:32650 --output merged.gpkg

python3 gis-kit/scripts/dissolve.py merged.gpkg --by district \
  --agg population:sum,name:first --output districts.gpkg
```

- `merge.py` 逐块读取/写入；字段对齐支持 `union` 和 `intersection`。缺 CRS 与有 CRS 的输入混合时拒绝合并；目标 CRS 已知时，无法给缺 CRS 输入安全重投影。输出在所有输入块成功读取、转换、写入并通过回读验证后才发布。
- `dissolve.py` 默认全局 dissolve。空分组键保留为一组。`first` 取组内首行各字段值并保留 null；`sum` 对全空组保留 null。其他聚合函数仍由 pandas/GeoPandas 执行。
- `--no-multi` 将融合后的多部件展开为单部件；每个部件会复制组级属性，因而不能再对复制的总量直接求和。
- `--no-global-merge --chunk-size N` 明确输出独立分块汇总，同一分组可以多行；这不等于全局 dissolve。分块 `sum` 只有在调用者另行掌握可组合逻辑时才可合并；分块均值没有权重/计数时不能直接平均。coverage 方法的既有有效性/面积校验逐块执行，不能证明不同块之间无重叠；调用者仍须保证输入满足 coverage 前提。

## 输出替换规则

clip、merge、dissolve 使用 `gis-kit/scripts/_safe_io.py`。输出先写入与目标同目录的临时单文件，回读检查字段、CRS（适用时）、完整要素数和数据可读性，再原子发布。该回读不等于源输出几何或属性逐值语义等价证明。

- 已存在目标默认拒绝；只有显式 `--overwrite` 才可替换单层文件。CLI 也拒绝把输出指向任一输入。
- 已存在的多层 GPKG 即使加 `--overwrite` 也拒绝替换，避免删除旁路图层。需要保留多层文件时，应写到独立的新 GPKG，再用图层管理工具合并。
- 带 `-wal`、`-shm` 或 `-journal` sidecar 的 GPKG 不允许覆盖；应先关闭持有写事务的 GIS 程序并确认数据集处于静止状态。
- 处理或回读失败时，临时文件及 SQLite sidecar 会清理，旧目标保持原状；输出不会留下可误认的部分成果。

可复用接口：`write_vector_atomic(frame, path, overwrite=False, protected_paths=())` 用于完整 GeoDataFrame；`AtomicVectorOutput(path, ...)` 的 `write(frame, append=False)` / `commit(expected_features=..., required_fields=..., expected_crs=...)` 用于分块写入；`validate_vector_file(path, ...)` 可独立回读检查。省略 `expected_crs` 表示不检查；传入 `None` 表示必须无 CRS。

## 批量运行

多个输入逐个调用工具即可，不需要专门的批处理入口。退出码 0 不等于成果可用：确认目标存在、运行后确实更新过，再用 `describe` 回读。

## 回归验证

```bash
PYTHONDONTWRITEBYTECODE=1 python3 gis-kit/tests/test_vector_execution.py
PYTHONDONTWRITEBYTECODE=1 python3 gis-kit/tests/test_large_data_paths.py
PYTHONDONTWRITEBYTECODE=1 python3 gis-kit/tests/test_benchmark.py
```

测试数据仅在系统临时目录生成，仓库内不存实验副本。
