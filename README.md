<div align="center">

# gis-kit

**把常用的 GIS 操作写成脚本，方便 AI agent 调用。**

面向 Claude Code、Codex 等 agent 的 skill。

[![License](https://img.shields.io/badge/license-Apache--2.0-blue.svg)](LICENSE)
[![Python](https://img.shields.io/badge/python-3.10%2B-3776AB.svg?logo=python&logoColor=white)](requirements-tested.txt)
[![Tests](https://github.com/shioo-gis/gis-kit/actions/workflows/tests.yml/badge.svg)](https://github.com/shioo-gis/gis-kit/actions/workflows/tests.yml)
[![Agent Skill](https://img.shields.io/badge/Agent%20Skill-SKILL.md-8A2BE2.svg)](SKILL.md)

中文 · [English](README.en.md)

</div>

---

## 这是什么

这是我自己用的 GIS 数据处理工具箱。我把平时用得上的操作写成脚本，目前有近 50 个，通过统一入口供 agent 发现和组合调用。

```bash
python scripts/gis.py list                         # 工具目录（只需标准库）
python scripts/gis.py list --search cad
python scripts/gis.py cad --help                   # 参数说明
python scripts/gis.py capabilities --json          # 能力与契约探测（只需标准库）
python scripts/gis.py describe parcels.gpkg --json # 只读文件头元数据，不读要素或像元
```

[SKILL.md](SKILL.md) 记下了处理空间数据时的基本判断，脚本也在容易悄悄出错的地方做了限制：

- **暂存、回读校验与原子发布**：输出先写临时文件或暂存目录，回读核对要素数、字段、CRS、几何或像元后才原子发布，`record.json` 与 `manifest.json` 记录输入、脚本实现和产物的 SHA-256。已有输出默认拒绝覆盖；`gpkg.py` 导入或复制遇到同名图层直接拒绝，不静默追加；`split.py` 保留空值行，并在多个值映射到同一文件名时提前拒绝。
- **重投影与量算遇到失真直接报错**：矢量重投影统一检查极点、跨投影接缝和无效坐标，遇到时直接报错并指出行号，不静默翻转或返回空结果；Web 墨卡托栅格重投影自动裁进世界范围。面积和距离量算核查轴单位与地面尺度，Web 墨卡托等尺度偏离过大的投影自动改走局部 UTM，跨带时报错并提示显式指定 `--analysis-crs`。
- **不编造事实，不确定项保留待审状态**：不带带号的 CAD 坐标不猜 EPSG，多值或落在公共边上的文字挂接列为冲突，字段转型保留空值（不写成 `"nan"` 或误转布尔值），编码修复不改动无乱码特征的外文，缺少需求权重时不报告人口覆盖率；未确认的结果保留 `candidate` 或 `hold`。
- **读前内存预算与跨平台一致**：默认按当前可用内存的 80% 做读前估算拦截（`--memory-budget`，见[内存预算](references/memory-budget.md)），大表转换和栅格重采样按块或按窗口读写；参数、清单与管道输出在 macOS、Linux 和 Windows 上固定使用 UTF-8。

我平时用它处理一般规模的任务。数据量更大或需要专业算法时，可以让 agent 调用可选的 DuckDB 空间 SQL，或接入 QGIS、GRASS、PySAL 等后端。

## 现有工具

### 1. 矢量、挂表与指标

- **日常矢量、挂表与指标**：支持裁剪、融合、合并、缓冲（含同心服务圈）、字段整理与版本化结果包（[基础工作流](references/vector-workflows.md)、[日常矢量](references/daily-vector.md)）。`join.py` 除空间关联外，还能按规范化后的地块编号（统一全角半角、连字符与每段前导零）把 Excel 或 CSV 指标表挂到地块上，列出两侧未匹配、重复与撞号（[计量与关联](references/vector-metrics.md)）；`topo.py` 检查空隙、重叠与碎面（[拓扑诊断](references/vector-topology.md)）；`indicators.py` 计算建筑面积、容积率与建筑密度。

### 2. 平台包与样式互转

- **拆包与读取样式**（`package.py`）：`unpack` 先检查归档路径与解压体积，再安全解开 ArcGIS 图层包和地图包（`.lpk`、`.lpkx`、`.mpk`、`.mpkx`），清点数据集并对应图层定义（[ArcGIS 包清点](references/esri-package.md)）；`read` 读取 QGIS `.qml`、ArcGIS Pro `.lyrx` 与 `.stylx` 符号库，转成平台无关的 `map-style` 文档，逐项记下近似或无法转换的符号（[读取平台样式](references/style-read.md)）。
- **按图打包**（`package.py pack`）：把一张图的 `map-style` 连同按图幅选取、裁切或整份保留的数据打成目标平台的包：内嵌图层顺序、QML 样式与预设标注点的 QGIS GeoPackage（[QGIS 打包](references/qgis-package.md)）；含文件地理数据库与 CIM 图层文件的 ArcGIS Pro 图层包 `.lpkx`（[ArcGIS Pro 打包](references/arcgis-pro-package.md)）；或由 ArcObjects 骨架拼装二进制 `.lyr` 的 ArcMap 图层包 `.lpk`（[ArcMap 打包](references/arcmap-package.md)）。

### 3. 栅格、DEM 地形与路网覆盖

- **栅格与地形**（`raster.py`、`terrain.py`、`terrain-detail.py`）：支持重投影、格网对齐、分区统计、栅格化、按窗口局部计算、斑块整理、区域总量守恒分配与 COG 输出（[数值栅格](references/raster-numeric.md)、[栅格整理](references/raster-cleanup.md)）；DEM 计算坡度、坡向、起伏度、数值晕渲、等高线与剖面，水文汇流、视域和各向异性步行成本可选用 GRASS 后端（[地形诊断](references/terrain-analysis.md)、[专业分析](references/professional-analysis.md)）。
- **路网覆盖与空间统计**（`network.py`、`suitability.py`、`spatial-inference.py`）：按源节点拓扑构建有向路网，区分层级、单行、接驳许可区与障碍，计算 OD、可达路段和设施覆盖情景，输出已覆盖、未覆盖、未知三态结果（[路网与需求覆盖](references/network-access.md)）；另支持多情景适宜性评分、高斯核密度与可选的 PySAL 空间自相关检验。

### 4. 大表空间 SQL 与重复执行

- **大表查询**（`spatial-sql.py`）：通过可选的 DuckDB Spatial 执行单条原生 `SELECT` 并直接写出经回读校验的 GeoParquet，避免把大体量明细全量载入 GeoPandas（[Spatial SQL](references/spatial-sql.md)）。
- **轨迹与配方**（`gis.py --trace`、`recipe.py`、`semantic-check.py`）：记录实际运行的步骤与文件指纹，整理成带内容缓存的顺序 recipe 复用于下一批数据；`semantic-check` 检查指定语义字段或 JSON 指针在步骤之间是否丢掉 `unknown`、`candidate`、`hold` 等受保护状态（[Harness](references/harness.md)、[重复交付](references/repeated-delivery.md)）。

### 5. 显示派生与 gis-composer 衔接

gis-composer 是我的另一个项目，专门负责出图：以 MapLibre 为主渲染地图 PNG，按 Storyboard 确定性地渲染地图影片，也可以接 Blender 出 DEM 地形图和影片。它还在私有开发，打算整理好之后再公开，时间没有定。

gis-kit 负责空间计算与诊断检查图，正式地图、Scene / Storyboard 和地图影片交给 gis-composer。两边只通过文件与 JSON 契约交接，互不导入代码；没有 gis-composer 时，`plot.py`、`cartographic-map.py`、`atlas.py` 和 `plot-chart.py` 可独立生成兜底地图、对象图册和统计图表。配合 gis-composer 时，gis-kit 把不含颜色的分类方案（`scheme.py`）、数值编码显示栅格（`raster.py display`）、按出图视角共边概括的显示矢量（[display-vector](references/display-vector.md)）、场地逐级行政区链（[admin-chain](references/admin-chain.md)）及白名单瓦片快照（[tile-snapshot](references/tile-snapshot.md)）原子发布到项目工作区的 `inputs/`，详见 [Composer 显示交付](references/composer-display.md)。

### 6. 扫描地图的语义矢量化与配准（实验性）

由多模态模型直接读图并在原图像素空间绘制点、折线与贝塞尔曲线，`raster-annotate.py` 负责局部裁剪、坐标换算、增量版本、几何检查、叠加审核包与导出，未闭合或无效的面不会自动补全。所有产物都是待审候选，需要人工审核。描绘与配准是两个独立步骤：未配准成果保留在无地理 CRS 的像素工程空间，不会加上虚构经纬度当作地理数据发布；需要地理坐标时，另用 `raster-georef.py` 按控制点拟合与独立检查点验收，已有描绘成果可直接转换而无需重画。详见[标注流程](references/raster-annotation.md)、[对象与记录契约](references/raster-schema.md)和[地图配准](references/raster-georeferencing.md)。

### 7. 湘源控规图纸转地块（专项适配）

这一部分比较小众，是专门针对**湘源控规**的图纸做的适配（`cad.py`，见 [CAD 图纸处理](references/cad-intake.md)），不是通用的 CAD 转 GIS 工具。其他软件出的图纸只能走下面通用的提取与围面路径，每张图都要自己确认，不保证开箱可用。

- **湘源控规图则**：`xiangyuan` 直接读取地块多段线扩展数据里的用地代码等属性，同时取出图内嵌入的 Excel 指标表（OLE）和 CAD 表格。
- **通用 DXF 路径**：调用本机已装的 ODA File Converter 或 LibreDWG 把 DWG 转成 DXF；体检图层、单位、离群图框并比对候选 CRS；剔除图框图例后按显式容差提取、围面、按 YAML 规则挂接文字，并用 `compare-fills` 对照填充面。
- **坐标与检查**：坐标系确认后用 `assign-crs` 赋 CRS（无带号坐标须先通过参考边界或已知点核对）；城市独立坐标用 `vector-georef.py` 做四参数或仿射转换（[矢量控制点转换](references/vector-georeferencing.md)）；最后用 `overlay` 叠合已供地等参考图层量出交集面积并出检查图。

## 配套：gis-plugin

gis-kit 本身只管数据处理，不碰 agent 的运行环境。跨工具、跨引擎的事，我放在另一个项目 gis-plugin 里做：它是一层很薄的外壳，不含任何读写 GIS 数据的代码，需要数据时调用 gis-kit 等引擎的命令行。目前做了这些：

- **hooks**：agent 读到 GIS 数据时自动注入元数据卡片和字段语义；保护登记过的源数据，不让命令覆盖或改写；项目里有样式约定时提示它在哪里；检查刚写出的 SVG、PPTX、HTML 里有没有防御、解说、免责一类用语。hooks 注入的是事实而不是规则清单，只用 Python 标准库，Claude Code 和 Codex 通用。
- **约定与诊断**：引擎之间交换文件的格式，以及检查引擎是否装好、版本是否兼容的 `gis-doctor`。
- **评测**：比较有无外壳时 agent 完成 GIS 任务的差别。

gis-plugin 还在私有开发。我打算继续完善，整理好之后再公开，时间没有定。gis-kit 不依赖它，单独使用完整可用。

## 快速开始

**1. 安装**：克隆到 agent 的 skills 目录，目录名保持 `gis-kit`。

```bash
# Claude Code
git clone https://github.com/shioo-gis/gis-kit.git ~/.claude/skills/gis-kit
# Codex
git clone https://github.com/shioo-gis/gis-kit.git ~/.codex/skills/gis-kit
```

**2. 准备 Python 环境**（Python ≥ 3.10）

```bash
python3.10 -m venv ~/.venvs/gis-kit
~/.venvs/gis-kit/bin/python -m pip install -r ~/.claude/skills/gis-kit/requirements-full.txt
~/.venvs/gis-kit/bin/python ~/.claude/skills/gis-kit/scripts/daily.py environment
```

**3. 直接对 agent 说**

> 把 `指标表.xlsx` 按地块编号挂到 `parcels.gpkg` 上，列出两边没对上的编号，再检查地块有没有重叠和空隙。
>
> 把这个 `.lpkx` 解开清点里面的图层和数据；再把 `example.map-style.json` 分别打成 QGIS 和 ArcGIS Pro 的包。
>
> 算出这块 DEM 的坡度和坡向，统计每个行政区的平均坡度。
>
> 给这几个医院算 15 分钟车程覆盖，按小区人口权重汇总覆盖率，列出未覆盖和通行未知的小区。

agent 会读 [SKILL.md](SKILL.md)，按需查看 `references/` 里的参考文档，自己选工具、设参数并回读结果。

## 环境与依赖

1. `requirements-core.txt`：矢量、栅格、量算和检查图所需的核心依赖。
2. `requirements-full.txt`：在核心依赖之外加入 CAD 图纸（`ezdxf`、`olefile`）、Excel 表格（`openpyxl`）、路网、统计、图像增强和测试工具。
3. `requirements-tested.txt`：记录 Python 3.10 下完整回归测试通过时的精确版本。
4. `requirements-spatial-sql.txt`：可选的 DuckDB 空间 SQL 依赖。

如果 pip 安装 GDAL 相关包有困难，可以用 `environment.yml` 创建 conda-forge 环境。agent 在同一任务中应始终使用同一个 Python 解释器，细节见[运行环境](references/runtime-environment.md)。

外部程序与专业后端按需独立安装：DWG 转 DXF 调用本机的 ODA File Converter（可用 `GIS_KIT_ODA` 指定路径）或 LibreDWG `dwg2dxf`；7z 格式图层包读写使用系统自带的 `bsdtar` 或 7-Zip；QGIS、GRASS 和 PySAL 为可选后端。安装了 gis-composer 时，通过 `GIS_COMPOSER_HOME` 指定其位置。

中文图面按 `config/user.yaml` 中的顺序选择第一个已安装的字体，默认是 Noto Sans CJK SC。如果列表中的字体都未安装，会回退到 DejaVu Sans，中文将无法正常显示。

## 测试

```bash
PYTHONDONTWRITEBYTECODE=1 python -m pytest -q -p no:cacheprovider tests
```

`tests/test_*.py` 是自包含的回归测试，使用合成数据或手算数据。

`tests/run_*_acceptance.py` 用真实文件检查完整处理链。运行前需要准备 OSM、Natural Earth 或 DEM 等外部数据，并把输出目录放在仓库之外。每个脚本开头列出了所需输入。

## 数据与隐私

脚本在本地运行，agent 读取的内容可能会进入远端模型的上下文。`--trace` 记录包含文件路径和任务参数，分享前请检查。

仓库不附带完整的第三方数据集。仓库中的测试图使用了谭其骧《中国历史地图集》的局部裁片，底图版权归原权利人，不适用本仓库的代码许可证。使用 OSM（ODbL）、Natural Earth（公有领域）及在线瓦片服务时，请遵守各自的许可、缓存条款和署名要求。

## 许可证

[Apache License 2.0](LICENSE)

<sub>关键词：GIS · geospatial · AI agent · Claude Code skill · Codex skill · Agent Skills · LLM · CAD · DXF · land use · QGIS · ArcGIS · GeoPackage · GeoPandas · Rasterio · GDAL · Shapely · DuckDB · DEM · terrain analysis · network analysis · georeferencing · 空间分析 · 城乡规划 · 地理信息 · 地图配准</sub>
