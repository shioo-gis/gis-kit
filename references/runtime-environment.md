# 依赖与运行环境

## 先确认调用的解释器

`python3`、虚拟环境 Python 和脚本声明的 PEP 723 运行器可能对应不同环境。先运行所选解释器的 `--version`、`-m pip --version`，再运行 `scripts/daily.py environment`。报告包含实际解释器路径、Python/GEOS/GDAL/库版本、驱动和 `capabilities.coverage_simplify`。可选包存在只表示可导入性探测，不能替代真实操作验收。

项目已有已验证环境时，后续 CLI、测试和子进程统一使用它的 Python；例如项目本地 `.local/gis-runtime/bin/python`，路径仅为可复用布局示例，不假设所有项目均存在该环境。不要只在环境探查时使用新 Python，正式处理时又退回旧 `python3`。

通用分析的本次验证基线是 Python 3.10.14、Shapely 2.1.2 / GEOS 3.13.1；共享边简化最低要求 Python 3.10、Shapely 2.1 与 GEOS 3.12。Shapely 的二进制 wheel 自带对应 GEOS，无需另改系统 GEOS。不同 GDAL Python 包可以自带不同 GDAL，分别记录 Pyogrio 与 Rasterio 的运行版本，不按一个版本推断另一个包。

## 输出编码

工具的 stdout、stderr 写到管道和文件时固定 UTF-8，直接运行和经 `gis.py` 运行相同，不随系统代码页或区域设置变化。读取方按 UTF-8 解码，例如 Python 的 `subprocess.run(..., encoding='utf-8')`。

- 终端不变：Windows 控制台正常显示中文，其它终端按自己的编码显示，写不出的字符转义成 `\xb2` 这样的形式。
- Windows PowerShell 接管外部命令的输出（`|`、`>`、赋值给变量）时按 `[Console]::OutputEncoding` 解码，中文代码页上会显示成乱码。可以先设 `[Console]::OutputEncoding = [Text.UTF8Encoding]::new()`，或用工具的 `--output` 写文件。
- 确实需要其它编码时设 `PYTHONIOENCODING`，例如 `gbk`；写不出的字符同样转义，不会崩溃。

## 按能力加载依赖

| 能力 | 依赖与边界 |
|---|---|
| 矢量、计量、关联、版本差异与规则 | GeoPandas、Shapely、Pyogrio、PyProj、Pandas、NumPy；共享边简化另检查 GEOS 最低版本 |
| 通用栅格、分区统计、GeoTIFF/COG | Rasterio；掩膜与分区同时需要矢量依赖；COG 驱动通过真实写出/回读验证 |
| 描述统计及 DBSCAN | NumPy；DBSCAN 另需 scikit-learn/SciPy；不自动启用 PySAL 推断算法 |
| PNG、模板、表格 | Pillow、Matplotlib、PyYAML；Excel 用 openpyxl，表格美化 tabulate 可走已有 fallback |
| 图像增强及独立骨架实验 | OpenCV；骨架实验另需 scikit-image。依赖安装不授权程序追线，仍需用户明确选择该实验路径 |
| CAD 图纸（DXF） | ezdxf（可选）：块名与属性、闭合多段线、布局与视口、MTEXT 清洗；未安装时 `cad.py` 退回 GDAL 的 DXF 驱动，并在报告里列出缺少的能力，见 [CAD 图纸清点](cad-intake.md)。olefile（可选）：取出图里嵌入的 OLE 对象（多为 Excel 指标表），未安装时只列出框和位置；工作表清单另需 openpyxl |
| DWG 转 DXF | 外部程序，不属于 Python 依赖：ODA File Converter（需接受其许可）或 LibreDWG `dwg2dxf`（`brew install libredwg`）；`cad.py convert` 只调用已安装的程序 |
| 按需 Spatial SQL | DuckDB 1.5.0、官方 spatial 扩展、PyArrow / PyProj；独立可选 requirements-spatial-sql.txt，不强制进入普通 GIS 路径，见 [规模处理](spatial-sql.md) |
| Arrow 读写路径 | PyArrow；普通 Pyogrio 路径不默认要求 Arrow |
| 永久回归与完整文件链 | pytest 及上述被测试路径的依赖；仅覆盖简化的独立测试也可用标准库 unittest |

QGIS、GRASS、ArcPy、PySAL 继续是另行验证的可选后端，不包含在此依赖集合。安装 Python 包不等于取得 ArcGIS 许可或平台算法可用。

## 授权后安装或更新

优先使用已验证的虚拟环境；缺依赖时先报告实际导入失败。用户授权安装后，创建独立环境并使用该环境的 `python -m pip`，不要无界升级系统 Python、改变全局 pyenv 设置或写入 installed skill。

[已测依赖版本](../requirements-tested.txt) 固定直接依赖，用于重建本次分析/回归环境；它不是跨平台二进制锁文件，也不保证任意 Python 版本都有 wheel。新平台应先核验 Python、wheel、驱动及真实文件链。日常安装可按能力选用 [核心依赖](../requirements-core.txt) 或 [完整依赖](../requirements-full.txt)，其下限取自已测版本、不等于已测组合；conda-forge 环境见 [environment.yml](../environment.yml)。以下命令仅为获得安装授权后的示例：

```bash
python3.10 -m venv /absolute/gis-env
/absolute/gis-env/bin/python -m pip install --only-binary=:all: -r /absolute/gis-kit/requirements-tested.txt
/absolute/gis-env/bin/python -m pip check
/absolute/gis-env/bin/python /absolute/gis-kit/scripts/daily.py environment
```

若使用 `--system-site-packages` 复用已有依赖，报告必须说明继承关系；父环境变化会影响运行，不应宣称完全独立。更新先在新环境验证，保留旧环境可回退；拒绝把不同 Python ABI 的包目录塞进 `PYTHONPATH` 混用。

测试及图像缓存放仓库外，设置 `PYTHONDONTWRITEBYTECODE=1`、`MPLCONFIGDIR` 和 `XDG_CACHE_HOME`。更新后执行全套测试、真实 CLI/GPKG/GeoTIFF 链、解码/几何回读及视觉检查，记录版本与驱动差异。仅安装成功或 `pip check` 通过不能作为功能完成依据。

已测环境中的 Affine 3.0.1 对旧式矩阵乘法产生 PendingDeprecationWarning；当前数值与文件链已验证，警告仍须记录，不能据此声称未来版本兼容，也不要通过全局屏蔽警告冒充无告警验证。
