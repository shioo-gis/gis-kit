# 日常矢量处理接口

入口：`python3 gis-kit/scripts/daily.py OP INPUT --params params.json --output NEW_DIRECTORY [--right OTHER]`。
`environment` 不需要输入，报告本机 Python、核心库、GDAL、GEOS、驱动与可选模块探测；模块存在不代表平台算法或许可可用。

参数为 JSON 对象，`schema_version` 默认 `1`。普通矢量使用 `layer`/`right_layer` 指定图层；来源 ID 使用 `id`/`right_id`，不能缺失或重复。`analysis_crs` 使用投影 CRS；距离与面积参数使用米、平方米，几何和最近位置坐标使用实际分析 CRS 的坐标单位。缺 CRS、非法几何或不适用的自动 UTM 范围会失败；显式投影适用性由调用者确认，未实现椭球计量。

## 输入与发布

CSV 按字符串读入，保留前导零及空字符串；`encoding` 默认 UTF-8。Excel 使用 `sheet`（默认第一张），需已有读取引擎；Excel 数值单元格已经丢失的前导零无法恢复。WKT/XY 表必须提供 `crs`。仅接受可独立指纹的单文件输入，Shapefile 应先受控转换为 GPKG；活动 SQLite sidecar 会拒绝。Z/M 不做静默降维，A 阶段仅支持明确的 2D 数据。

输出是全新目录，包含 `result.gpkg` 或 `result.csv` 和 `record.json`。不覆盖现存目录、不修改输入。先在同目录临时目录写出、回读，再原子重命名发布；协作写者使用短期发布锁。中途失败清理暂存，不发布半套结果。多进程崩溃遗留的 `.daily-*` 或 `.publish-lock` 需在确认无写者后处理，不自动删其他任务目录。

记录 v1 包含输入 SHA-256、实现文件 SHA-256、核心依赖版本、完整操作参数、实际分析 CRS、数量、异常/变更清单、产物 SHA-256 和回读证据。文件名用于输入角色辨认，绝对路径不自动写入记录。业务内容可能进入异常原值记录，报告按输入同等敏感等级保管。记录接口用于后续配方；当前无缓存、恢复执行或调度器。

## F01 整理与关联

`normalize`：保留原字段，在新字段中转换；重名拒绝。失败行全部从成果中排除，并在 `details.failures` 保存原始行与字段错误；同时给出接受/拒绝计数。重复主键的所有相关行均拒绝。疑似 XY 颠倒只报告可判定的越界；两个方向都合法时不能自动判断。示例：

```json
{
  "schema_version": 1, "id": "code", "wkt": "shape", "crs": "EPSG:26918",
  "fields": {
    "total": {"source": "population", "type": "float"},
    "survey_date": {"source": "date_raw", "type": "date", "format": "%Y-%m-%d"},
    "category": {"source": "class_raw", "codes": {"01": "residential"}}
  }
}
```

XY 使用 `x`、`y` 替换 `wkt`，支持十进制度和 `12°30'0"N`、`12:30:0W`。字段类型：`string`、`int`、`float`、`date`、`bool`；整数拒绝小数，布尔仅接受 true/false/1/0，代码表未映射值进入失败记录。

`attribute_join`：左侧为矢量，右侧可为表格；`key` 默认 `id`，`right_key` 默认左键，`fields` 选右表字段，`prefix` 默认 `joined_`。空键拒绝，重复右键需显式 `cardinality: "one_to_many"`，可能增加左侧行数；报告匹配、两侧未匹配和重复右键。输出字段冲突拒绝。

`update`：双侧矢量，使用同名 `id`；`fields` 列出采用的属性字段，`append`、`geometry_update` 默认 false。重复键直接失败；先生成属性前后值、几何前后 WKT、新增与未匹配清单，然后发布。几何变化可以只报告而不采用。输入保持不变。

## F02 几何加工

`geometry` 共用 `id`、`analysis_crs`、`method`，可选 `max_features`（默认 100000）。保留属性，新增 `source_id`、`source_part`、`part_id`；保留字段与新增字段冲突时拒绝。多部件使用文件存储顺序，每个部件独立从零计里程，重排部件会改变部件编号；业务来源 ID 始终保留。

| method | 参数与语义 |
|---|---|
| `split` | `--right` 点/线切割器；线可按点或线切，面按线切。重合线切割失败；`tolerance` 为分析坐标单位的重建容差（默认 1e-9），另用 `area_tolerance`（默认 1e-9，分析坐标单位平方）核对面积差 |
| `segment` | `distance_m` 分段长度，末段允许较短；输出 `start_m/end_m` |
| `sample` | `distance_m` 间距，部件首尾各一次；输出 `measure_m` |
| `locate` | `measures_m` 列表，越过部件长度拒绝 |
| `cross` | `distance_m` 采样间距、`width_m` 全宽；局部双侧割线近似切向，尖角处按此工程约定 |
| `offset` | `distance_m`、`side: left/right`；GEOS offset curve，不保证长度不变，空结果单列诊断 |
| `connect` | 点层的 `group`、`order` 字段；重复/空顺序拒绝；`source_ids` 以 JSON 字符串数组保留点来源，至少两点 |
| `explode` | 多部件拆分 |
| `rings` / `boundary` | 面内外环 / 面边界，孔洞保留 |
| `representative` / `label` | 面内代表点 / 面内标注候选点；不承诺最优排版 |

沿线结果为工程采样候选，不构成新增观测。

`grid`：面研究区生成 `square`、`hex`、`stratified`、`spaced`。提供 `method`、`size_m`、`origin: [x,y]`（实际分析 CRS 坐标），固定网格 ID 为列:行；六边形 `size_m` 为外接圆半径。裁切研究区并保留孔洞。采样固定 `seed`；`stratified` 每个方格至多一点，`spaced` 另满足 `minimum_m`，每格最多 `attempts_per_cell`（默认 100）次，不能保证每格都取得样本或达到指定数量。`max_features` 同时限制网格候选数。

`grid` 的 `voronoi` 接受至少两个不同点，另指定 `id`、`bounds: [xmin,ymin,xmax,ymax]`，边界必须包含所有站点；使用 GEOS 泰森多边形并按边界裁切，`cell_id` 为所属点 ID。

## F03 关系表

`relations` 使用 `--right`，`method` 为 `nearest`、`adjacency` 或 `bands`。记录双方稳定 ID、距离、排名、最近位置坐标及 `connector_wkt`；未匹配来源单列记录。

- `nearest`：`k`（默认 1）、可选 `radius_m`、`category` 同名类别字段、`exclude_self`。自匹配按两侧 ID 相等排除，仅在共享 ID 命名空间使用。按距离、字符串 ID 排序，等距严格取至多 K 个；半径边界包含。
- `adjacency`：仅面层。区分 point/edge/overlap，输出共享边米数、交叠平方米和双方交叠比例；接触也可能同时有重叠，分类优先 overlap。两侧同层时可用 `exclude_self`，有向关系可同时包含 A→B、B→A。
- `bands`：`bands_m` 严格递增且大于零，默认互斥 `[0,b1]`、`(b1,b2]`，`cumulative: true` 为累计；结果记录包括每个来源每个距离带的计数，零匹配也计零。

`max_pairs` 默认 100000，超限失败不截断。半径/邻接使用空间索引；无半径的最近 K 按单来源扫描目标并排序，未构造全对全矩阵，但仍可能昂贵，不宣称已优化大规模最近邻。

## F04 叠加、分配与任务组合

`overlay`：`method` 为 intersection/difference/union/identity/symmetric_difference，双侧面输入。复用 GeoPandas 全局叠加，`make_valid=False`，不静默修复。属性统一加 `left_`/`right_` 前缀；保留交集的线/点降维结果，新增 `area_m2`。记录各来源覆盖内外面积；来源数据由指纹关联。

`select`：扣除 `--right` 限制区，按残余面部件拆分，筛选 `min_area_m2`，保留 `candidate_id`、`area_m2` 和每轮排除原因。连续性按单来源残余部件定义，不跨业务 ID 自动 dissolve。

`allocate`：`value` 为来源总量字段，必须明确 `quantity_type: "total"` 和 `assumption: "uniform"`。右侧为分配面；正面积重复覆盖直接失败。输出每对面积及分配量，记录来源总量、已分配/未分配量、最大交叠对象（等面积按字符串 ID）。比率和类别不作为总量相加；首批未实现专用比率加权或类别汇总。

`summarize`：输入表或矢量，`group`、`value`、`quantity_type: "total"`；输出组内 sum/count 和总量核对。缺分组、无效数值拒绝。

完整调用链：`normalize → update/attribute_join → select/overlay → allocate → attribute_join → allocate → summarize → rules`。削减区域后，应先从原面向残余面分配总量，再由残余面向分区分配；直接把原总量套到残余面上会错误提高密度。

## F06 基础规则

`rules`：`id` 与 `rules` 数组，每条有唯一 `id`、`kind` 和可选 `severity`。

- unique/required/enum：`field`，枚举另需 `values`。
- compare：`field`、`op: le/lt/eq/ge/gt`，右值使用 `value` 或 `other_field`；不执行任意表达式。
- foreign_key：`field`、`right_field`，右表通过 `--right` 提供。
- within/disjoint：右侧空间层；within 含边界，disjoint 禁止任何接触。

错误表含源 ID、行号、规则 ID、代表位置、严重性与审核状态。存在未批准错误时 `record.status=hold`、`details.passed=false`；CLI 成功生成诊断包仍退出 0，下游必须检查状态，不能只看退出码。无自动修复。

例外由项目在 `approvals` 中显式提供 `approved_by`、`source_id`、`row`、`rule_id`、`input_sha256`、`right_sha256`（无右层为 null）、`rules_sha256`。指纹来自当前记录；规则哈希覆盖全部规则。数据或规则变化后旧例外失效并列入 `stale_approvals`，不会永久隐藏错误。B 阶段再扩展受控整理，不逐面简化冒充共享边保证。

## 旧入口迁移

`field.py` 面积/长度按米制计算，`--crs` 保留为显式分析投影；新增字段碰撞拒绝，质心先在分析投影计算再回到源 CRS。`export-table.py --include-coords` 同样支持 `--analysis-crs`，X/Y 仍采用源 CRS。表格导出先暂存与回读再发布，已有表格须显式 `--overwrite`，输入同路径拒绝。

`convert.py`、`field.py`、`fix.py` 使用已有安全写出，支持 GPKG/GeoJSON/FlatGeobuf 单文件，覆盖独立单层成果需 `--overwrite`；拒绝输入同路径、多层 GPKG 替换、活动 sidecar 和多文件格式。不再静默覆盖 Shapefile/GDB；需要这些格式时另行确认具备该格式事务契约的导出路径。`field.py --inplace` 明确拒绝并要求新文件。转换目录批处理中任一文件失败会非零退出；批次内已成功文件保留，不宣称整批原子事务。目录批处理识别 GPKG、Shapefile、GeoJSON（含 `.geojsonl`）、FlatGeobuf、FileGDB、KML/KMZ、GPX、GML、MapInfo `.tab`/`.mif` 和 DXF；单文件转换不限扩展名，GDAL 能打开即可。多图层输入（GPX 固定五层）未给 `--layer` 时拒绝，并列出图层名，不默认取第一层；两个输入会写成同名输出（如 `roads.tab` 与 `roads.mif`）时，整批在写出前拒绝。DXF 不带 CRS，转换后仍无 CRS，需要时另行确认坐标系后再赋予。超过 10 万要素的图层转为 GPKG 时按块读写，结果与一次性写出一致，见[内存预算](memory-budget.md)。`--fields 名称,代码` 只导出这些字段（按源数据里的顺序，几何总是保留），字段名不存在时在写出前报错，并列出已有字段。GeoJSON、FlatGeobuf 和 GPKG 的图层名取输出文件名，不是原子写出用的临时文件名。
