# 打包成 ArcMap 图层包（.lpk）

`package.py pack --format arcmap` 读一份 [`map-style`](https://github.com/shioo-gis/gis-plugin/blob/main/contracts/README.md#map-style) v1 和它引用的数据，写出一个 ArcMap 图层包（`.lpk`）。同事在 ArcMap 里“添加数据”或双击它，得到一个按面板、分组排好的组图层，符号颜色与 gis-composer 出的 PNG 逐字节相同。QGIS、ArcGIS Pro 的输出见 [qgis-package.md](qgis-package.md)、[arcgis-pro-package.md](arcgis-pro-package.md)，三者共用同一套取数逻辑。

```bash
python package.py pack maps/example.map-style.json --output /work/send/example-arcmap --format arcmap
python package.py pack maps/example.map-style.json --output /work/send/example-arcmap --format arcmap --lyr --clip
```

**数据范围**（默认按图幅选取 / `--clip` / `--full`、`--margin`）、**数据路径**（`dataRoot`、`@根名/…`、`--data-root`、`data.sha256`）和瓦片的处理与另外两个输出完全相同。代码在新模块 `_arcmap_style.py`（写 `.lyr`、组织 `.lpk`），`package.py` 只负责取数和入口。

这里只写方向 map-style → ArcMap。反方向（从 `.lyr` 读出 map-style）尚未实现。

## 写出什么

新目录（原子发布，已存在就报错）：

- **`<id>.lpk`**：图层包，发给同事的就是它。
- **`<id>.lyr`、`<id>.gdb/`、`rasters/`**（加 `--lyr` 时）：同一个图层文件放在解开的数据旁边，不用解包就能添加。数据源是相对路径，几个文件要放在同一个文件夹里。
- **`package.json`**（`manifest: arcmap-package` v1）：清单；`unsupported` 列出转不了或只能近似的项，`kind: approximated` 表示近似。
- **`record.json`**：运行记录，含本机绝对路径，不必发给同事。

### `.lpk` 的结构

```
<名>.lpk                     7z 压缩包，不是 zip
├── esriinfo/item.pkinfo      <version>10.7</version>、<type>Layer Package</type>
├── esriinfo/iteminfo.xml     标题、摘要、标签、经纬度范围
├── v107/0000<名>.lyr         工作空间写成 .\<名>.gdb
├── v107/<名>.gdb/            数据（文件地理数据库）
├── v107/rasters/*.tif        栅格（目前没有图层画它们，见下）
├── v105/0000<名>.lyr         工作空间写成 ..\v107\<名>.gdb
└── v10/0000<名>.lyr          同上
```

为什么数据放在 `v107/` 里，而不是研究阶段记下的 `commondata/`：两种布局 ArcMap 的 `ExtractPackage` 都能解开，但 ArcGIS Pro 3.4 的 `ExtractPackage` 解 `.lpk` 时只取最新的版本文件夹，把它的内容铺到目标目录，`commondata/` 里的数据不会解出来，图层就断了（实测）。ArcMap 10.8 自己用“打包图层”打的包正是把数据放在版本文件夹里（每个 `v*/` 一份 `.gdb`，共九份）。放进 `v107/` 后 ArcMap 和 Pro 都连得上；`v10/`、`v105/` 指向同一份，免得数据存三遍。

- 包 ID 随内容变化，与 `.lpkx` 相同：ArcGIS 按 ID 缓存解开的包，内容变了 ID 也要变。
- 不写缩略图。用 bsdtar 或 7-Zip 写 7z，与 `.lpkx` 用的同一段代码。

## 数据

与 Pro 输出相同：矢量写进文件地理数据库（GDAL OpenFileGDB 驱动，中文字段名、NULL、不截断），与 SQL 关键字同名的字段改名并跟进到渲染、查询、标注，栅格存 GeoTIFF。ArcMap 10.8 能读 GDAL 写的文件地理数据库（实测）。`.lyr` 里存的范围和坐标系是骨架的旧值，ArcMap 不用它们：图层范围、数据框坐标系都取自数据本身（实测 UTM、Web 墨卡托的数据配 WGS 84 的骨架）。

## 样式怎么转

尺寸 px → pt（× 0.75）。ArcMap 在 96 dpi 下 1 px 正好一个像素（实测线宽、标记边长）。

| map-style | ArcMap |
|---|---|
| `single` | 简单渲染器 |
| `categorized` | 唯一值渲染器；`default` 接住未列出的值和 NULL（实测）；数值分类写成文本（`1.5`、`4`），ArcMap 按字段值匹配 |
| `graduated`（相接、升序、无默认、全显示） | 分级渲染器；上界取下一个双精度数，分界值落进上一档，与 PNG 的左闭右开一致（实测） |
| `rule`；有缺口、隐藏档或默认的 `graduated` | 一个组图层，每条规则一个单一符号图层加定义查询，后面的规则排除前面已命中的（先命中先用） |
| 面：`fill`、`stroke` | 简单填充符号；没有 `fill` 或不透明度为 0 写空填充，没有 `stroke` 写空轮廓线 |
| 线：`stroke` | 简单线符号，颜色、线宽 |
| 点：`marker`、`fill`、`stroke` | 简单标记符号，`circle`、`square`、`cross`、`x`、`diamond`，大小、角度、轮廓线 |
| `filter` | 定义查询（文件地理数据库 SQL，NULL 读法同 Pro 输出；其他方言不筛并记录） |
| `opacity` | 图层透明度 |
| `visible: false` | 图层不显示 |
| `labels` | 标准标注引擎一个标注类：`field` 写 `[字段]`，`template` 写 VBScript 表达式；字体、字号、颜色、晕边；`enabled: false` 不显示 |
| `panel`、`group`、`background` | 组图层（主面板显示，其余不显示）；背景写成最下面的面图层，与 Pro 输出相同 |
| `labelPlacements` | 按样式分层的点图层（符号不画）只显示标注，引线按线型分层；位置由标注引擎定 |

两个判断：

- **分类图用真正的唯一值 / 分级渲染器，不用“每类一个图层”。** issue 提过变通办法，但类数和索引表的结构已经摸清（下节），组装结果和 ArcObjects 自己写的文件逐字节相同，300 类也能打开；真渲染器在 ArcMap 里就是一个图层、一个图例，Pro 读出来是 `CIMUniqueValueRenderer` / `CIMClassBreaksRenderer`。变通办法只用于 ArcMap 没有对应渲染器的情况（规则、带缺口的分级）。
- **符号透明度。** ArcMap 的符号没有透明度。一个图层所有符号部件的不透明度相同时，并进图层透明度，结果准确；不同时画成不透明并记录。

### 转不了的（列入 `unsupported`）

| 项 | 现在的处理 |
|---|---|
| 栅格图层 | 数据在包里，没有图层画它（没有骨架）；瓦片同其他输出，列入 `not_packed` |
| `fill.pattern` | 只画底色（只有线时什么也不画），记录 |
| `size`（比例圆） | 用符号本身的大小 |
| `sortBy` | 不生效 |
| 面、线上的 `marker`（含 `placement: line`） | 不画 |
| 点的 `triangle`、`arrow`、`chevron`、`ring`、`svg` | 画成圆（近似） |
| `rotationField` | 只用固定 `rotation` |
| `stroke.dash` | 画成 ArcMap 固定的虚线（近似） |
| `stroke.cap`、`join` | 简单线符号总是圆头圆角（近似） |
| 部件透明度不一致、晕边和文字的透明度 | 画成不透明（近似） |
| `labels.placement`、`labelPlacements` 的 `anchor`、`rotation` | 由标注引擎放置（近似），可能略去放不下的 |
| `letterSpacing`、标注底框 `box`、晕边模糊 | 不画 |
| 类 `visible: false` | 写成什么也不画的符号，图例里还在（近似） |

## 文件格式（实测）

`.lyr` 是闭源的 COM 持久化格式。下面是从 ArcObjects 写的样本差分出来的，够用来改参数，不是完整规范：

- **容器**：CFB v3（512 字节扇区），根目录下只有一条 `Layer` 流。gis-kit 自己读写（`read_layer_file`、`write_layer_file`），`olefile` 只读且是可选依赖，写容器不用它（测试里拿它交叉校验）。不到 4096 字节的流放迷你流。读取时不信任文件：扇区链和迷你链每个扇区只走一次，成环、越界、声明过大都报错，收集的内容不会超过文件本身；写入上限约 7 MB（不写 DIFAT），超过就报错。
- **对象**：类 GUID（Windows 内存布局）+ u32 编号 + u16 版本 + 字段。**编号从根对象的 0 起全流递增**；同一个对象再次出现时只写 GUID 和编号（回引）。空对象是 16 个零字节。对象外面没有总长度，只有组图层的每个子图层前面有 u64 字节数。所以字符串可以改成任意长度，但增减对象后要统一重排编号（`renumber`）。
- **字符串**：u32 字节数（含结尾）+ UTF-16LE。图层名、数据集名、工作空间路径会出现多次，要一起改（工作空间只改一处时能打开但 `isBroken=True`）。
- **颜色**：`{7EE9C496-D123-11D0-8383-080009B996CC}`，版本后 3 个标志字节，然后 3 个双精度 CIELAB。Lab 由 gamma 1.8 的 RGB 算出，矩阵接近 Apple RGB 但不相同：用 ArcObjects 写的值反解到 1e-15，白点 D65。按 sRGB 算会暗十几级。写进去的 RGB 与 ArcMap 渲染出的像素逐字节相同，Pro 读回也相同。
- **简单线符号**：颜色后依次是线宽、线型（0 实线、1 虚线、2 点线、5 空）、ROP。**简单填充符号**：先轮廓线对象，再颜色、ROP、一个保留字段、填充样式（0 实心、1 空）。**简单标记**：颜色、大小、样式（0 圆、1 方、2 十字、3 叉、4 菱形）、ROP、角度（逆时针）、偏移、轮廓开关、轮廓宽、轮廓颜色。
- **唯一值渲染器**：字段数、字段名、分隔符，默认符号（没有默认时也要占位），图例组数，每类一个图例组（标题、1、图例类：符号、标签、说明），一个空对象，值的个数，然后每个值 `(值, 所在图例组序号, 0, 0)`。这张表就是研究笔记里的“索引表”。用了默认符号时，默认类是第一个图例组（值为空，符号回引默认符号），序号相应加一，尾部的开关置位。
- **分级渲染器**：字段名、归一化字段（空）、档数、各档上界（双精度）、同样多个零、最小值；一个图例组里放所有档的图例类。
- **图层表**：显示字段、标注开关、定义查询，然后是字段表（字段数 + 每字段的名称、FieldInfo、别名、数值格式或空对象、只读标志）。字段表可以和数据不一致，ArcMap 仍以数据为准；gis-kit 按数据写一份。但字段数不能写 0：写 0 时打不开（“数据源无效”）。
- **透明度**：图层扩展对象后的显示过滤器槽，放 `{AD754A65-13B4-11D3-B89D-00600802E603}`，里面存 alpha（0～255），过了这个对象再存百分比。ArcMap 读 alpha。
- **标注**：字段表后的空对象换成标注集合；表达式字符串后面的开关表示是否简单表达式；文字符号里颜色、字号（双精度）、掩膜（样式、大小、填充符号，即晕边）；字体是 OLE StdFont：字符集、标志、字重、以万分之一磅计的字号、ANSI 编码的字体名。字符集写西文时中文标注画成方框，所以含中文的标注写 GB2312（实测）。
- **组图层**：名称、显示开关、子图层数，每个子图层前有 u64 字节数；子图层可以还是组图层。编号贯穿全部子图层。子图层在列表里靠前的画在上面。

## 骨架

`templates/arcmap/*.lyr` 是 ArcObjects 写的真图层文件，数据全部虚构，参数位置换成了 `@name`、`@workspace` 这样的占位字符串，机器路径和 Windows 语言（要素类类型名、“默认”标注类名）都去掉了；它们仍是能打开的图层文件。`_arcmap_style.py` 只裁切、改写、拼接这些字节，不从零序列化任何类。

重新生成（需要 Windows、ArcMap 10.8 和它的 .NET 互操作程序集；脚本在 `tests/arcmap_skeletons/`）：

1. `make_base.py ROOT`（ArcMap 的 Python 2.7）：在 `ROOT\skeleton.gdb` 写面、线、点三个虚构要素类，并各存一个基础 `.lyr`。
2. `build.ps1 ROOT`（**32 位** PowerShell）：编译并运行 `Skeletons.cs`，在 `ROOT\gen\` 写出各个用例，每个用例只差一个参数。ArcObjects 的类工厂要先 `RuntimeManager.Bind`、再初始化许可才能用；裸进程打不开工作空间，所以从基础 `.lyr` 取图层。
3. 把 `gen\` 拷回来，`normalize.py GEN templates/arcmap ROOT\skeleton.gdb`：写模板和 `golden.json`（每个用例规范化后的摘要）。

`tests/test_package_pack_arcmap.py` 用模板组装同样的参数，和 `golden.json` 比较：除 Lab 的最后几位外，要和 ArcObjects 写的逐字节相同。覆盖 1～4 类的唯一值和分级（面、线、点）、默认类、各种单一符号、定义查询、隐藏、透明度、标注。

## 验证

- 单元测试（虚构数据）：容器往返和 olefile 交叉校验、字符串改写、编号重排、颜色换算、与 ArcObjects 的逐字节比较、`.lpk` 布局、`unpack` 读回。
- 真机检查（可选，虚构数据）：设 `GIS_KIT_ARCMAP_PYTHON`（ArcMap 的 Python）、`GIS_KIT_ARCGIS_PYTHON`（Pro 的 Python）后 pytest 会跑 `tests/arcmap_open_check.py`、`tests/arcmap_open_check_pro.py`：读回 `isBroken`、渲染器类型、类和分界值、定义查询、透明度、标注，把图层加进数据框渲染 PNG，按点取色、量线宽和标记大小。

在 ArcMap 10.8、ArcGIS Pro 3.4.3 上实测：图层文件和 `.lpk` 解出的三个版本文件夹全部不断链；唯一值、分级被识别为 `UNIQUE_VALUES`、`GRADUATED_COLORS`，类、标签、分界值读回一致；取色逐字节一致（含分界值、NULL、未列出值、数值分类）；6 px 线宽、30 px 标记、4 px 轮廓都量到；图层上下顺序、隐藏、定义查询、中文名称和中文包名都对；300 类、30 档能打开。Pro 读出的渲染器类型和颜色也一致。

## 没有实测的

- 只用 `arcpy` 和 `ExtractPackage` 走过流程，没有在 ArcMap 界面里手动拖入 `.lpk`。
- `v10/`、`v105/` 是在 10.8 上打开的，没有 10.0～10.6 的真机。
