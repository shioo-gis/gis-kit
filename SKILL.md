---
name: gis-kit
description: 处理 GIS 矢量与栅格数据、空间分析、DEM 地形、路网与设施覆盖，以及扫描地图的语义描绘和配准；用于生成可回读的分析文件、候选成果与检查图。GIS toolkit for vector/raster processing, spatial analysis, DEM terrain, road-network access and facility coverage, and scanned-map tracing and georeferencing, producing verifiable files, candidates and QA plots.
---

# GIS 数据处理与空间分析

强模型负责理解任务、搜索资料、选择工具和组合调用；gis-kit 提供确定性计算、文件契约与验证，不建设独立调度平台。gis-kit 出检查图，正式地图（成果图）、Scene / Storyboard 和地图视频使用可用的 `gis-composer` skill；`plot.py` 的地图模板已冻结，只作为没有 Composer 时的兜底，分工见 [Composer 显示交付](references/composer-display.md)。

## 发现与调用

先按[运行环境](references/runtime-environment.md)确认解释器。`PY` 为其实际路径，`GIS_SKILL` 为本 skill 的实际目录，不假设工作项目包含 `gis-kit/`。

```bash
"$PY" "$GIS_SKILL/scripts/gis.py" list --json
"$PY" "$GIS_SKILL/scripts/gis.py" list --search raster
"$PY" "$GIS_SKILL/scripts/gis.py" raster --help
```

`list` 从公开脚本的 docstring 和现有 references 生成目录，不导入 GIS 库或启动后端；原 CLI 的 `--help` 是参数事实源。使用 `gis.py <工具名> ...` 直接转发，原来的 `scripts/<工具名>.py ...` 调用继续有效。这里只保留操作判断，不维护第二份脚本/参数索引。

确需复用已执行步骤时，显式使用 `gis.py --trace /work/trace <工具名> ...`；从轨迹形成现有 recipe 草稿，选择和审阅仍由 Agent 完成。发现和轨迹整理仅需标准库，实际计算按需加载依赖。语义字段衔接用 `semantic-check` 显式比对。边界与用法见 [Harness：发现、轨迹与语义检查](references/harness.md)。

## 核心判断

- 先确认输入图层/波段、CRS、计量单位、格网、NoData 及量的含义；总量、密度、比率和类别分别处理。赋 CRS 标签不能替代重投影，投影轴单位不默认是米。
- 保留原件、来源、参数、未知项和 `candidate` / `hold`；插补、推定及显示派生与观测分开。程序完成、几何门槛通过和局部人工接受分别记录，均不证明历史准确性或全图完整。检查器仅覆盖明确选择的字段/对象；没有全链自动语义保证。
- 按实际接口回读数值、几何、CRS 和文件，图像实际查看；旧接口的覆盖/原子写入行为逐一核实。局部栅格已有窗口链，全局算子及显示派生仍有全量边界。网络接入依赖输入证据，缺需求权重不报告人口覆盖；服务路段与服务面分开。
- 通用公开数据、地名搜索与 geocoding 交给 Agent 和已有工具按任务处理。高级 GIS 优先直接使用成熟库、CLI 或可选专业后端；只有重复需求和稳定封装价值成立才扩展本 skill。不维护通用取数框架、来源注册表或 gazetteer。

## 按需参考

只读取当前任务需要的参考；具体命令从 `list --json` 的 `references` 与原生 help 下钻。

- 大数据与内存：需要读前拦截时用 `gis.py --memory-budget`，契约见[内存预算](references/memory-budget.md)。
- 大表与空间 SQL：先按[Spatial SQL 与规模路径](references/spatial-sql.md)选择 DuckDB / GDAL 等成熟引擎；可选薄入口直接接收原生 SQL，回读检查 Parquet 输出，不默认全量载入 GeoPandas。
- 矢量准备与分析：[基础工作流](references/vector-workflows.md)、[ArcGIS 图层包 / 地图包](references/esri-package.md)、[日常结果包](references/daily-vector.md)、[匹配与制图综合](references/vector-cartography.md)、[分析扩展](references/analysis-extensions.md)；CAD 图纸先按[清点、提取、围面、挂接与赋 CRS](references/cad-intake.md)看清图层、单位和坐标网（湘源控规图则直接用 `cad.py xiangyuan` 取地块），再提取实体、剔除图框图例后按显式容差围面、按规则挂文字（填充可转成面，用 `cad.py compare-fills` 和围面结果对照），确认后赋 CRS，用 `cad.py overlay` 叠参考图层出检查图；独立坐标用[矢量控制点转换](references/vector-georeferencing.md)。
- 栅格与专业分析：[数值栅格](references/raster-numeric.md)、[栅格整理](references/raster-cleanup.md)、[地形诊断](references/terrain-analysis.md)、[路网与需求覆盖](references/network-access.md)、[其他专业分析](references/professional-analysis.md)。
- 文件衔接与复用：[Harness](references/harness.md)、[map-style 打包成带样式的 GeoPackage（QGIS）](references/qgis-package.md)或 [ArcGIS Pro 图层包（.lpkx）](references/arcgis-pro-package.md)、[顺序 recipe 与重复交付](references/repeated-delivery.md)、[Composer 显示交付](references/composer-display.md)、[在线瓦片本地快照](references/tile-snapshot.md)、[场地的行政区链](references/admin-chain.md)、[按图概括的显示矢量](references/display-vector.md)。
- 图像描绘与赋位：[标注流程](references/raster-annotation.md)、[配准流程](references/raster-georeferencing.md)；仅写标注 patch 时再读[对象契约](references/raster-schema.md)。

## 图像标注与配准

以下仅适用于图像识读任务。模型由用户或上层调度指定，不在 skill 内固定厂商、标识或推理档位；指定模型不可用时停止模型步骤，不静默替换。

默认由模型直接绘制坐标，程序负责裁剪、非生成式增强、坐标换算、版本、叠加、检查与导出。禁用生成式补图；阈值、骨架与程序追线仅在用户明确要求时使用。多局部任务逐处看图、提交和保存，按边或相连边批次保护已提交对象并核对覆盖，不等整图读完再集中输出。

描绘和配准可独立执行；完整任务先保留像素成果，再按图框转换。局部放大沿用原图坐标，独立插图另定义有效区域与变换。完整图幅分别核查类别、延续节点、遗漏面与插图；不自动分片修缝。明确要求的 TPS 研究沿用独立接口与冻结验收，不修改原生标注或仿射流程。配准 `pass` 仅表示通过预先声明的几何门槛，交付仍为待审候选。

## 环境与交付

后续 CLI、测试与子进程使用同一已确认解释器；缺依赖不得静默换算法。安装、升级、同步 installed skill 均须用户授权。macOS 沙箱不默认依赖 `ogrinfo` 或启动 `uv`；已有环境可用时直接使用，具体依赖和 fallback 见运行环境参考。

模板位于 `templates/`，偏好位于 `config/user.yaml`，永久回归位于 `tests/`。运行产物、日志、缓存、大数据及历史证据放仓库外。本地执行不等于数据不会进入远端模型上下文；trace 为显式本地记录，也可能包含路径和业务参数，分享前按数据边界审阅。
