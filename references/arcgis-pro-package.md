# 打包成 ArcGIS Pro 图层包（.lpkx）

`package.py pack --format arcgis-pro` 读一份 [`map-style`](https://github.com/shioo-gis/gis-plugin/blob/main/contracts/README.md#map-style) v1 和它引用的数据，写出一个 ArcGIS Pro 图层包（`.lpkx`）。同事把它拖进 Pro 的地图，得到一个按面板、分组排好的组图层，颜色、类别、筛选和标注位置与 gis-composer 出的 PNG 一致。QGIS 的输出见 [qgis-package.md](qgis-package.md)，两者共用同一套取数逻辑。

```bash
python package.py pack maps/example.map-style.json --output /work/send/example-pro --format arcgis-pro
python package.py pack maps/example.map-style.json --output /work/send/example-pro --format arcgis-pro --lyrx --clip
```

- **数据范围**（默认按图幅选取 / `--clip` / `--full`、`--margin`）、**数据路径**（`dataRoot`、`@根名/…`、`--data-root`、`data.sha256`）和瓦片的处理与 QGIS 输出完全相同，见 [qgis-package.md](qgis-package.md) 的“数据范围”“数据路径”两节。
- 代码按“map-style → 某个平台”的输出器组织：`package.py` 的 `prepare` / `store` 负责读取、校验、解析路径和按图幅写数据，`--format` 只决定样式和容器由哪个输出器写：`_qgis_style.py`（QML、QGIS 工程）或 `_arcgis_style.py`（CIM）。再加平台时另写一个输出器即可。

## 写出什么

新目录（原子发布，已存在就报错）：

- **`<id>.lpkx`**：图层包，发给同事的就是它。
- **`<id>.lyrx`、`<id>.gdb/`、`rasters/`**（加 `--lyrx` 时）：同一个图层文件放在解开的数据旁边，不用解包就能在 Pro 里“添加数据”。数据源是相对图层文件的路径，三者要放在同一个文件夹里。
- **`package.json`**（`manifest: arcgis-pro-package` v1）：清单，见下文。
- **`record.json`**：运行记录，含本机绝对路径，不必发给同事。

### `.lpkx` 的结构

以 ArcGIS Pro 3.4.3 自己用“打包图层”（`PackageLayer`）打出的包为准（虚构数据，`version='CURRENT'`）：

```
<名>.lpkx                 7z 压缩包（文件头 7z BC AF 27 1C），不是 zip
├── commondata/<名>.gdb/   打进包的数据（文件地理数据库）
├── esriinfo/item.pkinfo   包信息：ID、名称、<version>13.4</version>、<type>Layer Package</type>
├── esriinfo/iteminfo.xml  条目信息：标题、摘要、标签、类型关键字、经纬度范围
├── esriinfo/thumbnail/thumbnail.png
└── p30/<名>.lyrx          图层文件（CIM JSON，"version": "3.0.0"），数据源写成 DATABASE=..\commondata\<名>.gdb
```

- 图层文件放在 `p30/`（ArcGIS Pro 3.x），里面的 CIM 版本是 3.0.0，即 Pro 3.0 起都能读；Pro 在别处另存的 `.lyrx` 写的是安装版本（3.4.0），打进包时降到 3.0.0。
- ArcMap 的图层包（`.lpk`）是另一种布局：`v10/`、`v105/` 等版本文件夹里放 `.lyr`，见 [esri-package.md](esri-package.md)。
- gis-kit 照此写：`commondata/<名>.gdb`、`commondata/rasters/*.tif`、`p30/<名>.lyrx`、`esriinfo/item.pkinfo`（`version` 写 13.0，与 `p30`、CIM 3.0.0 对应）、`esriinfo/iteminfo.xml`；不写缩略图。用 bsdtar（libarchive，macOS 自带）写 7z，没有时用 7-Zip（`7z`、`7za`、`7zz`），两者都没有就报错。Pro 3.4.3 的 `ExtractPackage` 能解开，解出的图层全部连得上数据（见“验证”）。
- **包 ID 随内容变化**：Pro 按包 ID 把包解到 `文档\ArcGIS\Packages\<名>_<短哈希>`，再遇到同一个 ID 就直接用这份旧副本，`ExtractPackage` 换了输出文件夹也一样（Pro 3.4.3 实测）。所以 `item.pkinfo` 的 ID 由图名和包内所有文件的哈希算出：改了样式或数据重新打包，同事打开的就是新内容。这个缓存目录里的旧副本 Pro 不会自己清理。

## 数据

- **矢量写进文件地理数据库**（GDAL 的 OpenFileGDB 驱动），而不是 shapefile：字段名不截成 10 个字符、中文字段名和值不用操心编码、NULL 能保留、没有 2 GB 上限，Pro 读回最可靠。每份数据一个要素类，同一份数据被多个图层引用时只存一份，与 QGIS 输出相同。数据保持原坐标系。
- 文件地理数据库有几处和源数据不同，都写进清单：
  - 与 SQL 关键字同名的字段会被改名（如 `select` → `select_`）。图层文件里的渲染字段、定义查询和标注表达式一律用改后的名字；清单 `datasets[].renamed_fields` 列出对照，`warnings` 提示。
  - 64 位整数存成双精度（Pro 3.2 之前没有 64 位整数），`warnings` 逐字段提示；布尔值存成 0/1 小整数，筛选里的 `true`/`false` 相应写成 1/0。
- **栅格存成 GeoTIFF**，放在 `commondata/rasters/`（GDAL 不能往文件地理数据库里写栅格）。按图幅取窗口，与 QGIS 输出相同。
- **标注点、引线和背景**也写进同一个地理数据库：`map_labels`、`map_label_leaders`、`map_background`。它们用地图的坐标系（底图之外用得最多的那个，与 QGIS 工程坐标系的取法相同），这样把图层文件加进空地图时，Pro 的地图坐标系就是这个，不会因为标注点是经纬度而变成 WGS 84。

## 图层文件

一个 `.lyrx` 里只有一个顶层组图层，名称为 `map-style` 的 `title`（或 id）：

- **面板**：有 `panel` 时先按面板分组，主面板（`extent.panels` 的第一个）显示，其余面板组不显示（插图和主图是同一片地方，同时显示会叠画两遍）。面板组里再按 `group` 分组；没有 `panel` 的图层放在面板组之后。CIM 列表是上层在前，map-style 是下层在前，写的时候倒过来。图层文件没有地图主题和书签，切换插图要手动开关面板组。
- **背景**：顶层 `background` 写成最下面的 `background` 图层（`map_background` 要素类，覆盖图幅和各面板范围的面），颜色和 `opacity` 照写。图层文件不能设置地图背景色，所以用一个图层代替。
- 图层名取 `title`，其次 `legend.title`，再次图层 id；`visible: false` 的图层不显示；`opacity` 写成图层透明度。

## 样式怎么转

尺寸 px → pt（1 px = 0.75 pt），颜色 `#RRGGBB` 加透明度写成 `CIMRGBColor`（透明度 0～100）。CIM 的符号层上层在前，所以面的描边写在填充前面。

| map-style | ArcGIS Pro（CIM） |
|---|---|
| `single` | `CIMSimpleRenderer` |
| `categorized` | `CIMUniqueValueRenderer`，`fields` 为分类字段。`visible: false` 的类写成不可见的类。`default` 写成“所有其他值”（`useDefaultSymbol`、`isDefaultSymbolVisible`），另加一个值为 `<Null>` 的类用同一个符号：Pro 的“所有其他值”不含 NULL（实测），map-style 的 `default` 含 |
| `graduated` | 各档首尾相接、全部可见、没有 `default` 时写成 `CIMClassBreaksRenderer`（`GraduatedColor`，`minimumBreak` 为第一档下限）。map-style 左闭右开、最后一档右闭；CIM 每档是（上一档上限, 本档上限]，所以除最后一档外，上限取下一档下限之前最近的**单精度**数：Pro 按单精度比较，双精度的前一个数会让分界值落进下一档之前的那一档（实测）。有缺口、隐藏档或 `default` 时改写成下面的规则渲染，清单里注明 |
| `rule` | `CIMUniqueValueRenderer` 加 Arcade 取值表达式：依次 `if (条件) { return "1" }`…，第一个命中的规则给出类号，与 map-style 的“先命中先用”完全一致；一个图层、一个图例，`visible: false` 的规则写成不可见的类。`default` 是表达式最后返回的 `"default"` 类；`default: null` 时返回 null，没有类接住，不画。选这种写法而不是“每条规则一个带定义查询的图层”，是因为后者要在每条查询里排除前面所有规则，图例拆散，且 Pro 里改样式要改多个图层 |
| `renderer: null` | 简单渲染器配一个空符号（不画要素），只出标注 |
| 面的 `fill` / `stroke` | `CIMPolygonSymbol`：`CIMSolidStroke` + `CIMSolidFill`。线帽 `butt`/`round`/`square` → `Butt`/`Round`/`Square`，拐角 `miter`/`round`/`bevel` → `Miter`/`Round`/`Bevel` |
| `fill.pattern` | `CIMSolidFill` 之上的 `CIMHatchFill`：`rotation` 同样按逆时针，`separation` 为线距（pt），线写在 `lineSymbol` 里；`crossHatch` 是两层，角度差 90°。`fill.opacity: 0` 时只见线。点、线图层和标注底框不画图案，列入 `unsupported` |
| `dash` | 描边上的 `CIMGeometricEffectDashes`，`dashTemplate` 为 pt；奇数个数值按 SVG 规则重复一遍。标记轮廓和标注底框的虚线也照写 |
| 线的 `stroke` | `CIMLineSymbol` + `CIMSolidStroke` |
| 点的 `marker` | `CIMPointSymbol` + `CIMVectorMarker`（-2～2 的画框，`size` 为直径）：`circle` 用曲线环（与 Pro 默认圆点相同），`square`、`triangle`、`diamond`、`arrow` 为多边形，`ring` 为只有描边的圆；`cross`、`x` 有 `fill` 时为实心十字（`x` 转 45°），只有 `stroke` 时为线；`chevron` 为折线。只用线画的形状没有 `stroke` 时取 `fill` 的颜色。标记里的线宽是绝对的 pt：不写 `scaleSymbolsProportionally`，否则 Pro 会把线宽再乘以“标记尺寸 / 画框高度”（实测十字粗了一倍）。`rotation`（顺时针）取负写入 CIM（逆时针）。点图层的符号没有 `marker` 时画 6 px 圆并列入 `unsupported` |
| `marker.rotationField` | 符号属性连接（`CIMSymbolReference.primitiveOverrides`，`propertyName: Rotation`，指向标记的 `primitiveName`），值为 Arcade：`-(rotation + 字段)`，NULL 和非数值不加。不用渲染器上的 `CIMRotationVisualVariable`：可视化变量管整个渲染器，而 map-style 每一类有自己的标记。Pro 3.4.3 出图核对过点和沿线两种情况 |
| `shape: "svg"` | `CIMPictureMarker`。先核对 `marker.svg.sha256`，不符就拒绝打包。图片以 `data:image/svg+xml;base64,…` 写进符号，所以就在图层文件里，`.lpkx` 和 `--lyrx` 都不需要另外的文件；清单 `icons[]` 列出来源、哈希和尺寸。CIM 的 `size` 是高，map-style 的是较长边，横长的图按比例写低。按 SVG 自身颜色画。横长的图配比例圆时，可视化变量控制的是高而不是较长边，列入 `unsupported`（`kind: approximated`） |
| `marker.placement: "line"` | 线符号（和面符号的边线）里带 `markerPlacement` 的标记：`CIMMarkerPlacementAlongLineSameSize`，`placementTemplate` 为 `spacing`，`offsetAlongLine` 为半个间距，`endings: NoConstraint`（不为凑整改间距）；`alongLine` 对应 `angleToLine`。Pro 按线的角度转标记，这时标记的 0° 横在线上，而 map-style 的 0 顺着线，所以多转 90°。标记的颜色取符号的 `fill`、`stroke`；`stroke` 的虚线只留在线上。线和面图层上没写 `placement: "line"` 的 `marker`、点图层上写了 `placement: "line"` 的都列入 `unsupported`。面的边线在文件地理数据库里是顺时针环，起点也可能和源数据不同，所以标记落在边线上的具体位置可能和 QGIS 不同，间距相同 |
| `size`（比例圆） | 渲染器上的 `CIMSizeVisualVariable`（`Graduated`）：Arcade 取值 `$feature["字段"]`（`sqrt` 时为 `Sqrt(...)`），最小、最大值为 `domain`（`sqrt` 时取平方根），最小、最大尺寸为 `range` 的直径（pt）。这与 map-style 的公式完全相同（`sqrt` 对 √v 线性），并且总是截断；`clamp: false` 列入 `unsupported` |
| `sortBy` | 图层的 `featureSortInfos`，`asc` → `Ascending`（先画小值） |
| `opacity` | 图层透明度（`transparency` = (1 − opacity) × 100） |
| `filter` | 要素表的定义查询（`definitionExpression`，另写一条同名的 `definitionFilterChoices`），渲染和标注都只看到筛过的要素 |
| 栅格 `pseudocolor` `linear` | `CIMRasterStretchColorizer`：最小最大值拉伸，色带为多段线性色带（`CIMMultipartColorRamp`），每段权重按相邻两个值的间距，统计值写成第一个和最后一个值（`statsType: GlobalStats` + `stretchStats`；只写自定义最小最大值时 Pro 不用，实测）。`stretch` 不重算，列入 `unsupported` |
| 栅格 `pseudocolor` `discrete` | `CIMRasterClassifyColorizer`：每个颜色从它的值用到下一个值之前（上限同样取单精度的前一个数），第一个颜色也用于更小的值，最后一档开放到数据最大值 |
| 栅格 `pseudocolor` `exact`、`palette` | `CIMRasterUniqueValueColorizer`（按 `Value`，只适用于整数栅格；`exact` 列入 `unsupported`） |
| 栅格 `rgb` | `CIMRasterRGBColorizer`，按打包窗口每个波段的最小、最大值拉伸（同样写成统计值） |

**筛选转定义查询（文件地理数据库 SQL）**：字段名是普通标识符时直接写，否则加双引号；字符串单引号转义；布尔值写 1/0。NULL 的处理和 MapLibre 一致，而且每个条件都是“非真即假”：`==`、比较写成 `字段 IS NOT NULL AND …`，`!=`、`notIn` 写成 `字段 IS NULL OR …`，所以外面套 `NOT`、`AND`、`OR` 时不会遇到 SQL 的“未知”。`{"expression": …, "dialect": "sql"}` 原样使用；其他方言（含 `qgis`、`arcade`）用作图层筛选时不筛，列入清单。

**规则转 Arcade**：同样的 NULL 规则（`IsEmpty`），`in` 用 `Includes`；字段写 `$feature["字段"]`，中文字段名可以直接用。`{"expression": …, "dialect": "arcade"}` 原样使用；其他方言（含 `qgis`、`sql`）的规则跳过（不匹配任何要素，也不影响后面的规则），列入清单。

## 标注

- **字体**：与 QGIS 输出相同，主题别名从 `fonts[].names` 取实际字体族（`fontFamilyName`）和样式（`fontStyleName`，如 `Bold`）；同一别名有多个条目时按 `textStyle.weight` 选，规则和报告见 [qgis-package.md](qgis-package.md) 的“字体”；没有 `names` 的别名原样写入，列入 `unsupported`。同事电脑上缺字体时 Pro 会换成别的字体，`warnings` 里提示。
- **文字样式**：字号、颜色、`opacity`，晕边（`haloSize`、`haloSymbol`，含晕边 `opacity`），底框 `box` 写成 `CIMBackgroundCallout`（`padding` 的三种写法都换成 `CIMTextMargin` 的四边边距，四边不等也能原样表达；`fill`、`stroke` 为底框填色与边框，虚线照写）。`halo.blur` 没有对应（CIM 晕边是实边），`letterSpacing` 写成按字宽百分比的字距（em × 100，近似），两者都列入 `unsupported`。
- **图层上的 `labels`** 写成一个 `CIMLabelClass`，表达式为 Arcade：`field` 取字段，`template` 的 `{字段}` 拼成 `DefaultValue($feature["字段"], "") + …`。Maplex 位置：点 `auto` 为环绕点、`point` 为点上居中；线 `auto` 为沿线居中（直），`line` 为沿线弯曲，但带底框 `box` 时仍沿线直排（Pro 不给弯曲的标注画底框）；面为水平放在面内，`polygon-center` 不许放到面外。同时写了标准标注引擎的对应设置。`enabled: false`（gis-composer 导出时都是这样）时标注类保留但不显示，在图层上打开标注即可。
- **`labelPlacements`** 写成 `map_labels` 点图层（不画点、只出标注）和 `map_label_leaders` 引线图层，面板优先取标注自己的 `panel`，其次是 `layer` 所在的面板；字段与 QGIS 输出相同，另有 `size_pt` 和 `angle`（逆时针度数，即 `rotation` 取负）。每种 (文字样式, 锚点, 是否旋转) 一个标注类，按 `style_id`、`anchor`、`angle` 的 where 子句选中；偏移 0、从不移除、允许压盖，与 PNG 一致：
  - **不旋转的标注**：Maplex 的固定位置，锚点是文字落在点上的那一边，例如 `left` → 点的东侧（`EastOfPoint`），`bottom-right` → 点的西北侧（`NorthWestOfPoint`）。9 个锚点都在 Pro 3.4.3 里出图核对过。
  - **旋转的标注**：按 `angle` 字段旋转（`Arithmetic`）。Maplex 一旦按字段旋转，就不再理会固定位置，标注一律从点出发、沿角度方向排开（相当于 `left` 锚点），`center` 仍以点为中心；文字符号自身的 `angle` 在标注里不起作用（都在 Pro 3.4.3 里实测）。所以旋转的标注只有 `left`、`center` 两种锚点与 PNG 一致，其余锚点近似，每种（文字样式, 锚点）列入一条 `unsupported`（`item: anchor`）。
  - 引线图层按 `stroke_id` 唯一值着色：每种 `leaderStroke` 一类，颜色、线宽、虚线、线帽、拐角照写；没有 `leaderStroke` 时用文字颜色、1 px。
  - 多面板图里两张表按 `panel` 拆成每个面板一个图层（定义查询 `panel = '…'`），放进各自面板组的 `labels` 组。
- 只针对 Maplex（Pro 新建地图的默认标注引擎）验证过；地图改用标准标注引擎时这些固定位置不生效。

## ArcMap

ArcMap 读不了 ArcGIS Pro 的格式（ArcMap 10.8 实测）：

- `.lyrx`：`arcpy.mapping.Layer` 报“CreateObject Layer 的数据源无效”；在 ArcMap 里添加也同样不认。
- `.lpkx`：`ExtractPackage` 报 `ERROR 000814: 无效的文件类型`。ArcMap 只认 `.lpk`。

给 ArcMap 的同事时，用 `--lyrx` 打包，把 `<名>.gdb` 和 `rasters/` 发过去：ArcMap 10.8 能直接读 gis-kit 写的文件地理数据库（几何类型、坐标系、要素数、中文字段名都正确，图层不断链）和 GeoTIFF，只是没有样式，要照 `package.json` 的 `layers` 手动配。gis-kit 不写 ArcMap 的 `.lyr`（二进制格式）。

## 清单

`package.json` 与 QGIS 输出的清单结构相同（`map_style`、`scope`、`margin`、`extent`、`datasets`、`layers`、`panels`、`label_placements`、`fonts`、`icons`、`producer_notes`、`not_packed`、`unsupported`、`bytes`、`warnings`），不同之处：

- `layer_package`：文件名、大小、`archive: 7z`、写包的工具、包内图层文件 `p30/<名>.lyrx`、`cim_version`、地理数据库与栅格在包内的路径。
- `layer_file`：加 `--lyrx` 时直接可用的图层文件、地理数据库和栅格文件夹，否则为 `null`。
- `geodatabase`：名称和要素类；`map`：名称、标题、坐标系和打开方法。
- `layers[]` 的 `arcgis_renderer` 是写成的 CIM 渲染器或着色器，`filter` 是定义查询，`table` 是要素类；放置标注图层另有 `label_classes`。
- `datasets[].renamed_fields`：地理数据库改了名的字段。`background` 为 `{color, layer}`。

## 还没做的

- 不写地图（`.mapx`）和工程（`.aprx`）：地图背景色、比例尺、地图主题、书签、版式都不在图层文件里；面板只做成组图层，插图要手动开关。`legend`、`extent.print`、`scaleDenominator`、`extent.panels[].frame` 只记录不使用（`frame` 写在清单 `panels[]` 里，并在 `unsupported` 里有一条说明）。
- 旋转的预置标注只有 `left`、`center` 两种锚点与 PNG 一致（见上）。
- 瓦片底图不能原样打包，列入 `not_packed`，与 QGIS 输出相同。
- 图层包不带缩略图；`iteminfo.xml` 的摘要、标签是固定的说明文字。

## 验证

- `tests/test_package_pack_arcgis.py` 用虚构数据（与 QGIS 输出共用 `tests/pack_fixture.py`）检查：包的成员和 7z 格式；`package.py unpack` 读回包内的每份数据且没有多余数据；CIM 文档结构（URI 唯一、分组引用都存在、每个对象有 `type`、颜色取值范围、数据连接都是相对路径并指向包里的数据、包内与包外的图层文件只差路径）；分组、面板、背景和图层顺序；各渲染器、比例圆、排序、标记形状；三种栅格着色；标注、放置标注的标注类与 where 子句、引线；筛选的 SQL 与 Arcade（含 NULL、转义、方言）；改名字段跟进到渲染、查询和标注；未支持项报告。
- 装有 ArcGIS Pro 时（Windows），设 `GIS_KIT_ARCGIS_PYTHON` 为 Pro 自带的 Python（`…\ArcGIS\Pro\bin\Python\envs\arcgispro-py3\python.exe`），测试会用 `tests/arcgis_open_check.py` 让 Pro 打开直接的 `.lyrx` 和解开的 `.lpkx`，核对图层无断链、渲染器类型、定义查询后的要素数，并用解开的 `.lpkx`（同事拿到的就是它）出图取色。也可以单独运行：

  ```bash
  <pro python> tests/arcgis_open_check.py <包所在文件夹> <名> out.png <EPSG> 西,南,东,北 [经度,纬度 ...]
  ```

- 与 gis-composer 的 PNG 对照：用 composer 测试里的虚构工程（分类填色加比例圆和外置标注、两面板的分级图、阴影线绿地加线上标签框）渲染、发布、`project export-style` 导出 map-style，再分别打 QGIS 包和 Pro 包出图。三者的类别颜色、分级颜色、比例圆、预置标注与引线、标签框一致；不同之处都在预期内：composer 用 Web 墨卡托出图，两个包保持数据原坐标系（这里是经纬度，所以形状偏宽）；标题、图例、比例尺不导出；矢量瓦片底图不打包；阴影线 composer 导出时就记为 `untranslated`；没装主题字体的机器上标注换成别的字体。
- 本功能开发时在 ArcGIS Pro 3.4.3 上实测（虚构数据）：上面这条测试的断言全部成立（直接的 `.lyrx` 和解开的 `.lpkx` 各 16 个图层，0 个断链，取色与 QGIS 测试相同）；另一张专门的验证图逐项取色核对了分类（含 NULL 与未列出的值）、分级的分界值、带缺口与默认的分级、`!=` 筛选对 NULL、线性和离散伪彩色、调色板、RGB 拉伸，以及比例圆的直径；9 个锚点在旋转与不旋转时的位置也出图核对过。
- 第二轮字段（字重、留白、沿线标记、SVG、字段旋转、晕线、标注的面板）：`tests/test_package_pack_arcgis.py` 用 `tests/pack_fixture.py` 的 `build_round_two` 查 CIM 结构；在 ArcGIS Pro 3.4.3 上打开直接的 `.lyrx` 和解开的 `.lpkx`，都没有断链，Pro 读回的符号层类型与写入的一致，再量 `.lpkx` 的出图（`check_round_two_render`，与 QGIS 同一组断言，只是这里四边留白不取平均）：线距和线宽、晕线方向、第一个标记离起点半个间距、相邻间距、标记方向、SVG 的尺寸和方向、比例圆对 SVG 较长边的控制、字段旋转（含 NULL 和非数值）、留白，全部通过。
