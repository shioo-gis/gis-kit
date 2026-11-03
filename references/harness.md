# Harness：发现、轨迹与语义检查

## 工具发现与原生调用

`gis.py list --json` 静态读取公开脚本的模块 docstring，并从现有 references 中寻找脚本引用；不导入全部 GIS 库，不探测所有后端，不维护注册表。`--search TEXT` 按命令、摘要与参考路径做文本筛选，不承担语义路由。命令与参数逐层使用原 help：

```bash
"$PY" "$GIS_SKILL/scripts/gis.py" list --json
"$PY" "$GIS_SKILL/scripts/gis.py" list --search network
"$PY" "$GIS_SKILL/scripts/gis.py" network --help
"$PY" "$GIS_SKILL/scripts/gis.py" inspect-data summary /work/data.gpkg --json
```

`gis.py <工具名> ...` 使用同一个解释器启动对应公开脚本；不改 cwd、参数、标准输出/错误或原 CLI 退出语义（信号终止映射为 128+信号）。拒绝路径穿越和 `_` 私有 worker；所有旧脚本入口保留。它不决定算法、不重试、不安装、不在任务间保持服务。直接调用原脚本也不自动记录轨迹。

`gis.py capabilities --json` 输出 `contract: capabilities`、`schema_version` 1 的 JSON：`engine_version`、支持 `--json` 的 `commands`、读写的文件格式及版本（`contracts`）和能力组（`features`），供外部诊断工具（如 gis-plugin 的 doctor）读取，gis-kit 自身不依赖它们。能力组只用 `find_spec` 做可导入性探测，不导入后端；`available` 不等于真实操作验收，环境核实仍用 `daily.py environment`。

`gis.py describe <path> --json` 输出给 Agent 看的元数据卡片，格式以 [describe.schema.json](describe.schema.json)（`contract: describe`，`schema_version` 1）为准；gis-kit 是该格式的唯一产出方，其它工具（如 gis-plugin 的注入 hook）只读取。

- 只读文件头与系统表，不读要素或像元；要素数或范围需扫描才能得到时为 `null`。
- 矢量给出图层（最多 20 个，`layer_count` 为总数）、几何类型、要素数、CRS、范围、字段（最多 50 个，`field_count` 为总数）；栅格给出尺寸、波段、数据类型、逐波段 NoData（NaN 记为 `"nan"`，未设置为 `null`）、分辨率、CRS、范围，以及全部波段解码后的字节数 `decoded_bytes`。
- `crs` 含 `id`（如 `EPSG:4490`，无法识别时为 `null`）、`name`、`geographic`、`units`；没有 CRS 时整个为 `null`。
- `id` 为 `null` 时，可能另有 `horizontal`：复合坐标系（投影＋高程）的水平部分，或只能低置信度匹配的坐标系（如 ESRI 写的带号前缀高斯投影），推断出的 EPSG。只有候选 EPSG 的投影参数和地理坐标系都与原坐标系相同时才给出，`inferred` 恒为 `true`，`basis` 写明依据，`vertical` 为高程部分的名称。读取方要用它时，应说明这是推断。
- 后缀只决定先试矢量还是栅格，一种打不开就试另一种，所以 `.jpg`、`.png` 加世界文件按栅格描述：`transform` 给仿射，`world_file` 给来源，世界文件不含 CRS，所以 `crs` 为 `null` 并有 `crs_missing`。没有仿射时 `transform`、`resolution`、`extent` 都是 `null`；也没有 GCP 和 RPC 时另有 `georeference_missing`。
- CSV 的字段类型由 GDAL 看文件开头推断（`field_types_sampled_bytes` 为字节数），后面的行可能不符；这是唯一会读到记录的情况，读的量固定。
- `warnings` 只列确定性事实（`crs_missing`、`nodata_unset`、`georeference_missing`），不给建议。
- `revision`（大小与修改时间）供读取方判断卡片是否过期。
- 兼容变更只新增可选字段，读取方须忽略不认识的字段；不兼容变更才升 `schema_version`。

深入查看仍用 `inspect-data`。

`gis.py --memory-budget SIZE TOOL ...` 为该次调用设置可选的读前内存预算；超限时退出码 3，stderr 最后一行是 `GIS_KIT_ERROR ` 加机读 JSON。契约见 [内存预算](memory-budget.md)。

`daily.py` 与 `recipe.py` 共用现有 `_recipe_operations.py` 中的操作集合，避免可执行操作在 recipe 中漏列；network 适配沿用源拓扑接口的 `--access-areas` / `--barriers`。`batch.py` 的独立文件并行与 recipe 的有依赖顺序缓存继续分开，未为入口形式合并。

## 显式 execution trace

```bash
"$PY" "$GIS_SKILL/scripts/gis.py" --trace /work/trace daily grid /work/study.gpkg \
  --params /work/grid.json --output /work/grid
"$PY" "$GIS_SKILL/scripts/gis.py" --trace /work/trace daily profile /work/grid/result.gpkg \
  --params /work/profile.json --output /work/profile
```

全局 `--trace` 放在工具名之前。目录由调用者选择，置于仓库和输出包之外；不写中央日志或环境变量默认日志。每次调用一份随机 ID 的 JSON，记录解释器、cwd、参数列表、开始/结束时间及退出码。命令启动前发布 `started`，结束后原子替换本次记录；并行调用不争抢同一 JSONL。强制杀死进程可能留下 `started`，这种记录不得转换为 recipe。不提供自动恢复、后台监控或耐掉电保证。

只有现有固定 recipe adapter 能识别的调用，才额外保留解析后的参数快照（参数文件上限 1 MiB）、明确输入及参数文件哈希、实现指纹、输出文件清单/哈希、原状态和单产物提示。输入路径及显式后端目录沿原 CLI 的 cwd 语义解析；参数文件后来修改不会改写已记录参数。调用期间输入、参数或实现改变、原结果 hold/unknown、缺少结果包证据均不可提升为 recipe。多文件 Shapefile、外部栅格 sidecar 等沿用现有 fingerprint 限制；不发明新缓存契约。文件指纹复用 `_delivery.py` 的标准库实现，daily/recipe 的原 sidecar 边界保持不变；轨迹准备与整理不为计算哈希而加载 GIS 库。

其余命令、help、失败和无法建立安全快照的调用保留调用级元数据及 `recipe_eligible:false`。非有限数值或不可序列化的参数快照同样降为元数据记录，原 CLI 自行接受或拒绝参数，trace 不先因 JSON 写出失败而阻断调用。不截获子进程内部的每个操作，不保存 stdout/stderr、整套环境变量、输入数据副本或完整结果属性。已知凭据 flag、敏感 JSON 键、带查询/用户信息的 URL 做尽力脱敏；含此类参数的调用不生成 recipe 快照。任意自由文本和业务参数仍可能敏感，这不构成保密或 DLP 保证。

记录失败不会替底层旧 CLI 提供回滚；输出契约仍由原工具负责。成功退出也只表明进程完成，`recipe_eligible` 和源状态须分别检查。

## 从真实轨迹形成原有 recipe

```bash
"$PY" "$GIS_SKILL/scripts/gis.py" recipe from-trace /work/trace --output /work/draft.json
"$PY" "$GIS_SKILL/scripts/gis.py" recipe from-trace --help
# 审阅输入角色、参数、验证条件及 candidate 后，再用原 recipe 执行器：
"$PY" "$GIS_SKILL/scripts/gis.py" recipe /work/draft.json --cache /work/cache --output /work/replay
```

输出是既有 **schema_version:1** recipe 草稿；创建时拒绝覆盖且不执行，`from-trace` 本身仅需 Python 标准库。保留实际参数，将明确文件转换为 `input_ref`，以路径和内容哈希连接较早步骤的真实产物。后续仍可使用原 `--bindings`、variants、缓存指纹、seal、回读验证与失败不发布机制；实际执行和 GIS 文件回读时才加载对应依赖，完整契约见 [重复交付](repeated-delivery.md)。不增加 shell replay、插件、DAG 或另一套执行器。

默认要求目录内全部记录可转换。含探查、失败、中断或不受支持调用时明确拒绝；Agent 用 `--select ID ID ...` 选出有效序列，结果列明被排除的 ID。选择后仍按实际执行时间排列，重叠调用不能被猜成顺序链。转换前重新核对记录输入与产物，变更/消失就拒绝；此日志是本地证据，不是包含原始数据的便携归档或对恶意写者的防篡改边界。

现有 recipe 每步只向下游暴露一个选定的顶层产物：优先从后续调用确定该产物，否则使用结果记录提示或唯一数据文件。终端多产物需 `--artifact ID=FILENAME` 指定；同一步多产物被后续分别消费、或消费其嵌套目录内的产物时拒绝，交给 Agent 按现有能力整理，不将这些依赖静默变成外部输入。不会为绕过此限制增加多输出引用语言。

原生 `candidate` 仍为 candidate；草稿明确写 `allow_candidate:false`，Agent 审阅后才可按原契约允许候选继续计算，不能据此授予 accepted。hold 不可绕过。recipe 的 `record.status=complete` 标为 `status_scope:execution_only`；步骤 dossier 分开记录 `process_status`、`source_status`、`source_status_present` 及直接上游状态。旧字段 `execution_status` 曾保存源状态，继续作为兼容别名保留原值；新调用者使用 process/source 字段区分执行与语义，避免旧调用者把 candidate 误读成 complete。缺失源状态及兼容别名保持 null 且 presence=false，不默认成 complete。全部原始 record 仍随成果保留；没有推断原始外部输入的状态或构建全局证据图。

## 按明确边界做语义检查

```bash
# 相同稳定 ID 的属性检查；行顺序可以不同。
"$PY" "$GIS_SKILL/scripts/gis.py" semantic-check /work/before.gpkg /work/after.gpkg \
  --id object_id --fields status review_state value
# 多图层必须指定 --before-layer / --after-layer。
# 结果记录或不同结构的局部检查：显式选择同一 JSON pointer。
"$PY" "$GIS_SKILL/scripts/gis.py" semantic-check /work/before.json /work/after.json \
  --pointers /status /review/state
```

表模式支持 CSV、JSON 对象数组、GeoJSON properties，以及 Pyogrio 可读取的属性表/矢量图层；JSON pointer 模式仅需标准库。ID 按字符串身份匹配，拒绝缺失、布尔和重复 ID，不按位置对齐。选择的上游字段/pointer 必须存在；CSV 或矢量空表也按字段元数据核对，选错字段返回输入错误。合法空输入明确报告没有保护值。下游字段、值或带受保护状态的对象消失会产生 finding。

保护 `unknown`、`candidate`、`spatial_candidate`、`hold`；null、空字符串、unknown 视为等价的未知表达，合法 0/false 不视为未知。`--states` 可追加任务特定状态。保护值变化、未知被填成 0、candidate/hold 被改成 accepted/complete 都能报告；不会给保护状态建立通用高低次序，candidate→hold 也要求显式审阅。

输出包含具体 ID/字段或 pointer、变化前后值和 `protected_values_checked`。退出码 0 为所选范围无 finding，1 为发现变化，2 为输入/选择错误。没有保护值时明确 `no_protected_values`，不能当作全局语义通过。

这是 Agent 可放在转换、筛选、交付边界上的显式诊断，没有偷偷插入全部算子。合法删行、汇总、重编号、拆分或状态审核的映射/解释由任务决定；不自动修复或批准，不比较未指定字段，不证明栅格 mask、unknown 权重总量或地理真值守恒。属性表整体读入，未承诺大规模流式检查。
