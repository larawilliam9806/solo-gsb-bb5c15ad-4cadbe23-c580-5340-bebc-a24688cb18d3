# 实验流程事件回放台（Event Replay Console）

基于 **Python + FastAPI + SQLite** 的实验流程事件溯源 / 重放工具：

- 🗂️ 创建**带版本号**的实验流程（同名同版本唯一）
- 📥 为流程追加**带时间戳的步骤事件**，事件可**乱序、晚到**
- 🔁 按固定规则**重放**，得到任意时刻（`at`）的流程状态
- ↩️ 支持两层「撤销」：`remove` 事件撤销某步骤、事件软撤销（revoked）后整体不参与重放
- 🔍 对比任意两个时刻的状态**差异**（新增 / 移除 / 变更 / 未变）
- ⬇️ 重放结果一键导出 **JSON / CSV**
- 🖥️ 自带零构建的 Web 界面，并提供完整 **JSON API**（含 Swagger 文档）

---

## 一条命令启动

需要安装 Docker（含 Compose 插件）：

```bash
docker compose up -d --build
```

启动后访问：

| 入口 | 地址 |
| --- | --- |
| Web 界面 | http://localhost:8000 |
| Swagger API 文档 | http://localhost:8000/docs |
| 健康检查 | http://localhost:8000/health |

> 宿主机 8000 端口被占用时：`HOST_PORT=9000 docker compose up -d --build`，然后访问 http://localhost:9000
>
> Compose 默认 `SEED_ON_STARTUP=true`，首次启动会自动写入一个「粒子加速实验」演示流程（含乱序与撤销事件），打开页面即可体验。

容器启动时入口脚本会自动完成：安装依赖（镜像构建阶段）→ **数据库初始化建表** → 写入演示数据（可选）→ 启动 uvicorn。数据持久化在 Docker volume `replay-data`（容器内 `/data/replay.db`）。

停止 / 清理：

```bash
docker compose down          # 停止（保留数据）
docker compose down -v       # 停止并删除数据卷
```

---

## 配置（环境变量）

| 变量 | 默认值 | 说明 |
| --- | --- | --- |
| `HOST` | `0.0.0.0` | uvicorn 监听地址 |
| `PORT` | `8000` | 容器内监听端口 |
| `HOST_PORT` | `8000` | **宿主机映射端口**（仅 compose 用） |
| `DB_PATH` | `/data/replay.db` | SQLite 数据库文件路径 |
| `SEED_ON_STARTUP` | `false`（compose 中为 `true`） | 启动时自动写入演示流程（幂等） |
| `MAX_DATA_BYTES` | `65536` | 单事件 `data` JSON 大小上限 |

---

## 事件模型与重放规则

### 事件字段

| 字段 | 含义 |
| --- | --- |
| `seq` | 追加序号，同流程内自增（落库顺序） |
| `ts` | 步骤时间戳（UTC ISO8601），**可以乱序、晚到** |
| `kind` | `add` 新增步骤 / `update` 更新步骤（`data` 浅合并）/ `remove` 撤销（移除）步骤 |
| `step_key` | 步骤业务键，同一流程内标识一个步骤 |
| `title` / `data` | 步骤标题与任意 JSON 对象 |
| `revoked` | 事件是否被软撤销；撤销后**不参与重放**（可随时恢复） |

### 重放规则

1. 仅取 `revoked = false` 的事件；
2. 按 **(时间戳 `ts`, 追加序号 `seq`) 升序**折叠——所以晚到的乱序事件会自动回到正确位置；同一时间戳以追加顺序为准；
3. `add` 写入步骤、`update` 覆盖标题并浅合并 `data`、`remove` 删除步骤；
4. `update` / `remove` 作用于当时不存在的步骤时记为 `ignored`（`target-missing`），不影响其他事件；
5. 指定 `at` 时只折叠 `ts <= at` 的事件，从而得到**任意时刻**的状态。

### 快速试用（curl）

```bash
# 创建流程
curl -s -X POST localhost:8000/api/processes -H 'Content-Type: application/json' \
  -d '{"name":"粒子加速实验","version":"1.0.0"}'

# 追加事件（时间戳故意乱序）
curl -s -X POST localhost:8000/api/processes/1/events -H 'Content-Type: application/json' \
  -d '{"kind":"add","step_key":"accelerate","title":"一级加速","ts":"2026-01-01T00:00:20Z"}'
curl -s -X POST localhost:8000/api/processes/1/events -H 'Content-Type: application/json' \
  -d '{"kind":"add","step_key":"inject","title":"粒子注入","ts":"2026-01-01T00:00:00Z"}'

# 重放最新状态 / 任意时刻状态 / 两时刻差异
curl -s localhost:8000/api/processes/1/replay | python3 -m json.tool
curl -s "localhost:8000/api/processes/1/replay?at=2026-01-01T00:00:10Z"
curl -s "localhost:8000/api/processes/1/diff?from=2026-01-01T00:00:10Z&to=2026-01-01T00:00:30Z"

# 撤销 / 恢复事件（软删除，重放随即变化）
curl -s -X PATCH localhost:8000/api/processes/1/events/2 -H 'Content-Type: application/json' -d '{"revoked":true}'

# 导出
curl -OJ localhost:8000/api/processes/1/export.json
curl -OJ localhost:8000/api/processes/1/export.csv
```

---

## API 一览

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| `GET` | `/health` | 健康检查 |
| `POST` | `/api/seed` | 写入演示流程（已存在返回 409） |
| `GET` | `/api/processes` | 流程列表（含事件数、最近时间戳） |
| `POST` | `/api/processes` | 创建带版本号的流程 |
| `GET` | `/api/processes/{pid}` | 流程详情 + 全部事件 |
| `DELETE` | `/api/processes/{pid}` | 删除流程（级联删除事件） |
| `POST` | `/api/processes/{pid}/events` | 追加步骤事件（`ts` 可乱序，缺省为当前 UTC） |
| `PATCH` | `/api/processes/{pid}/events/{id}` | 撤销 / 恢复事件 `{"revoked": true|false}` |
| `DELETE` | `/api/processes/{pid}/events/{id}` | 硬删除单个事件 |
| `GET` | `/api/processes/{pid}/replay?at=` | 重放到任意时刻（缺省最新） |
| `GET` | `/api/processes/{pid}/diff?from=&to=` | 两时刻状态差异（均可选） |
| `GET` | `/api/processes/{pid}/export.json?at=` | 导出完整重放结果 JSON |
| `GET` | `/api/processes/{pid}/export.csv?at=` | 导出最终状态表 CSV（带 UTF-8 BOM） |

重放响应包含：`state`（最终步骤列表）、`applied`（生效事件）、`skipped`（晚于 `at` 被跳过）、`ignored`（已撤销 / 目标缺失）及计数汇总。

---

## 本地开发（不用 Docker）

要求 Python 3.11+：

```bash
pip install -r requirements-dev.txt

# 初始化数据库（可选：同时写入演示数据）
SEED_ON_STARTUP=true DB_PATH=./data/replay.db python -m app.initdb

# 启动（默认 0.0.0.0:8000）
uvicorn app.main:app --reload

# 运行测试（14 个用例：引擎规则 + 全链路 API + 导出）
python -m pytest -q
```

## 项目结构

```
├── app/
│   ├── main.py       # FastAPI 路由：流程 / 事件 / 重放 / 差异 / 导出 / 页面
│   ├── replay.py     # 纯函数重放引擎：排序折叠、任意时刻、状态差异
│   ├── db.py         # SQLite 连接、建表迁移、CRUD
│   ├── schemas.py    # Pydantic 请求模型
│   ├── seed.py       # 演示数据（乱序 + 撤销场景）
│   ├── initdb.py     # 启动初始化脚本（python -m app.initdb）
│   └── config.py     # 环境变量配置
├── static/index.html # 单文件 Web 界面（原生 JS，无构建）
├── tests/            # pytest 测试
├── docker/entrypoint.sh  # 容器入口：建库 → 启动
├── Dockerfile
├── docker-compose.yml
└── requirements.txt
```
