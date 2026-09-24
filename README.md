# 直播导播口令编排 API

基于 **Python + FastAPI** 的导播口令编排服务：接收一组带锚点依赖的直播导播口令，
解析每条口令在时间轴上的起止毫秒，并列出同一通道内半开区间重叠的冲突口令对；
另提供节目单延误分摊接口，按固定开场时刻与各段可压缩余量重排时长。

- 在线接口文档（Swagger UI）：<http://localhost:8000/docs>
- 健康检查：`GET /health`
- 编排接口：`POST /compile`
- 改动预览接口：`POST /preview`
- 节目单延误分摊：`POST /schedule/rebalance`

## 一条命令启动

需要已安装 Docker（含 Compose 插件）。在本目录执行：

```bash
docker compose up --build -d
```

启动后访问 <http://localhost:8000/docs> 即可在线调试。停止服务：

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

直播进行中根据固定开场时刻（**固定点**）分摊节目单延误。请求体为一个对象：

| 字段 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- |
| `segments` | array | 是 | 按播出顺序排列的节目段 |
| `actual_ends` | array | 否 | 已结束段的实际结束时刻；缺省表示节目尚未开始，按完整计划单分摊 |

`segments` 每一项：

| 字段 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- |
| `id` | string | 是 | 节目段唯一 id；不能为空 |
| `planned_duration_s` | integer | 是 | 计划时长（秒），必须为正数 |
| `min_duration_s` | integer | 是 | 最短时长（秒），必须为正数，且不得大于计划时长——任何段调整后不得低于此值 |
| `fixed_start_s` | integer \| null | 否 | 不可移动的固定开场时刻（距节目开播原点的秒数）；不填或 `null` 表示该段开场时刻可随前面段落移动 |

`actual_ends` 每一项：

| 字段 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- |
| `id` | string | 是 | 已结束的节目段 id |
| `end_s` | integer | 是 | 实际结束时刻（距节目开播原点的秒数，不能为负） |

分摊规则：

- 时间原点为 `0`，所有时刻均为**整数秒**。固定段的开场时刻不可移动，固定段
  本身按计划时长播出、**不参与压缩**。
- 每个固定点把节目单切成彼此独立的**分摊窗口**（光标与该固定开场时刻之间的
  可移动段）。最后一个已结束段的实际结束时刻（或原点 `0`）是当前调度光标。
- 窗口延误秒数 = `光标 + 窗口计划总时长 - 固定开场时刻`。
- 延误为正时，按各段**可压缩余量**（`planned_duration_s - min_duration_s`）
  的比例摊派压缩秒数；整除不尽的余数采用**最大余数法**：整数秒余数按小数份额
  从大到小补 1 秒，小数份额相同的按**节目顺序**（靠前的段优先）分配。
- 任何段调整后不得低于最短时长；窗口总余量不足以吸收延误时返回 `422`
  （`INSUFFICIENT_SLACK`），逐固定点给出无法追回的秒数。
- 延误为负（提前播出）时不压缩也不拉长，各段保持计划时长，提前量作为固定点
  前的空档在 `remaining_s` 中回传。
- 已结束段必须构成节目的**连续前缀**；节目单尾部（最后一个固定点之后）的
  可移动段没有约束，保持计划时长。

响应字段：

- `segments[].adjusted_duration_s`：调整后时长（已结束段为实际时长）。
- `segments[].cut_s`：被压缩的秒数；已结束段为相对计划的净值（延误为负）。
- `fixed_points[]`：每个固定点的分摊结果——
  `delay_s`（延误）、`recovered_s`（已追回）、`remaining_s`（固定点前剩余秒数：
  准点追回后为 `0`，提前播出时为可保留的空档）、`segment_ids`（参与分摊的段）。

### 请求示例：延误被追回

新闻段计划 600 秒、实际 660 秒结束（延误 60 秒）；其后访谈段计划 900 秒、
最短 720 秒（可压缩余量 180 秒）；天气段是固定点，固定开场 1500 秒。
光标 660 + 900 = 1560 > 1500，访谈段需压缩 60 秒：

```bash
curl -s http://localhost:8000/schedule/rebalance \
  -H 'Content-Type: application/json' \
  -d '{
    "segments": [
      {"id": "news",      "planned_duration_s": 600, "min_duration_s": 480},
      {"id": "interview", "planned_duration_s": 900, "min_duration_s": 720},
      {"id": "weather",   "planned_duration_s": 300, "min_duration_s": 240,
       "fixed_start_s": 1500}
    ],
    "actual_ends": [
      {"id": "news", "end_s": 660}
    ]
  }'
```

响应（`200`）：

```json
{
  "segments": [
    {
      "id": "news",
      "planned_duration_s": 600,
      "adjusted_duration_s": 660,
      "cut_s": -60,
      "fixed_start_s": null
    },
    {
      "id": "interview",
      "planned_duration_s": 900,
      "adjusted_duration_s": 840,
      "cut_s": 60,
      "fixed_start_s": null
    },
    {
      "id": "weather",
      "planned_duration_s": 300,
      "adjusted_duration_s": 300,
      "cut_s": 0,
      "fixed_start_s": 1500
    }
  ],
  "fixed_points": [
    {
      "id": "weather",
      "fixed_start_s": 1500,
      "segment_ids": ["interview"],
      "delay_s": 60,
      "recovered_s": 60,
      "remaining_s": 0
    }
  ]
}
```

### 请求示例：多段按余量比例分摊（最大余数法）

三段均可移动，计划时长均为 100 秒、最短分别为 90 / 70 / 70 秒
（余量 10 / 30 / 30，总余量 70）；固定点在 280 秒，窗口计划总时长 300 秒，
需追回 20 秒。份额为 `20*10/70 ≈ 2.857`、`20*30/70 ≈ 8.571`、
`20*30/70 ≈ 8.571`；先分底数 `2/8/8 = 18` 秒，余 2 秒：第一段小数份额
（60/70）最大先补 1 秒，后两段小数相同（40/70）按节目顺序由第二段再补 1 秒：

```bash
curl -s http://localhost:8000/schedule/rebalance \
  -H 'Content-Type: application/json' \
  -d '{
    "segments": [
      {"id": "a", "planned_duration_s": 100, "min_duration_s": 90},
      {"id": "b", "planned_duration_s": 100, "min_duration_s": 70},
      {"id": "c", "planned_duration_s": 100, "min_duration_s": 70},
      {"id": "f", "planned_duration_s": 1,   "min_duration_s": 1,
       "fixed_start_s": 280}
    ]
  }'
```

压缩结果为 `a: 97s、b: 91s、c: 92s`（压缩秒数 `3 / 9 / 8`），
`fixed_points[0].delay_s = recovered_s = 20`、`remaining_s = 0`。

### 请求示例：余量不足（422）

两段各只能压缩 10 秒（总余量 20 秒），固定点在 150 秒而计划总时长 200 秒，
需追回 50 秒、缺口 30 秒：

```bash
curl -s -w '\nHTTP %{http_code}\n' http://localhost:8000/schedule/rebalance \
  -H 'Content-Type: application/json' \
  -d '{
    "segments": [
      {"id": "a", "planned_duration_s": 100, "min_duration_s": 90},
      {"id": "b", "planned_duration_s": 100, "min_duration_s": 90},
      {"id": "f", "planned_duration_s": 1,   "min_duration_s": 1,
       "fixed_start_s": 150}
    ]
  }'
```

响应（`422`）：

```json
{
  "code": "INSUFFICIENT_SLACK",
  "message": "固定点 f 前可压缩余量不足：需追回 50 s，最多追回 20 s，无法追回 30 s",
  "details": {
    "unrecoverable_s": 30,
    "fixed_points": [
      {
        "id": "f",
        "fixed_start_s": 150,
        "segment_ids": ["a", "b"],
        "required_recovery_s": 50,
        "recoverable_s": 20,
        "shortage_s": 30
      }
    ]
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
| `INVALID_MIN_DURATION` | 仅 `POST /schedule/rebalance`：`min_duration_s` 非正，或大于 `planned_duration_s` |
| `INVALID_ACTUAL_END` | 仅 `POST /schedule/rebalance`：实际结束时刻非法（为负、同段重复提交、非连续前缀、结束不晚于实际开场） |
| `ACTUAL_END_UNKNOWN_ID` | 仅 `POST /schedule/rebalance`：实际结束时刻引用了不存在的节目段 id |
| `FIXED_START_CONFLICT` | 仅 `POST /schedule/rebalance`：已结束的固定段实际开场时刻与其 `fixed_start_s` 不一致 |
| `INSUFFICIENT_SLACK` | 仅 `POST /schedule/rebalance`：固定点前各段可压缩余量总和不足以吸收延误，`details.unrecoverable_s` 为无法追回的总秒数 |

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

核心编排逻辑位于 `app/compiler.py`（节目单分摊位于 `app/schedule.py`），仅使用
标准库，可直接运行单元测试：

```bash
python3 -m unittest discover -s tests
```

## 项目结构

```
.
├── app/
│   ├── __init__.py
│   ├── compiler.py      # 纯标准库的依赖解析 / 时间计算 / 冲突检测
│   ├── schedule.py      # 纯标准库的节目单延误分摊（固定点窗口 / 最大余数法）
│   ├── schemas.py       # Pydantic 请求与响应模型
│   └── main.py          # FastAPI 入口（/compile、/preview、/schedule/rebalance、/health、统一错误处理）
├── tests/
│   ├── test_compiler.py # 核心逻辑单元测试（标准库 unittest）
│   └── test_schedule.py # 节目单分摊单元测试（标准库 unittest）
├── requirements.txt
├── Dockerfile
└── docker-compose.yml
```
