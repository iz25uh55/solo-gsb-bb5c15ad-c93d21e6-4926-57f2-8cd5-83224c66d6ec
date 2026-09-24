# 直播导播口令编排 API

基于 **Python + FastAPI** 的导播口令编排服务：接收一组带锚点依赖的直播导播口令，
解析每条口令在时间轴上的起止毫秒，并列出同一通道内半开区间重叠的冲突口令对；
直播进行中还可对节目单做延误分摊，在不移动固定开场时刻的前提下把延误按各段
可压缩余量比例分配给未播段。

- 在线接口文档（Swagger UI）：<http://localhost:8000/docs>
- 健康检查：`GET /health`
- 编排接口：`POST /compile`
- 改动预览接口：`POST /preview`
- 节目单延误分摊接口：`POST /schedule/rebalance`

## 一条命令启动

需要已安装 Docker（含 Compose 插件）。在本目录执行：

```bash
docker compose up --build -d
```

启动后访问 <http://localhost:8000/docs> 即可在线调试。也可以用 curl 验证：

```bash
curl -s http://localhost:8000/health
# {"status":"ok"}
```

停止服务：

```bash
docker compose down
```

> 镜像在构建阶段执行 `pip install -r requirements.txt`，**启动容器时依赖已安装完毕**，
> 无需进入容器手动安装。

## 必要配置

| 配置项 | 说明 | 默认值 |
| --- | --- | --- |
| 容器监听端口 | 固定 `8000`（由 `uvicorn --port 8000` 指定） | `8000` |
| 宿主机映射端口 | Compose 读取环境变量 `PUBLISH_PORT`，映射为 `${PUBLISH_PORT}:8000` | `8000` |
| 依赖安装 | `Dockerfile` 构建时执行 `pip install -r requirements.txt` | 自动 |

如需改用宿主机 9000 端口：

```bash
PUBLISH_PORT=9000 docker compose up --build -d
```

服务无数据库、无外部依赖，不需要额外配置环境变量。

## 接口约定：`POST /compile`

请求体为口令数组，每一项字段：

| 字段 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- |
| `id` | string | 是 | 口令唯一 id；不能为空，不能使用保留锚点名 `START` |
| `channel` | string | 是 | 通道标识；只有同通道口令之间才检测冲突 |
| `anchor` | string | 否 | 锚点，缺省 `START`；可填 `START`（时间原点 0 ms）或另一条口令的 id |
| `offset_ms` | integer | 否 | 相对 **锚点结束时刻** 的偏移（毫秒），缺省 `0`，允许为负 |
| `duration_ms` | integer | 是 | 持续时长（毫秒），必须为正数 |

时间与冲突规则：

- 虚拟锚点 `START` 的起点和结束时刻均为 `0`。
- 口令 `x` 的起点 = 锚点口令的结束时刻 + `offset_ms`，结束时刻 = 起点 + `duration_ms`。
- 允许负偏移，但解析出的起点不得小于 `0`，否则返回 `NEGATIVE_START`。
- `timeline` 按 `(start_ms, id)` 升序排列。
- 同一通道内两个半开区间 `[start_ms, end_ms)` 发生重叠即为冲突；
  **首尾相接（前一个 end 等于后一个 start）不算冲突**；不同通道互不影响。
- 冲突对对内按 id 排序（`a < b`），所有冲突对之间按 `(a, b, channel)` 排序。

### 请求示例

```bash
curl -s http://localhost:8000/compile \
  -H 'Content-Type: application/json' \
  -d '[
    {"id": "a", "channel": "main", "anchor": "START", "offset_ms": 0,    "duration_ms": 1000},
    {"id": "b", "channel": "main", "anchor": "a",     "offset_ms": 0,    "duration_ms": 500},
    {"id": "c", "channel": "main", "anchor": "a",     "offset_ms": -200, "duration_ms": 500},
    {"id": "d", "channel": "sub",  "anchor": "START", "offset_ms": 0,    "duration_ms": 1000}
  ]'
```

对应时间：

- `a`：`[0, 1000)`
- `b`：相对 `a` 结束时刻（1000）+ 0 → `[1000, 1500)`，与 `a` 首尾相接，不冲突
- `c`：相对 `a` 结束时刻 -200 → `[800, 1300)`，与 `a`、`b` 均在 `main` 通道重叠
- `d`：`[0, 1000)`，但属于 `sub` 通道，不参与冲突

响应示例（`200`）：

```json
{
  "timeline": [
    {
      "id": "a",
      "channel": "main",
      "anchor": "START",
      "offset_ms": 0,
      "duration_ms": 1000,
      "start_ms": 0,
      "end_ms": 1000
    },
    {
      "id": "d",
      "channel": "sub",
      "anchor": "START",
      "offset_ms": 0,
      "duration_ms": 1000,
      "start_ms": 0,
      "end_ms": 1000
    },
    {
      "id": "c",
      "channel": "main",
      "anchor": "a",
      "offset_ms": -200,
      "duration_ms": 500,
      "start_ms": 800,
      "end_ms": 1300
    },
    {
      "id": "b",
      "channel": "main",
      "anchor": "a",
      "offset_ms": 0,
      "duration_ms": 500,
      "start_ms": 1000,
      "end_ms": 1500
    }
  ],
  "conflicts": [
    {
      "channel": "main",
      "a": "a",
      "b": "c",
      "a_interval": [0, 1000],
      "b_interval": [800, 1300]
    },
    {
      "channel": "main",
      "a": "b",
      "b": "c",
      "a_interval": [1000, 1500],
      "b_interval": [800, 1300]
    }
  ]
}
```

## 接口约定：`POST /preview`

在不落地任何数据的前提下，预览「修改一条现有口令的 `offset_ms` / `duration_ms`」
对整条编排的影响。服务端会分别编译**原编排**和**改动后编排**，再对比差异。

请求体为一个对象：

| 字段 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- |
| `commands` | array | 是 | 口令数组，每一项的字段与 `POST /compile` 的请求体完全一致 |
| `change` | object | 是 | 一项改动，见下表 |

`change` 字段：

| 字段 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- |
| `id` | string | 是 | 要改动的现有口令 id（必须存在于 `commands` 中） |
| `offset_ms` | integer | 二选一 | 新的偏移；不填则保留原值 |
| `duration_ms` | integer | 二选一 | 新的持续时长（仍须为正数）；不填则保留原值 |

`offset_ms` 与 `duration_ms` **至少提供一个**；未提供的字段以及其他字段
（`channel`、`anchor` 等）一律保留原值，原请求数据不会被修改。原编排与改动后编排
均沿用与 `/compile` 相同的字段校验与 `422` 错误格式。

响应字段：

- `changed_ids`：时间起止（`start_ms` / `end_ms`）发生变化的口令 id，
  **包含锚点依赖的传导**（被改动口令的下游口令会一起移动），按 id 升序。
- `added_conflicts`：改动后**新增**的同通道冲突对，区间为改动后版本。
- `removed_conflicts`：改动后**消失**的同通道冲突对，区间为原版本。

冲突对以 `(channel, a, b)` 判定身份（同一条冲突对若只是区间变化，不算新增也不算消失）；
每个冲突对仍携带 `a_interval` / `b_interval`，两个列表均按 `(a, b, channel)` 排序。

### 请求示例

```bash
curl -s http://localhost:8000/preview \
  -H 'Content-Type: application/json' \
  -d '{
    "commands": [
      {"id": "a", "channel": "main", "anchor": "START", "offset_ms": 0,    "duration_ms": 1000},
      {"id": "b", "channel": "main", "anchor": "a",     "offset_ms": 0,    "duration_ms": 500},
      {"id": "c", "channel": "main", "anchor": "a",     "offset_ms": -200, "duration_ms": 500},
      {"id": "d", "channel": "sub",  "anchor": "START", "offset_ms": 0,    "duration_ms": 1000}
    ],
    "change": {"id": "a", "offset_ms": 100}
  }'
```

改动把 `a` 的起点推迟到 100 ms：

- `a`：`[0, 1000)` → `[100, 1100)`。
- `b` 锚定 `a` 的结束时刻：`[1000, 1500)` → `[1100, 1600)`（依赖传导）。
- `c` 锚定 `a` 的结束时刻：`[800, 1300)` → `[900, 1400)`（依赖传导）。
- `d` 不依赖 `a`，时间不变。

原有的两个冲突对 `(main, a, c)`、`(main, b, c)` 改动后仍然重叠，身份不变。
响应示例（`200`）：

```json
{
  "changed_ids": ["a", "b", "c"],
  "added_conflicts": [],
  "removed_conflicts": []
}
```

下面这个改动把 `c` 的持续时长缩短为 100 ms：

```bash
curl -s http://localhost:8000/preview \
  -H 'Content-Type: application/json' \
  -d '{
    "commands": [
      {"id": "a", "channel": "main", "anchor": "START", "offset_ms": 0,    "duration_ms": 1000},
      {"id": "b", "channel": "main", "anchor": "a",     "offset_ms": 0,    "duration_ms": 500},
      {"id": "c", "channel": "main", "anchor": "a",     "offset_ms": -200, "duration_ms": 500},
      {"id": "d", "channel": "sub",  "anchor": "START", "offset_ms": 0,    "duration_ms": 1000}
    ],
    "change": {"id": "c", "duration_ms": 100}
  }'
```

`c` 由 `[800, 1300)` 缩短为 `[800, 900)`：与 `a`（`[0, 1000)`）仍重叠，
但与 `b`（`[1000, 1500)`）不再重叠，冲突对 `(b, c)` 消失，区间携带原版本：

```json
{
  "changed_ids": ["c"],
  "added_conflicts": [],
  "removed_conflicts": [
    {
      "channel": "main",
      "a": "b",
      "b": "c",
      "a_interval": [1000, 1500],
      "b_interval": [800, 1300]
    }
  ]
}
```

把 `c` 反过来延长（例如 `duration_ms: 800` → `[800, 1600)`）则不会产生新冲突对；
而把原本首尾相接的口令延长到越过对方起点时，新冲突会出现在 `added_conflicts` 中，
区间携带改动后的版本。

## 接口约定：`POST /schedule/rebalance`

直播进行中，已结束段的实际结束时刻往往偏离计划，节目单整体随之延误或提前。
本接口在**不移动任何固定开场时刻**的前提下，把延误按各段**可压缩余量**
（计划时长 − 最短时长）的比例分摊给固定点之前的未播段，返回调整后的各段时长。

请求体为一个对象：

| 字段 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- |
| `segments` | array | 是 | 按播出顺序排列的节目段，允许为空数组 |

`segments` 每一项字段：

| 字段 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- |
| `id` | string | 是 | 节目段唯一 id；不能为空 |
| `planned_duration_sec` | integer | 是 | 计划时长（秒），正数且不短于 `min_duration_sec` |
| `min_duration_sec` | integer | 是 | 最短时长（秒），正数；延误分摊时的压缩下限 |
| `fixed_start_at_sec` | integer | 否 | 固定开场时刻（秒，相对节目单原点 0）；不可移动，缺省表示非固定点 |
| `actual_end_at_sec` | integer | 否 | 已结束段的实际结束时刻（秒）；缺省表示该段未播 |

分摊规则：

- **固定点切窗**：带 `fixed_start_at_sec` 的段是固定点，它把节目单切成若干窗口，
  每个窗口内的段必须在窗口末尾的固定点时刻之前播完；最后一个固定点之后的段
  没有窗口约束，保持计划时长。
- **可用时间**：窗口内已结束段的最后一个 `actual_end_at_sec` 决定未播段的可用时间
  （可用时间 = 固定点时刻 − 该实际结束时刻；窗口内无已结束段时从窗口起点算起）。
- **延误分摊**：窗口内未播段计划总时长超出可用时间的部分即延误，按各段可压缩余量
  比例分摊压缩；整数秒余数按**小数部分从大到小**分配，**同分按节目顺序**（靠前者先得）；
  任何段不会被压到 `min_duration_sec` 以下（余量为 0 的段不参与分摊）。
- **提前不拉伸**：可用时间有富余时不拉长任何段，富余秒数作为该固定点的
  `remaining_sec` 返回（可追回时间）。
- **已结束段**必须是节目单的连续前缀，其实际结束时刻单调不减；已结束段不参与压缩，
  响应中保持计划时长。
- **余量不足**：延误超过窗口内可压缩余量总和时，返回 `422` +
  `INSUFFICIENT_MARGIN`，`details.unrecoverable_sec` 为无法追回的秒数。

响应字段：

- `segments`：按节目顺序排列，每项含 `id`、`planned_duration_sec`、
  `adjusted_duration_sec`（分摊后时长）、`compressed_sec`（被压缩的秒数）、
  `finished`（是否已结束）。
- `fixed_points`：每个「前面有窗口」的固定点一项，含 `segment_id`、
  `fixed_start_at_sec`、`absorbed_sec`（窗口内被压缩吸收的延误秒数）、
  `remaining_sec`（固定点前剩余秒数，发生压缩时为 0）。

### 请求示例（延误按比例分摊）

`opening` 计划 600s 但实际 660s 才结束（晚 60s），`news` 必须在 1800s 准点开场：

```bash
curl -s http://localhost:8000/schedule/rebalance \
  -H 'Content-Type: application/json' \
  -d '{
    "segments": [
      {"id": "opening",   "planned_duration_sec": 600, "min_duration_sec": 300, "actual_end_at_sec": 660},
      {"id": "interview", "planned_duration_sec": 900, "min_duration_sec": 600},
      {"id": "music",     "planned_duration_sec": 600, "min_duration_sec": 300},
      {"id": "news",      "planned_duration_sec": 300, "min_duration_sec": 300, "fixed_start_at_sec": 1800}
    ]
  }'
```

可用时间 = 1800 − 660 = 1140s，计划还需 900 + 600 = 1500s，延误 360s；
`interview` 与 `music` 余量均为 300s，各摊 180s。响应示例（`200`）：

```json
{
  "segments": [
    {"id": "opening",   "planned_duration_sec": 600, "adjusted_duration_sec": 600, "compressed_sec": 0,   "finished": true},
    {"id": "interview", "planned_duration_sec": 900, "adjusted_duration_sec": 720, "compressed_sec": 180, "finished": false},
    {"id": "music",     "planned_duration_sec": 600, "adjusted_duration_sec": 420, "compressed_sec": 180, "finished": false},
    {"id": "news",      "planned_duration_sec": 300, "adjusted_duration_sec": 300, "compressed_sec": 0,   "finished": false}
  ],
  "fixed_points": [
    {"segment_id": "news", "fixed_start_at_sec": 1800, "absorbed_sec": 360, "remaining_sec": 0}
  ]
}
```

### 请求示例（整数秒余数分配）

延误 7s、三段余量各 3s：精确分摊值均为 `7 × 3 / 9 ≈ 2.33`，每段先分 2s，
余 1s；小数部分相同，按节目顺序补给最靠前的 `a`（`a` 压 3s，`b`、`c` 各压 2s）：

```bash
curl -s http://localhost:8000/schedule/rebalance \
  -H 'Content-Type: application/json' \
  -d '{
    "segments": [
      {"id": "a",    "planned_duration_sec": 103, "min_duration_sec": 100},
      {"id": "b",    "planned_duration_sec": 103, "min_duration_sec": 100},
      {"id": "c",    "planned_duration_sec": 103, "min_duration_sec": 100},
      {"id": "news", "planned_duration_sec": 50,  "min_duration_sec": 50, "fixed_start_at_sec": 302}
    ]
  }'
```

```json
{
  "segments": [
    {"id": "a",    "planned_duration_sec": 103, "adjusted_duration_sec": 100, "compressed_sec": 3, "finished": false},
    {"id": "b",    "planned_duration_sec": 103, "adjusted_duration_sec": 101, "compressed_sec": 2, "finished": false},
    {"id": "c",    "planned_duration_sec": 103, "adjusted_duration_sec": 101, "compressed_sec": 2, "finished": false},
    {"id": "news", "planned_duration_sec": 50,  "adjusted_duration_sec": 50,  "compressed_sec": 0, "finished": false}
  ],
  "fixed_points": [
    {"segment_id": "news", "fixed_start_at_sec": 302, "absorbed_sec": 7, "remaining_sec": 0}
  ]
}
```

### 请求示例（余量不足，422）

`opening` 拖到 1700s 才结束，距离 1800s 的 `news` 只剩 100s，而 `interview`
计划 500s、最多只能压缩 50s：

```bash
curl -s http://localhost:8000/schedule/rebalance \
  -H 'Content-Type: application/json' \
  -d '{
    "segments": [
      {"id": "opening",   "planned_duration_sec": 600, "min_duration_sec": 600, "actual_end_at_sec": 1700},
      {"id": "interview", "planned_duration_sec": 500, "min_duration_sec": 450},
      {"id": "news",      "planned_duration_sec": 300, "min_duration_sec": 300, "fixed_start_at_sec": 1800}
    ]
  }'
```

响应（`422`）：延误 400s，余量只有 50s，仍有 350s 无法追回：

```json
{
  "code": "INSUFFICIENT_MARGIN",
  "message": "固定点 news（1800s）前可压缩余量不足，仍有 350 秒无法追回",
  "details": {
    "unrecoverable_sec": 350,
    "required_compression_sec": 400,
    "available_margin_sec": 50,
    "fixed_point": {"segment_id": "news", "fixed_start_at_sec": 1800}
  }
}
```

## 错误处理

所有业务错误均返回 HTTP `422`，响应体统一为：

```json
{
  "code": "DEPENDENCY_CYCLE",
  "message": "检测到依赖环: a -> b -> a",
  "details": { "cycle": ["a", "b", "a"] }
}
```

错误码：

| `code` | 触发条件 |
| --- | --- |
| `INVALID_REQUEST` | 请求体不是合法的口令数组（字段缺失、类型错误、多余字段等） |
| `DUPLICATE_ID` | 数组中出现重复 `id` |
| `INVALID_ID` | `id` 为空，或使用了保留锚点名 `START` |
| `INVALID_CHANNEL` | `channel` 为空 |
| `INVALID_DURATION` | `duration_ms` 不是正数 |
| `MISSING_ANCHOR` | `anchor` 指向一个不存在的口令 id |
| `DEPENDENCY_CYCLE` | 锚点依赖构成环 |
| `NEGATIVE_START` | 解析出的口令起点小于 `0`（起点不允许为负） |
| `CHANGE_TARGET_NOT_FOUND` | 仅 `POST /preview`：`change.id` 在口令数组中不存在 |
| `INVALID_FIXED_START` | 仅 `POST /schedule/rebalance`：固定开场时刻为负，或未按节目顺序严格递增 |
| `INVALID_ACTUAL_END` | 仅 `POST /schedule/rebalance`：实际结束时刻为负、已结束段不构成连续前缀、结束时刻非单调不减，或已结束固定点段结束时刻早于其固定开场时刻 |
| `INSUFFICIENT_MARGIN` | 仅 `POST /schedule/rebalance`：固定点前可压缩余量不足，`details.unrecoverable_sec` 为无法追回的秒数 |

`POST /schedule/rebalance` 额外约定：

- `planned_duration_sec` 短于 `min_duration_sec`、或任一时长非正数时，返回
  `INVALID_DURATION`；重复 `id`、空 `id` 分别返回 `DUPLICATE_ID`、`INVALID_ID`。
- 已结束段的实际结束时刻越过下一个固定点、且窗口内已无可压缩的段时，同样返回
  `INSUFFICIENT_MARGIN`（无法追回的时间已成事实）。

`POST /preview` 额外约定：

- `change` 缺少 `id`、`offset_ms` / `duration_ms` 一个都没给、字段类型错误或出现多余字段时，
  返回 `INVALID_REQUEST`。
- 原编排本身非法时，直接返回原编排对应的错误码；改动后编排非法（如把时长改成 0、起点改为负）时，
  返回改动后编排对应的错误码（如 `INVALID_DURATION`、`NEGATIVE_START`）。

## 本地开发（不使用 Docker）

要求 Python 3.11+：

```bash
pip install -r requirements.txt
uvicorn app.main:app --reload --port 8000
```

核心编排逻辑位于 `app/compiler.py`，节目单延误分摊逻辑位于 `app/rebalance.py`，
均仅使用标准库，可直接运行单元测试：

```bash
python3 -m unittest discover -s tests
```

## 项目结构

```
.
├── app/
│   ├── __init__.py
│   ├── compiler.py        # 纯标准库的依赖解析 / 时间计算 / 冲突检测
│   ├── rebalance.py       # 纯标准库的节目单延误分摊（固定点切窗 + 余量比例分摊）
│   ├── schemas.py         # Pydantic 请求与响应模型
│   └── main.py            # FastAPI 入口（/compile、/preview、/schedule/rebalance、/health、统一错误处理）
├── tests/
│   ├── test_compiler.py   # 编排核心逻辑单元测试（标准库 unittest）
│   └── test_rebalance.py  # 延误分摊核心逻辑单元测试（标准库 unittest）
├── requirements.txt
├── Dockerfile
└── docker-compose.yml
```
