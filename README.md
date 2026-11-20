# 实验室 FHIR R4 报告接入研究库（Lab FHIR Research Ingest API）

FastAPI + PostgreSQL 实现的 FHIR R4 事务型 Bundle 接入服务。检验科报告包以
transaction Bundle 提交，服务在**单数据库事务**内完成条件建患、观测/报告的
新增或版本化更新，并通过**接入单号幂等账本**保证“同内容重试复用首次回执、
异内容拒绝、响应丢失/进程重启不重复应用”。

## 1. 一键启动（Docker Compose）

```bash
docker compose up --build
```

- API 与交互式文档：<http://localhost:8000/docs>（Swagger UI，可直接在页面上发起 POST /fhir）
- OpenAPI：<http://localhost:8000/openapi.json>
- 健康检查：<http://localhost:8000/health>
- PostgreSQL：`localhost:5432`，库 `researchdb`，用户/密码 `research/research`

首次启动时 API 容器会等待数据库就绪并自动建表（`app/db_init.py`）。

## 2. 提交报告包

`POST /fhir`，请求头必须携带接入单号：

```
Idempotency-Key: LAB-ACC-1001
Content-Type: application/json
```

示例（注意条目故意乱序：报告 → 观测 → 患者，依赖 `urn:uuid` 前向引用）：

```json
{
  "resourceType": "Bundle",
  "type": "transaction",
  "entry": [
    {
      "fullUrl": "urn:uuid:3c3f9d7e-0003-4b2a-9e1a-000000000001",
      "resource": {
        "resourceType": "DiagnosticReport",
        "status": "final",
        "code": {"coding": [{"system": "http://loinc.org", "code": "24323-8"}]},
        "subject": {"reference": "urn:uuid:3c3f9d7e-0001-4b2a-9e1a-000000000001"},
        "result": [
          {"reference": "urn:uuid:3c3f9d7e-0002-4b2a-9e1a-000000000001"}
        ]
      },
      "request": {"method": "POST", "url": "DiagnosticReport"}
    },
    {
      "fullUrl": "urn:uuid:3c3f9d7e-0002-4b2a-9e1a-000000000001",
      "resource": {
        "resourceType": "Observation",
        "status": "final",
        "code": {"coding": [{"system": "http://loinc.org", "code": "2345-7"}]},
        "subject": {"reference": "urn:uuid:3c3f9d7e-0001-4b2a-9e1a-000000000001"},
        "valueQuantity": {"value": 5.5, "unit": "mmol/L"}
      },
      "request": {"method": "POST", "url": "Observation"}
    },
    {
      "fullUrl": "urn:uuid:3c3f9d7e-0001-4b2a-9e1a-000000000001",
      "resource": {
        "resourceType": "Patient",
        "identifier": [{"system": "http://hospital/mrn", "value": "MRN-001"}]
      },
      "request": {"method": "POST", "url": "Patient"}
    }
  ]
}
```

### 更新（带 If-Match 的乐观锁）

```json
{
  "resourceType": "Bundle",
  "type": "transaction",
  "entry": [
    {
      "resource": {
        "resourceType": "Observation",
        "id": "<已有观测id>",
        "status": "final",
        "code": {"coding": [{"code": "2345-7"}]},
        "subject": {"reference": "Patient/<患者id>"},
        "valueQuantity": {"value": 6.8, "unit": "mmol/L"}
      },
      "request": {
        "method": "PUT",
        "url": "Observation/<已有观测id>",
        "ifMatch": "W/\"1\""
      }
    }
  ]
}
```

- `ifMatch` 与当前版本不一致 → `412 Precondition Failed`，整包拒绝。
- 更新包内引用已存在资源请使用绝对引用（`Patient/<id>`、`Observation/<id>`）；
  `urn:uuid` 仅在同一 Bundle 内、且对应条目存在时有效。

## 3. 回执与读取接口

提交成功返回 `transaction-response` Bundle：

- `entry[].response`：FHIR 风格的 status / etag / location；
- `resourceMap[]`：按请求顺序给出每个条目的 `resourceType / id / version / action / fullUrl` 映射。

读取接口（均支持按患者维度）：

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/patients?identifier=system|value` | 按标识查患者 |
| GET | `/patients/{id}` | 患者摘要（含报告/观测计数） |
| GET | `/patients/{id}/observations` | 该患者全部观测 |
| GET | `/patients/{id}/diagnostic-reports` | 该患者全部报告（含观测 id 列表） |
| GET | `/observations/{id}` | 单个观测（响应头带 `ETag: W/"v"`） |
| GET | `/diagnostic-reports/{id}` | 单个报告 |
| GET | `/observations/{id}/_history` | 观测全部历史版本 |
| GET | `/diagnostic-reports/{id}/_history` | 报告全部历史版本 |
| GET | `/patients/{id}/_history` | 患者历史版本 |

## 3.1 报告转挂（更正挂错患者的报告）

数据员核对发现报告挂错患者时，调用：

```
POST /diagnostic-reports/{report_id}/reassign
Content-Type: application/json

{
  "expectedVersion": 1,                  // 核对时看到的报告版本，也接受 ETag 字符串 "W/\"1\""
  "targetPatientId": "<正确PatientId>" // 正确的患者 ID
}
```

规则：

1. **只受理引用不超过 8 条观测的报告**（可用 `MAX_REASSIGN_OBSERVATIONS` 调整），
   超限 → `422`。
2. **只被本报告引用的观测保留原 ID**，随报告一起改挂到目标患者（观测版本 +1）。
3. **被其他报告共享的观测不移动**：按当前内容为目标患者复制一条新观测（新 ID、
   版本 1），本报告改引副本，其他报告仍引用原观测。
4. 报告正文（subject/result/meta）、`report_links` 关联与所有新资源版本在**同一
   事务**提交；历史版本（`/_history`）继续可查。
5. **回执**列出每条观测更正前后的 ID：

```json
{
  "receipt": "diagnostic-report-reassign",
  "reportId": "<报告id>",
  "reportVersion": 2,
  "reportEtag": "W/\"2\"",
  "fromPatientId": "<原患者id>",
  "toPatientId": "<目标患者id>",
  "observations": [
    {"action": "copied", "beforeId": "<原观测id>", "afterId": "<副本id>", "version": 1},
    {"action": "moved",  "beforeId": "<观测id>",  "afterId": "<观测id>",  "version": 2}
  ]
}
```

6. **版本过期**（`expectedVersion` 与当前报告版本不一致）→ `412`，拒绝转挂；
   成功后同一版本号重试也会得到 `412`，即转挂天然只生效一次。
7. **失败不留痕**：任何校验/并发失败都整体回滚，不会留下副本、新版本或关联
   变更。
8. **并发语义**：转挂先按 id 升序锁定相关观测行、再锁报告行（与 `POST /fhir`
   管线一致的加锁顺序），因此与“同时更正这些观测的 Bundle”或“同时新增引用的
   Bundle”在 PostgreSQL 中的提交结果等同于某种依次执行；若数据库检测到死锁/
   锁冲突，本次转挂返回 `409`，重新读取报告后重试即可。

目标患者不存在 → `404`；报告不存在 → `404`；目标患者即当前患者 → `422`；
请求体缺字段/类型错误 → `422`（OperationOutcome）。

演示：`docker compose up --build` 后打开 <http://localhost:8000/docs>，在
`reassign` 分组下直接试用；或运行 `./scripts/demo_reassign.sh` 查看完整命令行
演示（建患 → 建报告 → 版本过期拒绝 → 转挂 → 回执与历史核对）。

## 4. 业务规则与失败语义

1. **条目上限 20**，仅允许 `Patient / Observation / DiagnosticReport`；
   超限或类型不符 → `400`。
2. **患者条件创建**：按 `identifier.system/value` 命中则复用，否则新建；
   包内标识重复 → `400`；并发同标识由数据库唯一约束 + 行锁保证只建一份。
3. **乱序前向引用**：`urn:uuid` 在解析阶段统一绑定，与条目顺序无关；
   无法解析、引用不存在的外部资源 → `422`，整包拒绝。
4. **患者一致性**：每份报告与其全部观测必须同属一个患者；把观测改挂其他
   患者时，若已有引用报告未在**同一 Bundle** 内一并更正 → `422`。
5. **原子性**：任何校验/版本/引用失败都会回滚整个事务，不留任何资源或
   修改；其他患者已提交的数据不受影响，后续合法 Bundle 仍可正常提交。
6. **幂等**：
   - 同 `Idempotency-Key` + 同内容（规范化 JSON 哈希，与键序/空白无关）→
     直接返回首次回执，不重复落库；
   - 同 key + 异内容 → `409`；
   - 失败的提交会释放幂等槽，修正后可用同一单号重试；
   - 幂等状态持久化在 PostgreSQL，进程重启/响应丢失后重试仍然安全。
7. 所有错误均返回 FHIR `OperationOutcome`（`application/fhir+json`）。

## 5. 本地开发与自测（无需 Docker）

```bash
pip install -r requirements.txt
export DATABASE_URL="postgresql+psycopg2://research:research@localhost:5432/researchdb"
python -m app.db_init
uvicorn app.main:app --reload
pytest -q
```

测试默认连接 `postgresql+psycopg2://research@localhost:5433/researchdb_test?host=/tmp`，
可用 `DATABASE_URL` 覆盖；并发测试（同标识建患、If-Match 竞争）仅在 PostgreSQL 下执行。

## 6. 目录结构

```
app/
  main.py            FastAPI 入口、异常处理
  config.py          环境配置
  database.py        引擎/会话/建表
  models.py          ORM（患者/标识/观测/报告/关联/版本/幂等账本，PG JSONB）
  errors.py          OperationOutcome 错误体系
  fhir_utils.py      ETag/版本工具
  routers/           /fhir 提交、转挂与资源读取路由
  services/
    bundle.py        Bundle 校验、幂等、事务处理、患者一致性规则
    reassign.py      报告转挂（移动/复制、版本校验、并发序列化）
    queries.py       读取与历史查询
scripts/entrypoint.sh  容器启动（等库+建表）
scripts/demo_reassign.sh  转挂端到端演示脚本
tests/               pytest 用例（含并发、重启、原子性、转挂）
docker-compose.yml   db + api 两服务，含健康检查
```
