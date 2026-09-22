# 实验流程事件回放台

基于 **FastAPI + SQLite** 的实验流程事件回放系统：创建带版本号的流程、追加带时间戳的步骤事件，
在事件**乱序到达**或**撤销**后按确定性规则重放，查看**任意时刻的状态**与**两时刻差异**，并支持**导出回放结果**。

## 一条命令启动（Docker）

```bash
docker compose up --build
```

启动时自动完成依赖安装（镜像构建阶段）与 SQLite 数据库初始化（应用 lifespan 阶段），无需手工操作。

## 访问地址

| 入口 | 地址 |
|---|---|
| Web 界面 | http://localhost:8000 |
| 交互式 API 文档 (Swagger) | http://localhost:8000/docs |
| 健康自查示例 | `curl http://localhost:8000/api/flows` |

## 配置项（环境变量）

| 变量 | 默认值 | 说明 |
|---|---|---|
| `PORT` | `8000` | 服务监听端口（Compose 中映射为 `8000:8000`，改宿主机端口请编辑 `docker-compose.yml` 的 `ports`） |
| `DB_PATH` | `/app/data/replay.db`（容器）/ `data/replay.db`（本地） | SQLite 数据库文件路径，Compose 通过命名卷 `replay-data` 持久化 |

## 本地开发（不用 Docker）

```bash
pip install -r requirements.txt
uvicorn app.main:app --reload --port 8000
# 冒烟测试
python tests/smoke_test.py
```

## 回放规则

1. **排序**：事件按 `(event_time, seq, id)` 升序得到唯一回放顺序，与到达先后无关 —— 乱序写入不影响结果。
2. **撤销**：`undone` 事件取消其 `target_event_id` 指向的事件（仅当目标在回放顺序中位于 undo 之前）；被取消的事件不产生任何效果。
3. **应用**：`started → running`，`completed → completed`，`failed → failed`，`updated → 仅合并 payload`；所有非 undo 事件的 payload 均合并进步骤数据。
4. **时刻状态**：`at` 时刻的状态 = 仅让 `event_time <= at` 的事件参与上述流程。

## JSON API 一览

| 方法 | 路径 | 说明 |
|---|---|---|
| `POST` | `/api/flows` | 创建流程（`name`、`version`、`description`） |
| `GET` | `/api/flows` | 流程列表（含事件数） |
| `GET` / `DELETE` | `/api/flows/{id}` | 流程详情 / 删除 |
| `POST` | `/api/flows/{id}/events` | 追加事件（`step_id`、`event_type`、`event_time`、`seq`、`payload`、`target_event_id`） |
| `GET` | `/api/flows/{id}/events?order=received\|replay` | 到达顺序 / 回放顺序事件列表 |
| `GET` | `/api/flows/{id}/replay?at=<ISO时间>` | 重放任意时刻状态（缺省为最终状态） |
| `GET` | `/api/flows/{id}/diff?from=<t1>&to=<t2>` | 两时刻状态差异（新增 / 消失 / 变化） |
| `GET` | `/api/flows/{id}/export` | 导出完整回放结果 JSON（含逐时刻快照，附件下载） |

### 示例

```bash
# 创建流程
curl -X POST localhost:8000/api/flows -H 'Content-Type: application/json' \
  -d '{"name":"蛋白纯化","version":"v1.0"}'

# 乱序追加：先写 10:05 的完成事件，再写 10:00 的开始事件
curl -X POST localhost:8000/api/flows/1/events -H 'Content-Type: application/json' \
  -d '{"step_id":"配制溶液","event_type":"completed","event_time":"2026-09-22T10:05:00Z"}'
curl -X POST localhost:8000/api/flows/1/events -H 'Content-Type: application/json' \
  -d '{"step_id":"配制溶液","event_type":"started","event_time":"2026-09-22T10:00:00Z"}'

# 撤销 id=1 的完成事件
curl -X POST localhost:8000/api/flows/1/events -H 'Content-Type: application/json' \
  -d '{"step_id":"配制溶液","event_type":"undone","target_event_id":1}'

# 查看 10:03 时刻状态 / 两时刻差异 / 导出
curl 'localhost:8000/api/flows/1/replay?at=2026-09-22T10:03:00Z'
curl 'localhost:8000/api/flows/1/diff?from=2026-09-22T10:03:00Z&to=2026-09-22T10:15:00Z'
curl -OJ localhost:8000/api/flows/1/export
```

## 项目结构

```
app/
├── main.py            # FastAPI 路由与 lifespan 初始化
├── replay.py          # 回放引擎（排序 / 撤销 / 时刻状态 / 差异 / 快照）
├── database.py        # SQLite 连接与建表
├── models.py          # Pydantic 模型与时间解析
└── static/index.html  # 单页 Web 界面（原生 JS，无构建步骤）
tests/smoke_test.py    # 端到端冒烟测试
Dockerfile / docker-compose.yml
```

Web 界面支持：流程创建与切换、事件追加（可改时间模拟乱序）、一键填充“乱序+撤销”示例、
到达/回放双序事件表、时间轴滑块回放任意时刻状态、两时刻差异对比、回放结果导出下载。
