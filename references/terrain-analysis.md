# DEM 地形诊断

`terrain.py` 复用 Rasterio 的显式波段/掩膜读取、结果包原子发布及 SciPy 邻域运算，提供 Horn 3×3 坡度、坡向、3×3 高差和高程分位数。用于既有 DEM 的地形背景诊断，不输出灾害路径、风险等级、汇流或历史河道。

```bash
python terrain.py projected-dem.tif --params terrain.json --output new-result-directory
```

参数示例：

```json
{"schema_version":1,"band":1,"vertical_unit":"metre","vertical_datum":"unknown","source_description":"来源与处理链说明","max_pixels":5000000}
```

垂直单位必须声明为 `metre`、`foot` 或 `us_survey_foot`；不自动猜测。垂直基准和来源必须有文本说明，未知写 `unknown`。需要二维投影 CRS（两轴同一已知线性单位，x 在 y 的顺时针方向；与 CRS 声明的轴序无关，EPSG:4547、3031 都可以）和北向上网格，水平轴单位转为米。坡向和光照方向相对格网北；地面尺度不做改正，范围内的尺度因子写在记录的 `ground_scale`（EPSG:3857 在 60 度约为 2，坡度正切相应偏小）；经纬度或旋转数据先显式调用 `raster.py warp`，连续数据明确重采样方法。投影适用性由调用项目核实，未作投影比例尺改正；坡向以**格网北**为零，顺时针，表示下坡方向。

坡度单位为度，起伏度为邻域最大高程减最小高程（米）；支持非方形像元。平地坡向为 NoData。任何 3×3 邻点缺失或落在图像外均不计算，不插值、不填洼；源高程统计仍包含全部有效源像元。拒绝校准 scale/offset、外部栅格 sidecar、未知参数与超出像元上限；当前全量数组路径不声称流式计算。`max_pixels` 仅限制像元数，非精确内存配额。

可选 `"hillshade":{"azimuth":315,"altitude":45,"z_factor":1}` 另写 `hillshade.tif`：由同一组 Horn 梯度算出的朗伯光照值 0–1（单位 `ratio`，平地等于 sin 高度角），光源方位按格网北顺时针。它只是数值，不带颜色；交给 Composer 时用 `raster.py display` 的 `"mode":"values"`、`"encoding":"value-rgb24"` 写出显示栅格，由主题的晕渲色谱上色。`z_factor` 是唯一的比例调整：在 EPSG:3857 这类放大水平距离的投影上计算时，按中心纬度取约 1/cos(纬度) 并写进参数，记录会保留。

输出 `slope.tif`、`aspect.tif`、`relief.tif`（及可选 `hillshade.tif`）与 `record.json`。发布前逐像元回读、核对 CRS/变换/单位，并检查来源和实现哈希未变；输出目录必须全新。记录包含来源身份、假设、版本、统计、文件哈希、墙钟和本进程生命周期最大 RSS。正式使用仍需检查真实图像与源数据质量。

算法语义参考 [GDAL DEM 文档](https://gdal.org/en/stable/programs/gdaldem.html)；实现使用 [SciPy correlate](https://docs.scipy.org/doc/scipy/reference/generated/scipy.ndimage.correlate.html) 及 min/max filter，未调用 GDAL DEMProcessing，未宣称跨后端逐位一致。

## 等高线与剖面

```bash
python terrain-detail.py dem.tif --params detail.json --output new-detail
```

```json
{"vertical_unit":"metre","vertical_datum":"unknown","source_description":"DEM 来源","levels_m":[500,1000,1500],"profile":{"input":"reaches.gpkg","id":"reach_id","distance_m":100},"max_pixels":5000000,"max_features":100000}
```

`levels_m` 与 `profile` 至少指定一个。当前仅接受单波段米制高程、北向上投影格网；剖面的水平计量复用既有投影单位契约。等高线复用 ContourPy serial，在像元中心插值；一个四边形有缺失角点就排除整个四边形，不向栅格外框外推。必须明确递增、唯一的等高线级别（上限 1000）；空结果也交付有效 GPKG。线 ID 仅在固定输入/参数/后端下复现，不作为跨 DEM 版本地物身份。

剖面复用 `_daily.geometry(method=sample)`，按来源 ID、存储部件、里程和端点关联采样；源记录重排后 sample ID 不变。高程使用包含采样点的像元，不做隐藏插值；`valid/nodata/outside` 分开记录，多部件里程从零重启。输出 `contours.gpkg`、`profile.gpkg` 和记录包，原始河线方向不保证下游方向；DEM 沿线高程不能直接解释为实测河床。

参考：[ContourPy 线格式](https://contourpy.readthedocs.io/en/stable/user_guide/calculate/line_type.html)、[掩膜](https://contourpy.readthedocs.io/en/stable/user_guide/calculate/z_corner_mask.html)。

## 显式 GRASS 水文与视域

```bash
python terrain-backend.py hydrology dem.tif --grass /path/to/grass --params hydro.json --output new-hydrology
python terrain-backend.py viewshed dem.tif --grass /path/to/grass --params views.json --output new-viewshed
```

用户或运行项目提供真实可执行 GRASS launcher；不安装或自动寻找替代算法。每次在仓库外结果目录旁创建独立 location、HOME 和临时目录，完成或失败后删除。记录实际后端版本、模块退出码与资源，不输出任意子进程原始日志。

水文参数示例：

```json
{"vertical_unit":"metre","vertical_datum":"unknown","source_description":"DEM 来源","flow_method":"D8","threshold_cells":100,"outlet":[500005,3100005]}
```

复用 `r.watershed`，明确选择 D8 或 MFD（固定 convergence=5）；AT 最小成本路由，无额外填洼。输出汇流、流向、分区与河网**候选栅格**，不输出观测流量、矢量河网或历史河道。threshold 是外部流域的最小像元数，也影响分区/河网细节。负汇流值保留边界外来水不确定性，不能取绝对值后当作已知精确汇水量。正流向为 1—8，自东北起逆时针每 45°；负值表示离开区域。`outlet` 可选，按包含像元定位；仅 D8 支持其流域提取，MFD 的主流向不能代表全部分流贡献。出口是否落在目标河道由调用项目验证。

视域参数示例：

```json
{"vertical_unit":"metre","vertical_datum":"unknown","source_description":"DEM 来源","observers":[{"id":"A","x":500005,"y":3100005,"height_m":2}],"target_height_m":0,"max_distance_m":5000}
```

复用 `r.viewshed -b`，支持 1—32 个唯一 ID 观察点。坐标必须显式落在像元中心，原位置与吸附位移由调用项目保存；高度非负。输出逐点 0/1 与 `visible_count`，所有观察半径以外为 NoData；采用平面视域，不计算曲率/折射，远距离应用不在本包边界内。DEM/DSM 像元尺度与表面质量限制真实通视结论。

两类后端操作要求单波段、北向上、水平和垂直均为米、**所有像元有效**，默认最多 100 万像元；缺失数据直接拒绝，不能把空洞当作水流或视线障碍。GRASS region 文本往返的角点偏移仅允许 ≤1e-9 像元并记录，之后保留原格网；半像元偏移拒绝。发布前回读整个栅格。子进程 max RSS 记录的是已完成模块子进程的最大值，不冒充并发进程树峰值。

参考：[GRASS 7.8 watershed](https://grass.osgeo.org/grass78/manuals/r.watershed.html)、[outlet](https://grass.osgeo.org/grass78/manuals/r.water.outlet.html)、[viewshed](https://grass.osgeo.org/grass78/manuals/r.viewshed.html)。这些接口已在相应旧版后端验证；其他版本需重新验收。

## 方向性地形成本

无路网累计成本、阻力障碍、候选路径与超额成本走廊使用独立 `terrain-cost.py`，见 [专业分析接口](professional-analysis.md#无路网地形成本terrain-costpy)。坡度诊断与逐边步行成本分开；不可用普通终点出发的各向异性累计成本替代各像元到终点的成本。
