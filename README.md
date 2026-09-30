# 合作办学课程版本服务

按**专业与招生批次**维护合作办学课程方案及其完整依赖（贡献方、教材依赖、技能标准、签署规则），
发布时锁定整个依赖版本；课程替代、紧急更正、学期迁移、撤回签署均**不得让已承诺旧培养路径的学生
适用方案漂移**；多人同时发布或依赖冲突时，**只能产生一个合法结果**。

纯 Python 标准库实现（Python 3.11+，SQLite + http.server，无第三方依赖）。

## 四个领域不变量如何落地

| 契约不变量 | 实现机制 |
| --- | --- |
| 培养版本 | 方案按 `(专业, 作用域[专业/招生批次], 版本号)` 版本化；已发布版本行不可变，任何修改都基于它新建修订草案（`based_on_id`）。 |
| 多方法定人数 | 签署规则逐贡献方规定 `min_signers`；会签阶段逐方统计独立签署人，任一方不足即 `quorum_not_met`，禁止发布。撤回在发布前删除签名并留痕，发布后禁止撤回。 |
| 学生适用性 | 学生**首次查询时**按规则解析并写入 `student_bindings` 永久冻结；之后课程替代、紧急更正、学期迁移、撤回、新版本都不改派。 |
| 依赖冻结 | 发布时对依赖闭包（课程、贡献方、教材、技能标准、签署规则）规范化计算 sha256，连同当时签名快照写入版本；查询时重算哈希自证未被篡改。 |

### 适用版本解析规则（每名学生的查询都说明"为何采用该版本"）

1. 只考虑生效日 **不晚于入学日** 的已发布版本；
2. **招生批次级优先于专业级**（精确匹配优先）；
3. 同级按 `生效日 → 版本号 → 发布时间 → 版本ID` 逐级决胜，结果唯一可复现。

查询响应 `why_this_version` 给出规则、作用域匹配、每个落选候选及落选原因；
`post_enrollment_drift` 列出入学后被隔离的全部变化（新版本、学时/考核/学期差异、被替代课程、
撤回记录），明确"这些变化不适用于该学生"。

### 并发与依赖冲突：只能有一个合法结果

- 生效槽位 `(专业, 批次范围, 生效日)` 上有数据库唯一索引；
- 写临界区取 `fcntl` 文件锁（跨进程）+ 进程内 RLock，事务用 `BEGIN IMMEDIATE`；
- **同槽位、同依赖闭包**：幂等，两个发布请求归一到同一合法版本；
- **同槽位、不同依赖闭包**：先提交者胜（线性化），后来者收 `publish_conflict`
  （槽位已有学生绑定时升级为 `dependency_conflict`），并提示改用新生效日期；
- 仲裁落败版本不占用生效槽位，因此不会进入任何学生的候选集。

### 三类受控演进

- **课程替代** `substitute`：整门课替换（编号/学时/考核可变），变更写入冻结的 `change_log`；
- **紧急更正** `correct`：仅限学时与考核方式，强制填写原因，前后值记入冻结的 `corrections`；
- **学期迁移** `migrate`：仅改开课学期。

三者都只生成新草案 → 新会签 → 新发布版本，从不触碰旧版本。

## 目录

- `domain/contract.json`：领域角色、状态、不变量与样例。
- `src/curriculum_service/`：服务端
  - `models.py` 课程值对象与常量；`operations.py` 三类受控演进（纯函数）；
  - `applicability.py` 适用版本解析（纯函数）；
  - `repository.py` SQLite schema 与仓储；`locking.py` 跨进程文件锁；
  - `canon.py` 规范化序列化与内容哈希；
  - `service.py` 领域服务（生命周期/会签/发布冻结/仲裁/适用性冻结）；
  - `http_api.py`、`__main__.py` JSON HTTP 接口。
- `tools/seed_demo.py`：端到端场景（旧生绑定旧方案后，学时 48→56、考查→考试、学期 2→3）。
- `tests/`：契约测试 + 24 项领域回归（含并发仲裁、依赖冲突、不漂移）。

## 运行

```bash
# 测试
python3 -m unittest discover -s tests -v
python3 -m compileall -q src tools tests
python3 tools/check_contract.py domain/contract.json

# 端到端演示（内置断言：旧学生不漂移，新版本只适用新生）
python3 tools/seed_demo.py

# HTTP 服务
python3 -m curriculum_service --host 127.0.0.1 --port 8000 --db data/curriculum.sqlite3
```

## HTTP 接口摘要

`POST /api/majors`、`POST /api/batches`、`POST /api/students`
`POST /api/drafts`、`POST /api/versions/{id}/revise|content|substitute|correct|migrate|submit|sign|revoke|publish`
`GET /api/versions/{id}`、`GET /api/versions/{id}/snapshot`、
`GET /api/students/{id}/resolution`、`GET /api/majors/{code}/versions`、`GET /api/events`

请求可用 `"actor"` 字段或 `X-Actor` 头标注操作人；错误统一返回 `{error, detail}`，
状态码 400/404/409/422。
