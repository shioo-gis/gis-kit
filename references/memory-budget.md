# 内存预算

默认开启的读前检查：先估算一次整读要占多少内存，超出预算就在读数据之前失败，并给出机读错误。gis-kit 只负责估算和拦截；超限之后改走 spatial-sql、缩小范围还是提高预算，由调用方（如 gis-plugin）决定。

## 设置预算

```bash
"$PY" "$GIS_SKILL/scripts/gis.py" --memory-budget 2GiB terrain /work/dem.tif --params /work/p.json --output /work/out
GIS_KIT_MEMORY_BUDGET=2GiB "$PY" "$GIS_SKILL/scripts/terrain.py" ...
```

- `gis.py --memory-budget SIZE` 把预算以字节数写入子进程的 `GIS_KIT_MEMORY_BUDGET`，不改变当前进程环境；直接调用脚本时读取同一个环境变量。
- `SIZE` 可写字节数，或带单位：`KiB/MiB/GiB/TiB` 按 1024 进位，`KB/MB/GB/TB` 按 1000 进位。非法值由 `gis.py` 以退出码 2 拒绝。
- **未设置时默认开启**，上限是此刻可用内存的 80%（macOS 取 `vm_stat`，Linux 取 `MemAvailable`，Windows 取 `GlobalMemoryStatusEx`；读不到就不检查）。所以默认上限随机器当前状态变化；要结果确定就显式写大小。`off`（`--memory-budget off` 或 `GIS_KIT_MEMORY_BUDGET=off`）关闭检查，也不再为估算读取样本。
- 预算与 params 里的 `max_pixels`、`max_elements` 等护栏叠加生效，两者都要通过；后者会被 recipe 记录，含义不变。

## 估算方法

| 对象 | 方法 | `basis` |
|---|---|---|
| 栅格 | 宽 × 高 × 份数 × 工作 dtype 字节数（默认 float64）。份数按合成样本（400 万、1600 万像元，float32、int16、int32）实测的峰值 RSS 减去导入基线校正：`terrain` 14（带 hillshade 15）、`terrain-detail` 7、`display` 8（`mode: values` 10）、`mosaic` 8、数值全量运算 输入 + 8、栅格整理 输入 + 9。`warp`、`align`：GDAL 分块读时是目标数组 4 份加不超过 320 MiB 的工作区；退回整读时加源 6 份。`terrain-backend`、`terrain-cost`、`hydro-review`、`redistribute` 没有实测过（缺 GRASS 或未跑），份数仍按代码持有的数组声明 | `decoded_pixels` |
| 图像（PIL） | 宽 × 高 × 该工具这一步的每像元字节数（按解码和转换步骤算，没有实测） | `decoded_pixels` |
| 矢量图层 | 抽样前 1000 个要素，算出每要素字节数（属性 deep 内存 + 坐标数 × 16 + 每对象 128 字节），乘以要素数，再乘峰值系数 3 | `sampled_features` |

矢量的峰值系数来自实测：pyogrio 解码时 RSS 峰值是读完后常驻大小的 2.3–3.1 倍（GPKG，2 万–20 万要素，8–512 个顶点）。驱动报不出要素数时不估算、不拦截。估算只用于拦住明显超限的读入，**不是 RSS 上限**；数据分布不均（例如前 1000 个要素特别简单）时会偏低。

## 超出预算

进程在读数据之前退出，**退出码 3**（其他失败仍为 1，参数错误为 2）。已创建的暂存目录照常清理，不发布任何结果。stderr 先输出一行说明，最后一行以 `GIS_KIT_ERROR ` 开头，后接一个 JSON 对象：

```json
{"contract": "error", "schema_version": 1, "code": "memory_budget_exceeded",
 "tool": "terrain", "input": "/work/dem.tif", "layer": null,
 "estimated_bytes": 6400000000, "budget_bytes": 2147483648, "basis": "decoded_pixels", "budget_source": "explicit"}
```

- `tool`：脚本名；`input`：触发检查的绝对路径；`layer`：矢量图层名，栅格为 `null`。
- `budget_source`：`explicit` 是调用方给的预算，`available_memory` 是默认上限。
- 只陈述事实，不给建议。读取方应忽略不认识的字段；不兼容的变更才升 `schema_version`。
- `gis.py capabilities --json` 的 `contracts.memory-budget` 给出 `env`、`exit_code` 和支持的错误版本 `error`，读取方据此判断当前 gis-kit 是否支持这套契约。

## 覆盖范围

**栅格**：`terrain`、`terrain-detail`、`terrain-backend`、`terrain-cost`、`hydro-review`，`raster` 的 display / warp / align / mosaic，`package` 的栅格，栅格整理（cleanup）操作，以及数值栅格的整幅运算和 redistribute 目标网格。按窗口处理的路径内存由窗口大小决定，不检查。

**图像**：`nonlinear-georef`、`oriented-crop`、`raster-local-trace`、`raster-annotate` 的视图和 `raster-georef` 的预览。

**矢量**：读入整个图层的公开工具都在读之前检查，`package`、`admin-chain`、`display-vector` 按 bbox 内的要素数估算，包括 buffer、clip、dissolve、edit、field、fix、join、split、stats、indicators、topo、plot、plot-chart、inspect-data、export-table、daily、network、suitability、spatial-inference、cartographic-map、atlas，以及 raster 的矢量掩膜、扫描图配准的像素矢量。

- 有多个输入的工具（join、network、suitability、topo 等）对每个输入分别检查，**不累加**；几份输入加起来超出预算时可能放行。
- 只读一部分数据时不检查：`clip` 带 `--bbox` 或 `--where`、`inspect-data head`、`dissolve --no-global-merge`（本来就按块处理）。
- CSV / Excel 表格读入、`gpkg sql` 的查询结果、写出后的回读校验不在检查范围内。

**分块读写，不受预算限制**：`convert` 输出 GPKG 时，以及 `gpkg import` / `copy` / `export`（输出 GPKG 或 Shapefile）时，超过一个分块（10 万要素）的图层按块读写，峰值内存由块大小决定，所以不做整读预检。写之前会先扫一遍数据，按整份读取时的规则确定每列 dtype 和图层几何类型（例如某整数字段只要有空值就写成浮点；Polygon 与 MultiPolygon 混合时在 GPKG 中统一为 MultiPolygon），保证结果与一次性写出一致。GeoJSON、FlatGeobuf 等格式在 GDAL 中追加写会重新载入整个文件，仍走整读路径并做预检。

`describe` 卡片里栅格的 `decoded_bytes`（全部波段按各自 dtype 解码后的大小）是一个确定值，可用于事先判断；工作副本（如 float64）会更大。
