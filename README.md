<div align="center">

# gis-kit

**把常用的 GIS 操作写成脚本，方便 AI agent 调用。**

面向 Claude Code、Codex 等 agent 的 skill。

[![License](https://img.shields.io/badge/license-Apache--2.0-blue.svg)](LICENSE)
[![Python](https://img.shields.io/badge/python-3.10%2B-3776AB.svg?logo=python&logoColor=white)](requirements-tested.txt)
[![Tests](https://github.com/shio-vinyl/gis-kit/actions/workflows/tests.yml/badge.svg)](https://github.com/shio-vinyl/gis-kit/actions/workflows/tests.yml)
[![Agent Skill](https://img.shields.io/badge/Agent%20Skill-SKILL.md-8A2BE2.svg)](SKILL.md)

中文 · [English](README.en.md)

</div>

---

## 这是什么

这是我自己用的 GIS 工具箱。我把平时用得上的操作写成脚本，目前有 50 多个。

这些脚本有一个统一入口，agent 可以列出和搜索工具，通过 `--help` 查看参数，再按任务组合调用。

```bash
python scripts/gis.py list                # 工具目录（只需标准库）
python scripts/gis.py list --search raster
python scripts/gis.py raster --help       # 参数说明
```

[SKILL.md](SKILL.md) 记录了处理数据时的基本要求：确认 CRS、单位和 NoData，保留 `candidate` / `hold` 等状态。具体选哪个工具、怎样衔接，由 agent 根据任务决定。

我平时用它处理一般规模的任务。数据量较大或对性能有更高要求时，可以让 agent 调用 QGIS、GRASS 等 GIS 后端，大表也可以使用可选的 DuckDB 空间 SQL。

我会继续按实际需要补充工具，也会保留一些研究性的功能。

## 现有工具

1. 矢量处理支持裁剪、融合、合并、空间关联和缓冲，也有字段与拓扑检查修复、面积与城市指标计算。日常结果可以打包保存，并记录版本。按地块编号可以把 Excel 指标表挂到地块上，编号的全角、连字符和前导零差异会统一处理。
2. CAD 规划图纸可以转成 GIS 地块。具体来说：
   - DWG 用本机已装的 LibreDWG 或 ODA 转成 DXF；
   - 体检时清点图层、单位和离群图框，并按坐标量级或参考图层列出候选坐标系；
   - 通用图纸按显式容差提取实体、围成面、挂上文字，填充转成面后和围面结果对照；
   - 湘源控规图则直接读地块的扩展数据属性，图里嵌入的 Excel 指标表和 CAD 表格一并取出；
   - 坐标系确认后赋 CRS，局部坐标可以用控制点转到国家坐标；
   - 成果地块和已供地、更新范围等参考图层叠成检查图，量出交集面积。

   ArcGIS 图层包和地图包（.lpk、.mpk）可以安全地拆开，清点包里的数据和图层。反过来，一张图的 `map-style`（gis-composer 导出）可以连同数据打包成一个 GeoPackage：数据按图幅选取、裁切或整份保留，样式写成 QML，标注位置写成点图层，并内嵌排好顺序的 QGIS 工程，同事在 QGIS 里打开就和成图一致；也可以打成 ArcGIS Pro 的图层包（.lpkx），数据在文件地理数据库里，样式写成 CIM 图层文件；或者打包成 ArcMap 图层包（.lpk），二进制图层文件由 ArcObjects 写的骨架拼成，渲染出的颜色和请求的逐字节相同。
3. 栅格与地形工具支持分区统计、重投影和 COG 输出，大栅格按窗口处理。DEM 可计算坡度、坡向、等高线和剖面；水文、可视域和地形成本可选用 GRASS 后端。
4. 路网工具处理有向路网、OD 和设施覆盖情景，也支持需求加权覆盖。缺少需求权重时，不报告人口覆盖率。
5. 栅格图像的语义矢量化由模型直接识图并绘制点、线、面，程序负责坐标换算、版本记录、几何检查与导出。我主要用历史地图做研究，目前推荐 GPT-6 Astra 和 GPT-6.1，过程和测试情况见下文。
6. 重复任务可以用 `--trace` 记录实际执行的步骤，整理成 recipe 后用于下一批数据。`semantic-check` 检查指定的语义字段在步骤之间是否丢失或改变。

## 栅格图像的语义矢量化

gis-kit 有一套让多模态模型直接读栅格图、在图上绘制点线面的流程。识别目标、选择线的走向、确定面的范围由模型完成；裁剪、坐标换算、版本记录、几何检查、生成叠加对照图和导出文件由程序完成。

模型输出的坐标直接写在原图的像素坐标系中。折线和三次贝塞尔曲线都由模型绘制，控制点原样保存。如果某条曲线是程序对折线平滑后得到的，我会单独注明，不会把它算作模型画的。

目前主要用历史地图做研究和测试。

### 起因

过去把栅格地图转成矢量基本靠人工描绘，模型能做的很有限。GPT-6 Astra 之后，GPT 系列在识图和标注上的进步很明显，我才开始认真尝试这件事。

测试材料有四份：谭其骧《中国历史地图集》中两张 850×800 的局部裁片，斯坦福数字地图收藏中 1926 年的法国西北部地图，1789 年的黑森地图，以及 1829 年的美国罗德岛州图。

从测试结果看，模型已经能完成这项工作，**主要障碍是费用太高**，没法大量使用。

目前我使用并推荐的是 GPT-6 Astra 和 GPT-6.1。更早的 5.6 系列效果很差，Luna 仍不可靠，6.0 Sol 也没能用起来。这里说能完成，指的是模型能给出一份值得人工检查、也能在此基础上继续修改的矢量草稿；我没有测过准确率，所有结果都需要审核。

6.1 Sol 发布后，从这段时间的使用情况看，它能比较稳定地完成整套流程，费用比 Astra 低了一大截。我还没有做系统测试，这里先作为观察记录，之后补充。

![Astra 与 GPT-5.6 系列在同一地图裁片上的直接描绘对比](assets/vectorization/astra-vs-5.6.png)

*早期模型对比：同一裁片、各条件单次运行，保留未经修正的预测几何。点线数量不代表准确率；完整图可点击放大。*

### 测试中发现的问题

1. 聚落点位大多能对应上，界线走向一直画得不好。
2. 把矢量叠回原图给模型看，再让它修改一次，能纠正一部分明显绕错的线。改用贝塞尔曲线绘制后，我没有看到比折线更贴合原图。
3. 几何检查只能发现几何错误。一条绕了大弯的线同样可以通过自交检查。
4. 分片之间共用节点可以保证接缝相连，连接位置是否正确仍要人工判断。我遇到过两个分片引用同一个节点、一起连到错误位置的情况。
5. 完整图幅还处理不好，漏画、错接、复杂岸线、插图和面覆盖都需要逐项审核。有一次整幅图的独立审核没有通过，我因此暂停了补绘。
6. 在两张裁片上比较了六个思考强度档位，共做了十二次单轮提取。低档适合粗略描出几何形状，高档在区分历史和现代对象、覆盖范围上表现更好，再往上没有哪一档持续更好。
7. 为了降低费用，我把逐边提交改成按交汇点分组提交。初始提交次数从 39 次减少到 24 次，模型请求次数却从 55 次增加到 63 次，token 用量也增加了。

![原图、模型初稿与一次叠加审核后的局部修订对比](assets/vectorization/local-revision.png)

*Astra / low 局部修订：从左到右为原图、初稿和一次修订后的结果。明显错绕得到纠正，局部偏差仍然存在，结果保留为待审候选。*

### 流程说明

识图和提交都按局部进行，每个局部单独保存为一个 patch，出错时只需要回到对应的那一块。

线在实际交汇处断开并共用节点，面由一圈有方向的共享边构成外环和孔洞。形状和连接关系分开修改，返修时可以只改其中一小块。

程序会检查自交、重叠、越界和面的有效性，语义上的遗漏它发现不了。审核包按编号生成，人工反馈分为通过、拒绝和待审三种状态。

导出格式包括标注 JSON、保留原生曲线的 SVG，以及点线面 GPKG。未闭合或无效的面会列入遗漏清单，程序不会自动补全。

描绘和配准是两个独立的步骤。未配准的成果保留在像素工程空间，不带地理 CRS，**我不会给它加上经纬度后当作地理数据发布**。需要地理坐标时，另外用控制点配准，并记录独立检查点误差、验收状态和交接清单。配准通过只表示达到了事先声明的几何标准，图上对象的位置和历史归属是否正确，需要另外判断。配准后，已有的描绘成果可以直接使用，不需要重画。

### 使用建议

1. 绘制阶段不建议默认使用高思考强度。如果需要模型更仔细地判断，放在审核阶段更合适。高强度审核的效果我也没有做过对照实验，这条只是工作流上的经验。
2. 审核不要变成无限次的自审和返修。我只把四种情况作为阻断条件：漏掉整条边、连接到错误对象、漏掉重要地块、接缝断开。其他问题先记录下来，优先修改影响使用的部分。
3. 现有测试基本是小样本、单次实验，可以用来讨论可用性、贴合程度、覆盖范围和错接情况，还不足以说明准确率。

<details>
<summary>查看 GPT-6.1 六档思考强度对比（福建路裁片）</summary>

![GPT-6.1 六档思考强度的描绘结果，以及 Sol 6.0 历史结果](assets/vectorization/gpt-6.1-all-efforts.png)

*2026-09-30：同一 850×800 裁片，六档各进行一次独立单轮提取，未经修补。图中另附 Sol 6.0 / high 的 9 月 28 日结果。耗时包含排队等因素，输出 token 包含思考 token；这组小样本不提供准确率或严格速度排名。完整图可点击放大。*

</details>

自动分片和接缝合并还没有实现。所有产物都应视为待审候选，请不要当作成品直接使用。

具体操作见[标注流程](references/raster-annotation.md)、[对象与记录契约](references/raster-schema.md)和[地图配准](references/raster-georeferencing.md)。

## 快速开始

**1. 安装**：克隆到 agent 的 skills 目录，目录名保持 `gis-kit`。

```bash
# Claude Code
git clone https://github.com/shio-vinyl/gis-kit.git ~/.claude/skills/gis-kit
# Codex
git clone https://github.com/shio-vinyl/gis-kit.git ~/.codex/skills/gis-kit
```

**2. 准备 Python 环境**（Python ≥ 3.10）

```bash
python3.10 -m venv ~/.venvs/gis-kit
~/.venvs/gis-kit/bin/python -m pip install -r ~/.claude/skills/gis-kit/requirements-full.txt
~/.venvs/gis-kit/bin/python ~/.claude/skills/gis-kit/scripts/daily.py environment
```

**3. 直接对 agent 说**

> 把 `parcels.gpkg` 按用地类型融合，用 CGCS2000 投影算面积，出一张检查图。
>
> 算出这块 DEM 的坡度和坡向，统计每个行政区的平均坡度。
>
> 给这几个医院算 15 分钟车程覆盖，列出覆盖不到的小区。
>
> 把这张扫描老地图上的道路描出来，用我给的控制点配准。

agent 会读 [SKILL.md](SKILL.md)，按需查看 `references/` 里的参考文档，自己选工具和参数。

## 环境与依赖

1. `requirements-core.txt` 包含矢量、栅格和检查图所需的核心依赖。
2. `requirements-full.txt` 在核心依赖之外加入路网、统计、图像增强和测试工具。
3. `requirements-tested.txt` 记录 Python 3.10 下完整测试通过时使用的精确版本。
4. `requirements-spatial-sql.txt` 提供可选的 DuckDB 空间 SQL 依赖。

如果 pip 安装 GDAL 相关包有困难，可以用 `environment.yml` 创建 conda-forge 环境。

agent 运行时应始终使用同一个 Python 解释器，细节见[运行环境](references/runtime-environment.md)。QGIS、GRASS 和 PySAL 是可选后端，需要单独安装。

中文图面按 `config/user.yaml` 中的顺序选择第一个已安装的字体，默认是 Noto Sans CJK SC。如果列表中的字体都未安装，会回退到 DejaVu Sans，中文将无法正常显示。

正式地图、Scene / Storyboard 和地图影片由我的另一个项目 gis-composer 负责。它暂时还没有公开，我想再整理一段时间。

gis-kit 不依赖 gis-composer，可以独立生成诊断图和检查图。安装了 gis-composer 时，通过 `GIS_COMPOSER_HOME` 指定它的位置。

## 测试

```bash
PYTHONDONTWRITEBYTECODE=1 python -m pytest -q -p no:cacheprovider tests
```

`tests/test_*.py` 是自包含的回归测试，使用合成数据或手算数据。

`tests/run_*_acceptance.py` 用真实文件检查完整处理链。运行前需要准备 OSM、Natural Earth 或 DEM 等外部数据，并把输出目录放在仓库之外。每个脚本开头列出了所需输入。

## 数据与隐私

脚本在本地运行，agent 读取的内容可能会进入远端模型的上下文。`--trace` 记录包含文件路径和任务参数，分享前请检查。

仓库不附带完整的第三方数据集。文档测试图使用了谭其骧《中国历史地图集》的局部裁片，底图版权归原权利人，不适用本仓库的代码许可证。使用 OSM（ODbL）、Natural Earth（公有领域）等数据时，请遵守各自的许可和署名要求。

## 许可证

[Apache License 2.0](LICENSE)

<sub>关键词：GIS · geospatial · AI agent · Claude Code skill · Codex skill · Agent Skills · LLM · GeoPandas · Rasterio · GDAL · Shapely · DuckDB · DEM · terrain analysis · network analysis · georeferencing · 空间分析 · 地理信息 · 地图配准</sub>
