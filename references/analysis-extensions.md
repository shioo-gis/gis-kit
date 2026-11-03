# 分析扩展接口

与日常矢量操作一样，输入使用单文件数据集；输出必须是不存在的新目录，禁止覆盖输入。参数文件为 JSON `schema_version: 1`。结果保存输入/实现指纹、方法、环境、异常与回读验证。候选、未知匹配及审核状态必须单独检查，CLI 成功不等于业务采用。

## 栅格：`raster.py`

```bash
PYTHONDONTWRITEBYTECODE=1 python3 gis-kit/scripts/raster.py zonal input.tif --params params.json --output /absolute/new-bundle
```

本页基础操作只有 `mosaic` 接受多个 `input`。新增多栅格/多波段数值、局部更新和守恒分配见 [数值栅格接口](raster-numeric.md)，继续使用同一入口。依赖现有 Rasterio/NumPy；分区/掩膜/采样另用 GeoPandas/Shapely。参数按下表配置：

| 操作 | 参数与语义 |
|---|---|
| `inspect` | CRS、网格、波段、类型、NoData、掩膜及存储块探查 |
| `copy` | 单波段 GeoTIFF 或 `format: "COG"`；实际检查 COG 布局 |
| `display` | 显示派生，只接受 north-up EPSG:3857。`band`、`mode: values/colors`（默认 colors）、`breaks`、`unit`、`status`；`colors` 模式给 `colors`（每个区间一个 `#RRGGBB`）；`encoding: class-index/value-rgb24`（后者配合 `kind: continuous`）。详见 [composer-display.md](composer-display.md) |
| `clip` | `vector`、可选 `layer`、`all_touched`；以面掩膜裁剪，保留源网格范围，不自动缩小外框 |
| `warp` | `crs`、可选目标单位 `resolution`；`kind: categorical/continuous`；分类默认 nearest，可显式 mode；连续必须指定 `resampling: nearest/bilinear/cubic/average/mode`；可选 `bounds`（经纬度 `[西, 南, 东, 北]`）把目标限制在覆盖该范围的源窗口（整像元外扩一格），用于从全球 DEM 取样区。`max_pixels` 按目标格网计；目标是 EPSG:3857 时格网裁进世界范围，记录里写 `target_grid_trimmed_to_world`。结果保留源类型：浮点仍是原类型、缺值用 NaN；整数只在最近邻或众数且声明了 NoData 时不变，否则用能容纳结果的最小浮点（≤16 位用 float32，32 位用 float64） |
| `align` | `reference` 栅格；严格复用其 CRS、原点、分辨率、宽高，重采样参数同 warp |
| `mosaic` | 显式 `overlap: first/last/min/max`；来源必须同 CRS、分辨率且原点按整像元对齐，否则先 align |
| `calculate` | 有限值 `scale`、`offset`；或 `threshold`、`true`、`false` 表示原像元 ≥ 阈值的条件计算；不执行任意表达式 |
| `reclassify` | `mapping: {"旧值": 新值}`；`unmapped: keep/nodata/error`，默认 nodata；区间规则与 CSV 查表见数值栅格接口 |
| `sample` | `vector` 点图层、稳定 `id`；取包含点的像元，右/下外边界在图外，缺失及图外均为 null |
| `histogram` | 有效像元类别频数，排除 NoData、掩膜和非有限值 |
| `zonal` | `vector` 面图层、`id`、`method: center/all_touched/fractional`，可选 `histogram: true` |

共用 `band`（从 1 起，默认 1）、`block_size`（默认 256）、`max_pixels`（默认 50000000）。要求已知 CRS；`inspect/warp` 接受旋转网格，其余操作要求北向上，旋转源先用 warp 正规化。外部 `.msk/.aux.xml/.ovr` sidecar 拒绝，避免单文件指纹漏掉数据语义；内部掩膜受支持。本页基础写出为单波段 Float64，NaN NoData；整数超出 ±2^53 拒绝，避免类别精度丢失。原始数据类型、波段单位及 scale/offset 记录在报告中；非默认 scale/offset 拒绝分析，须先明确校准。多波段、类型转换与显式校准使用新增 `bands` 操作。

窗口计算/掩膜/频数/统计可调块大小。warp、align 由 GDAL 分块读源，峰值取决于目标格网，不随源大小增长（1.44 亿像元的 int16 源降采样实测净约 160 MB）；源有内部掩膜、alpha 或声明之外的无穷大、NaN，或是 64 位整数时，退回整读，此时 `max_pixels` 也按源计。mosaic 使用有像元数上限的内存数组。输出逐块回读比对解码数值哈希；哈希带块大小，只能同口径比较。COG 使用 nearest overview；原分辨率像元必须保持一致。

分区输出 `valid_pixels`、`weight`、`sum`、`mean`、极值及可选直方图。fractional 使用 Shapely 对像元矩形求交，权重为投影平面覆盖比例，要求投影 CRS；计算每个有效像元的 `value × weight` 后求和，均值除以总有效权重。这里不输出平方米面积，不把经纬度像元数当面积。不同分区可重叠，各自独立统计；零有效像元的 sum/mean/min/max 为 null。浮点跨块验收容差为绝对 1e-8（和）/1e-10（权重），适用于固定测试样例，不是任意数据规模的误差承诺。

## 矢量：复用 `daily.py`

仍使用 `operation input --right other --params params.json --output /absolute/new-bundle`；无右输入的操作省略 `--right`。

- `compare`：两版数据及 `id`，可选 `fields`、`analysis_crs`、`category`。按稳定 ID 比较，保留 before/after WKT 和属性差异；几何等价忽略顶点/记录顺序。未匹配面正面积交叠提出 split/merge/complex/match 候选，状态 unknown，不采用或改写 ID。候选覆盖比例在明确的计量 CRS 下计算。`category` 生成稳定匹配 ID 的分类转移计数；缺失类别保留 null。点线跨 ID 匹配尚不支持；缺记录只说明这版数据未记录。
- `time_slice`：`id/start/end/at`，UTC 解析，半开 `[start,end)`；`unknown: hold/open` 默认为 hold，未知端点 ID 进入报告，open 将未知端点解释为无限端。未知历史日期应保持 hold，不能猜测。
- `profile`：选定 `layer` 的全量字段/缺值/异常行、CRS、范围、`time_fields`；列出所有图层但标明仅检查选定层。`required_fields` 检查缺失字段，`conditions` 原样列为未验证条件。无 CRS 也可诊断，任务可用性保持 unknown；不生成 C 阶段对象档案或图册。
- `grid_summary`：左侧点 `id`，右侧已有网格 `right_id`；复用 A 阶段 `grid`。每点保存候选格数及唯一归属，多格边界按字符串格 ID 最小值归属一次，未匹配明确记录。输出每格计数及每平方公里密度，不等于显著热点；重叠网格也沿用显式唯一归属策略。
- `distribution`：`value` 数值字段，可选 `study_area`；有限观测的数量/缺失、均值、总体标准差、线性分位数，无推断检验。
- `cluster`：点图层 `id/eps_m/min_samples`，可选 `analysis_crs/study_area`；使用已有 scikit-learn DBSCAN。按稳定 ID 排序决定边界点归属，簇名 `cluster:<最小来源ID>`，噪声 `noise`。依赖不可用时不能以自写算法代替；没有随机抽样、置换或显著性输出。

## 受控几何整理

`daily.py cleanup` 参数 `id/method/tolerance_m` 及可选 `analysis_crs`：

- `near_duplicates`：Hausdorff 距离阈值内的候选对，不删除对象。
- `precision`：Shapely 精度网格；`snap`：向 `--right` 参考几何的有限吸附。输出前后 WKT、位移、面积、部件和孔洞变化；空塌缩/无效结果拒绝。结果包 hold，业务规则未自动通过。
- `split_edges`：Shapely 线网求并拆边，保留多来源 ID；不自动决定语义连接，候选需另行审核。
- `coverage_simplify`：使用 Shapely ≥2.1 / GEOS ≥3.12 原生覆盖简化；环境见 [依赖说明](runtime-environment.md)。要求有效、非重叠且共享边顶点匹配的 Polygon/MultiPolygon 集合，计算前后均验证整体 coverage。默认 `simplify_boundary: false` 仅简化内部共享边，严格保持整体外轮廓及孔洞；显式 true 才允许简化外边界。可选 `gap_width_m`（默认 0）检测窄缝；默认允许真实孔洞，不能把有效 coverage 解释成无孔洞。

`tolerance_m` 是 Visvalingam–Whyatt 面积导出的简化尺度，**不是最大位移保证**。采用时使用独立位移与面积限额。报告保存前后顶点数、整体范围变化、CRS、后端版本及每对象影响，输出仍为 hold 候选。来源 ID 排序后整批计算，再恢复原顺序。语义依据：[coverage_simplify](https://shapely.readthedocs.io/en/stable/reference/shapely.coverage_simplify.html)、[coverage_is_valid](https://shapely.readthedocs.io/en/stable/reference/shapely.coverage_is_valid.html)。

`cleanup_adopt` 只采用同一组稳定 ID 的候选几何，保留原属性。必须显式提供 `approved_by/source_sha256/candidate_sha256/max_displacement_m/max_area_change_m2/rules`，可选 `allow_structure_change`（默认 false）；SHA-256 必须绑定两份文件。`rules` 使用日常规则列表，对前后重新检查，采用后必须全通过；跨表规则此入口不支持，不能省略它们冒充业务验收。拆边产生新 ID 的候选不能用该入口采用。

共享边简化候选采用时设置 `require_coverage: true`，重新检查两版整体覆盖；默认 `preserve_coverage_boundary: true` 保持整体外轮廓和孔洞。只有明确批准整体边界变化时才设置 false。`gap_width_m` 同样用于采用阶段的窄缝复核。

## 图幅覆盖与接缝：`coverage.py`

```bash
PYTHONDONTWRITEBYTECODE=1 python3 gis-kit/scripts/coverage.py /absolute/annotation-run --ledger ledger.json --output /absolute/new-report.json
```

读取既有 `raster-annotate.py` run，不改源图、原生曲线或版本。ledger 是项目侧手工观察/审核记录，不由程序从观看次数生成；单写者维护，报告不可覆盖。最低结构：

```json
{
  "schema_version": 1,
  "source_sha256": "<manifest image sha256>",
  "revision": 0,
  "revision_sha256": "<revision file sha256>",
  "regions": [{"id":"main","kind":"main","bounds":[0,0,100,100],"categories":["border"]}],
  "observations": [{"region":"main","category":"border","bounds":[0,0,100,100],"evidence":"<source observation>","objects":["e1"],"drawn":true,"review":"pending"}],
  "seams": [],
  "pending_faces": [],
  "uncertainties": [],
  "scope_review": {"status":"pending"}
}
```

主图/插图使用 `kind: main/inset`。范围是原图像素工程空间。`observed/drawn/reviewed` 分开计算覆盖面积并集比例，不重复累加重叠观看；绘制要关联现有对象，确无目标时写 `empty_reason`。审核通过记录 `review: accepted` 与 `reviewer`，必须源自实际审核；仅写过对象不会算审核完成。`scope_review` 需要 `status: accepted/reviewer/evidence` 明确确认声明范围，未观察插图、疑义及待闭合面均保持 hold。

接缝条目含 `node/edge/regions`（两个不同区域）、`direction/observations/status/reviewer`；双侧 observations 按区域 ID 分别记录原图证据，显式确认后才为 confirmed。程序核对节点是该边端点，不按距离自动连接。源图、版本号或版本文件哈希变化使所有旧审核保守失效（包含受影响邻边和面，未做精细增量失效）。已有 uncertain 边/点/面、incomplete 面和 uncertain crossing 也会阻止 complete。这里的 complete 仅表示声明范围的审核台账通过，不构成 C 阶段完整图幅验收或历史真实性结论。
