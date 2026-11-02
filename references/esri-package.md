# ArcGIS 图层包与地图包

`package.py unpack` 把 ArcGIS 的图层包（`.lpk`、`.lpkx`）或地图包（`.mpk`、`.mpkx`）安全地解开，清点里面有什么数据，并把每个图层文件和它引用的数据对上。解包结果发布到一个新目录，之后可以直接用 gis-kit 的其他工具读取。反方向（一张图的 `map-style` 打包成 QGIS 打开即带样式的 GeoPackage，或 ArcGIS Pro 的图层包）见 [qgis-package.md](qgis-package.md)、[arcgis-pro-package.md](arcgis-pro-package.md)；`unpack` 也能读回 gis-kit 自己打的 `.lpkx`。

```bash
python package.py unpack 示例拆除类.lpk --output /work/pkg/chaichu
```

## 解包

- **格式**：包实际是 7z 或 zip 压缩包，按文件头判断。7z 用 `bsdtar`（libarchive，macOS 自带；Windows 自带的 `tar.exe` 也是它）读，没有 `bsdtar` 时改用 7-Zip（`7z` / `7za` / `7zz`）；zip 用 Python 自带的 `zipfile`。
- **先检查后解压**：先列出全部成员，有下列情况时直接拒绝，什么都不解压：
  - 成员路径是绝对路径、带盘符，或者含 `..`；
  - 成员是符号链接或硬链接；
  - 解压后的总大小超过 `--max-bytes`（默认 20 GiB）。
- **解压后核对**：所有文件都必须落在 `package/` 里面、不能是链接，总字节数也要和列表一致。

## 清点

- **包信息**：读 `esriinfo/item.pkinfo`（名称、版本、类型、ID）和 `iteminfo.xml`（标题、摘要、标签、类型关键字、经纬度范围）。
- **数据集**：`commondata/` 下，以及各图层版本文件夹（`v10`、`v107` 等）里的每个 Shapefile、FileGDB 和栅格，都附一张 `describe` 卡片（图层、要素数、CRS、范围、字段）。缺 CRS 等警告会汇总到清单的 `warnings` 里。
  - ArcMap 包有时把数据放在版本文件夹里，而不是 `commondata/` 下，每个版本文件夹各放一份，`.lyr` 用相对路径（例如 `规划.gdb`）引用。几份完全相同（文件名、大小、SHA-256 都一样）时只列一次，取版本号最高的那份，其余记在 `copies` 里；几份不同时各列一次，并给出警告。
  - 复合坐标系（投影＋高程），或 ESRI 写的带号前缀高斯投影，`crs.id` 可能为 `null`。能认出水平部分时，卡片的 `crs.horizontal` 给出推断的 EPSG（例如带号前缀的 CGCS2000 3 度带 38 带＋1985 国家高程基准，水平部分为 EPSG:4526），`inferred: true`。只有投影参数和地理坐标系都与候选 EPSG 相同时才给出。
- **图层文件**：
  - ArcMap 的 `.lyr`：二进制格式。ArcMap 包里每个 ArcGIS 版本各有一个文件夹（`v10`、`v105` 等），只读版本号最高的那个。从文件中的 UTF-16 字符串（两个字符起，`水系` 这样的名称也取）里取出数据源路径（例如 `..\commondata\示例规划`）、提到的名称和标注表达式（例如 `[项目名]`），再按数据集名和所在文件夹对应到数据。不以 `commondata` 开头的路径相对于 `.lyr` 所在的版本文件夹，只对应那个文件夹里的数据（或与它相同、记在 `copies` 里的那份）。FileGDB 里的图层名出现在 `.lyr` 里时，对应到 `<gdb 路径>:<图层名>`；图层名在二进制里可能和前后的字节连在一起，所以按包含关系查找。符号化读不出来，记为 `not read: binary .lyr`。
  - ArcGIS Pro 的 `.lyrx`：CIM JSON。逐个读出图层的数据连接（工作空间和数据集）、定义查询、渲染器类型、渲染字段、分类（标签和取值；断点渲染时取上限）和标注表达式。颜色和符号不读，记为 `classes and labels read; colours and symbols not reproduced`。
  - `.mxd`、`.mapx`：只列出文件名，不读里面的内容。
- **缺数据**：图层引用的数据不在包里时（例如打包时只保留了引用，或者企业数据库的数据没有打进来），`data_in_package` 为 `false`，并给出警告。包里没有任何数据集时，警告这个包只有图层定义。有数据集没被任何图层引用时，同样给出警告。

## 输出

新目录，里面有：

- `package/`：原样解出的包内容，数据在 `package/commondata/` 下。
- `package.json`（`manifest: esri-package` v1）：
  - `source`：文件名、大小、SHA-256、归档格式、读取工具、解压后字节数、成员数；
  - `package`、`item`：包信息；
  - `layer_versions`：各 ArcGIS 版本的图层文件夹；
  - `datasets[]`：路径、类型和 describe 卡片，版本文件夹里的相同副本记在 `copies`；
  - `layers[]`：文件、名称、格式，从中读出的内容，`data`（对应的数据集），以及 `data_in_package`；
  - `thumbnail`、`warnings`。
- `record.json`。

打包时的 CRS 原样保留在数据里，例如 `.prj`。这些数据也可以作为 `cad.py inspect --reference` 的参考图层：示例包里的 `拆除类` 是 EPSG:4547，用它比对示例法图，得到的结论是范围一致。
