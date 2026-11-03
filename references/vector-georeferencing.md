# 矢量控制点转换

`vector-georef.py` 用经过检查的四参数或六参数变换，把矢量数据从局部平面坐标（城市独立坐标、CAD 图纸坐标）转到一个投影坐标系。典型用法：把城市独立坐标的 CAD 成果转到 CGCS2000 3 度带 114°E。只提供四参数（平移、旋转、统一缩放）和六参数仿射两种模型，不做高阶多项式或橡皮拉伸。

```bash
# 由同名点拟合
python vector-georef.py fit --gcp 同名点.csv --target-crs EPSG:4547 --max-check-rmse 0.5 \
  --basis "6 个道路中线交点，取自 CGCS2000 现状地形图" --output /work/geo/sz2cgcs
# 记录官方或用户给的参数，可以同时用点检查
python vector-georef.py define --origin official --tx 490000 --ty 2480000 --scale 1.00002 --rotation 1.5 \
  --target-crs EPSG:4547 --basis "某某转换参数文件" --gcp 检查点.csv --max-check-rmse 0.5 --output /work/geo/official
# 应用：默认转换全部图层
python vector-georef.py apply /work/cad/fatu-parcels/parcels.gpkg --transform /work/geo/sz2cgcs/transform.json --output /work/geo/parcels-cgcs
```

## 控制点

CSV 必须有 `id`、`role`、`src_x`、`src_y`、`dst_x`、`dst_y` 列，`note` 列可选。

- `role` 必须显式写 `fit` 或 `check`，不随机抽取；检查点不参与拟合。
- `id` 必须唯一。同一个源坐标不能出现两次，防止同一个点既做拟合点又做检查点。
- 源坐标的坐标系由 `--source-crs` 指定，默认 `none`，即没有 CRS、单位为米的平面，例如 `cad.py` 的输出。目标坐标写在 `--target-crs` 里，必须是以米为单位的投影坐标系。
- 所需点数：拟合点至少比未知数多一个，四参数至少 3 个，仿射至少 4 个，这样残差才有意义；另外至少要有 1 个检查点。仿射拟合遇到共线或病态的点会报错。

## 门槛与状态

- `--max-check-rmse`（米）必须在拟合前给出；`--max-check-error` 是可选的单点最大误差。
- 检查点的 RMSE 或最大误差超过门槛、或者没有检查点时，状态为 `hold`，其余为 `pass`。`pass` 只说明这几个检查点满足了事先给定的门槛，仍要和参考数据叠加核对。
- `define` 没给 `--gcp` 时，参数没经过任何检查，状态为 `hold`。给了 `--gcp` 时，所有点都当作检查点。
- `--basis` 必填，写明点或参数的来源；`origin` 记为 `fitted`、`official` 或 `user`。

## 输出

- `fit` / `define`：新目录，含：
  - `transform.json`（`transform: vector-transform` v1）：
    - `model` 和 `matrix`（`[[a, b, c], [d, e, f]]`，即 x′ = a x + b y + c、y′ = d x + e y + f）；
    - `parameters`（两轴缩放、旋转角、平移、行列式）；
    - `origin`、`basis`，源 / 目标 CRS；
    - 控制点及各点残差、拟合与检查的统计；
    - `acceptance`（状态、原因、门槛）；
    - `fit_hull`：拟合点在源坐标下的凸包。
  - `residuals.png`：残差箭头放大后画在目标坐标上，拟合点蓝色、检查点红色，标出各点残差。
  - `record.json`。
- `apply`：新目录，含：
  - `<输入名>.gpkg`：图层名和属性保持不变，CRS 为目标 CRS，另加 `extrapolated` 字段，表示要素是否落在拟合点凸包之外。`define` 没有凸包时，这个字段为空。Z 值原样保留，不随比例缩放；M 值读取时就被丢弃（stderr 有警告，`record.json` 的 `assumptions` 也写明）。
  - `manifest.json`（`manifest: vector-georef-apply` v1）：输入的 SHA-256、所用变换及其状态、各图层要素数、各图层外推要素数、各图层带 Z 的要素数（`z_features`）、是否用了 `--allow-hold`。
  - `record.json`。

## 应用时的检查

- 变换处于 `hold` 时拒绝应用；加 `--allow-hold` 才执行，并记入清单。
- 输入图层的 CRS 必须和变换的源 CRS 一致：两者都没有 CRS，或者两者相同。
- 输入里已有 `extrapolated` 字段时拒绝，以免覆盖原有字段。
- 只处理二维坐标。四参数变换会让面积乘以缩放系数的平方，清单里的 `parameters` 可以拿来核对。

已是国家坐标、只是没有 CRS 标签的图纸，不需要做转换，用 `cad.py assign-crs` 赋 CRS 即可，见 [CAD 图纸](cad-intake.md)。
