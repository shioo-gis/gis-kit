# 读取平台样式（QGIS、ArcGIS Pro → map-style）

`package.py read` 读一个平台样式文件，转成 [`map-style`](https://github.com/shioo-gis/gis-plugin/blob/main/contracts/README.md#map-style) v1，让已有的样式可以用到自己的图上（gis-composer、QGIS、ArcGIS Pro）。这是 [`pack`](qgis-package.md) 的反方向。

```bash
python package.py read 用地.qml --output /work/style/landuse --data parcels.gpkg --data-layer parcels
python package.py read 图层.lyrx --output /work/style/map
python package.py read 符号库.stylx --output /work/style/lib --field 用地编码 --match name --font-dir /work/fonts
```

map-style 是最小公约数，符号只有 `fill`、`stroke`、`marker` 各一个，这里的用途是把样式拿来用，不是高保真的格式转换。转不了或只能近似的项全部写进 `notes`（`untranslated` 没写进来，`approximated` 近似写的），不悄悄丢掉。

## 输出

`--output` 是一个新目录，整体可以搬走（`dataRoot: "file"`）：

| 文件 | 内容 |
|---|---|
| `<名>.map-style.json` | 通过 map-style Schema 校验的文档 |
| `assets/<哈希>.svg` | 转成图片的符号，`marker.svg` 引用 |
| `style-read.json` | `manifest: style-read` v1：`symbols` 每个符号落在哪一档，`losses` 每类损失的次数，`library`（`.stylx` 的条目数），`fonts`，`images` |
| `record.json` | 运行记录，源文件和产物的哈希 |

`symbols` 的档位按损失最重的算：`exact` 原样，`svg` 合成一张图但没有损失，`split` 拆成多个图层，`approximated` 有近似，`lost` 有丢失。

## 数据

- `.qml` 和 `.stylx` 不含数据。`data.path` 是占位的 `data/unbound/<图层>`，记入 `notes`；`pack` 会因为找不到数据而拒绝，不会打出空包。用 `--data` 和 `--data-layer` 指定，或读完后改文档。
- `.lyrx` 的数据连接是绝对路径或带 `..`，map-style 都不允许。只保留最后的名称（`demo.gdb` 加 `layer`、`a.shp`、`g.tif`），其余路径不进文档，也记入 `notes`，要指回实际位置。

## `.stylx`

`.stylx` 是 SQLite：`ITEMS` 的 `CONTENT` 是 CIM JSON，末尾带一个 `\0`；`CLASSES` 说明条目种类。Pro 自带的 `Styles.stylx` 是另一种内部结构（没有 `ITEMS`），不读。

符号库不是地图，map-style 也没有符号库的概念，所以按符号库的用途来读：**每种几何一个 `categorized` 图层，每个符号是一类，值是它的名称**。数据里有一个字段存这些名称，这个图层就能按符号库上色。

- `--field`：这个字段的名字，默认 `name`。
- `--match name|key`：用名称还是 `KEY` 匹配。常见的符号库两种写法都有：名称是编码，`KEY` 是“编码 名称”。
- 用地用海这类面符号是纯 RGB 填色，读出来就是“编码 → 颜色”的配色表。
- 颜色、色带、文字符号、指北针等非地图符号的条目只计数，记入 `notes`。同一几何里名称重复的只留第一个。

## 对应关系

| 层次 | QGIS | ArcGIS Pro（CIM） | map-style |
|---|---|---|---|
| 分类 | `categorizedSymbol`；值为空的类 | `CIMUniqueValueRenderer`；`defaultSymbol` | `categorized`；`default` |
| 分级 | `graduatedSymbol` | `CIMClassBreaksRenderer` | `graduated` |
| 规则 | `RuleRenderer`，`ELSE`；全是同字段相等的规则读成分类，全是相接区间的读成分级 | 多字段唯一值、含 `<Null>` 的类、Arcade 取值 | `rule`，`default` |
| 栅格 | 伪彩色、调色板、多波段、灰度 | 拉伸、分类、唯一值、RGB | `pseudocolor`、`palette`、`rgb` |
| 单层符号 | `SimpleFill`、`SimpleLine`、`SimpleMarker` | `CIMSolidFill`、`CIMSolidStroke`、`CIMVectorMarker` | `fill`、`stroke`、`marker` |
| 晕线 | `LinePatternFill`（两组垂直的读成 `crossHatch`） | `CIMHatchFill` | `fill.pattern` |
| SVG、图片标记 | `SvgMarker`（内嵌或样式旁的文件，填入 `param(fill)` 等） | `CIMPictureMarker` | `marker.svg` |
| 沿线标记 | `MarkerLine` 按间隔 | `CIMMarkerPlacementAlongLineSameSize` | `placement: line` |
| 标注 | `labeling` | `labelClasses` | `labels` |

单位换成 px：mm × 96/25.4，pt × 96/72。颜色都写成 `#RRGGBB` 加透明度。CIM 的颜色有两种写法，`{type, values}`（透明度 0–100）和 Pro 自带符号库用的 `[r, g, b, a]`（0–255）。

**筛选**：QGIS 表达式、定义查询（SQL）和 Arcade 明确是结构化筛选时才转，其余保留成 `{expression, dialect}`（`qgis`、`sql`、`arcade`）。NULL 的语义要一致才转：`"a" <> 1` 对 NULL 是未知，转成 `notNull` 加 `!=`；`"a" IS NULL OR "a" <> 1` 才是 map-style 的 `!=`；对可能为 NULL 的值取 `NOT` 不转。`pack` 写出的筛选都能读回原样。

**规则**：map-style 取第一个命中的规则，QGIS 画所有命中的规则。规则可能重叠时记 `approximated`（`rule-overlap`）。`pack` 写的规则带排除条件，读回时去掉。

## 复杂符号

一个 map-style 符号只有一个填充、其上的晕线、一个描边、一个标记，按这个顺序画。

- **多层线、多层面**：放不进一个符号的部分另起一个图层（`<id>-2`），叠在上面，图例只显示第一层。一个要素内部的顺序不变，但要素之间的交错会变，比如道路衬边压在别的道路上面。
- **多层点符号**：合成一张 SVG，图像以锚点为中心，所以落点不变。曲线（`a`、`c`、`b`）、嵌套标记、旋转、偏移、锚点和 `scaleSymbolsProportionally` 都算进去。`markerGraphics` 先列的画在下面，这和 `symbolLayers` 相反（Pro 实测）。
- **沿线标记的颜色**：标记只能用符号的 `fill`、`stroke`。线图层的 `stroke` 是线本身，面图层的 `fill` 是面本身，标记颜色对不上时转成 SVG，用它自己的颜色。

**标记里的文字需要字体。** 读取时有这个字体（已安装，或用 `--font-dir` 指定）就转成轮廓，之后 SVG 不再依赖字体，字形和原符号一致。没有字体时 SVG 保留 `<text>` 并写明字体名，记 `approximated`（`text-needs-font`）：文字和位置都在，但在没装这个字体的机器上字形会换。字体不随结果分发，由用户确认能否使用。

### 转不了的项（`notes` 里 `untranslated`，`losses` 里的代码）

- `polygon-center-marker`：面中心的标记（QGIS 的 `CentroidFill`）。保留填充，标记丢失；符号本来就只有这个标记时，类还在，但什么都不画（`nothing-drawn`）。
- `marker-fill`：面内重复的标记。`line-marker-position`：线端、比例位置、顶点上的标记。
- `geometric-effect`：虚线以外的几何效果。`paint-effect`：QGIS 的绘制效果。`picture-fill`、`picture-marker`（图片不在样式里）。`symbol-layer`：其他符号层。
- `scale-range`：比例尺范围。`blend-mode`：混合模式。`data-defined`、`visual-variable`：连到字段的其他属性（旋转和比例圆除外）。
- `label-rules`：多个标注类只留第一个。`label-expression`：不是字段或模板的标注表达式（近似成其中第一个字段，或丢掉）。`placed-labels`：固定位置的标注在数据表里，不在样式里。
- `renderer`：热力图、图表等渲染器，要素不画。栅格没有对应着色方式时：`.qml` 拒绝，`.lyrx` 跳过这个图层。

只能近似的（`approximated`）：`line-marker-start`（标记从半个间距开始，不保留起点和线端处理）、`dash-ending`、`offset`、`named-dash`、`brush-hatch`、`gradient`、`range-boundary`（QGIS 的分界值落在下一档之前）、`default-null`（Pro 的“所有其他值”不含 NULL）、`marker-clip`（裁剪真正切到标记时不裁切，只是贴边的不算）、`letter-spacing`、`color-model`。

一个符号读不下去时只损失这个符号：记 `unreadable-symbol`，这一类保留但不画。

## 代码结构（新增读取方向时照此）

写入方向是“map-style → 某个平台”的输出器，读取方向对称：

- `scripts/_style_read.py`：平台无关的部分，不知道 QML 和 CIM。命令入口、`Reading`（一次读取：`notes`、图片、图层、统计）、把符号部件合并成 map-style 符号（`merge_parts`）、把多次绘制展开成图层（`add_layer`）、筛选文本解析（`parse_filter`）、规则还原（`rules_renderer`）、SVG 绘制。
- `scripts/_qgis_read.py`、`scripts/_arcgis_read.py`：每个平台一个读取器。
- `package.py` 只有注册和调用两行。

加一个平台（ArcMap 的 `.lyr`、`.style`）只需要：

1. 新建 `_arcmap_read.py`，提供 `read(path, reading)`，在 `_style_read.READERS` 里加后缀。
2. 把一个符号转成**部件**，自下而上：`{kind: fill, color, opacity}`、`{kind: pattern, pattern}`、`{kind: stroke, stroke}`、`{kind: marker, marker, fill, stroke, ops, place}`。`marker` 为 `None` 时按 `ops` 画成 SVG。用 `reading.symbol(geometry, where, convert)` 换回“几次绘制”，这一步同时统计损失。
3. 把渲染器转成中性形式：与 map-style 相同，只是 `symbol` 换成 `passes`。规则用 `rules_renderer`。
4. 调用 `reading.add_layer(...)`。数据不明时用 `reading.unbound_data(...)`。
5. 转不了的调用 `reading.note(kind, code, reason, where, property)`。

## 验证

- `tests/test_style_read.py` 用虚构样式覆盖各层次：配色和分类、简单符号、SVG 和晕线、复杂符号，`.stylx` 的 `\0` 和两种颜色写法，筛选解析，以及拒绝不合法的文件。`.qml` 里的外部 SVG 只读样式所在目录，其他路径不打开。
- 往返：`pack_fixture` 的两张虚构地图经 `pack` 打成 QGIS 和 Pro 格式，再读回，渲染器、符号、筛选、比例圆、排序、标注的主要字段一致。设 `GIS_PLUGIN_CONTRACTS` 为 gis-plugin 的 `contracts` 目录且装有 `jsonschema` 时，每份结果都用 Schema 校验。
- 平台实际保存的文件：`tests/qgis_save_styles.py` 用 QGIS API 设置样式后 `saveNamedStyle`，测试读回。`tests/arcgis_save_styles.py` 在 ArcGIS Pro 里设置符号并保存 `.lyrx`，再把 `pack` 的 `.lyrx` 打开重存；把输出目录设给 `GIS_KIT_ARCGIS_SAVED` 后测试读回。
- 本功能开发时实测（虚构数据）：
  - QGIS 保存的 14 种样式、Pro 3.4.3 保存的 8 个 `.lyrx` 和重存的 2 个 `.lyrx` 都读对了。Pro 重存后的文件读回，主要字段同样与原 map-style 一致。
  - Pro 自带的 3 个 `.stylx`（`ENC`、`AML`、`mil2525d`，共 7698 个符号）全部读完，`unreadable-symbol` 为 0。
  - 多层点符号转 SVG：10 个虚构符号（文字、`frame` 0–50、旋转、偏移、锚点、嵌套、按比例和不按比例的线宽），分别用原 CIM 符号和 SVG 在 Pro 里出图比较。不含文字和圆的几个差异在 0.5% 以内，圆的外缘和字形边缘差约 1 像素（192 dpi）。同一批 SVG 在 QGIS 里位置和形状一致。
  - `<text>`：字体已装时字形一致；字体缺失时 Pro 和 QGIS 都用替代字体画出来，不会丢字。
