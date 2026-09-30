# 非遗活动采购透明台账

多校联合采购演出服务的透明台账服务端：围绕**需求 → 询价 → 评审 → 履约 → 结算**建立连续台账，
把分散在报价表、回避声明、验收邮件中的信息收敛为一条可复核、不可变的哈希链式分录链。

## 领域规则如何落地

| 需求中的关切 | 实现方式 |
| --- | --- |
| 关系回避 | `conflict_declarations` 记录供应关系申报；存在**最新状态为未回避**的关联即拦截报价与授标；冻结后声明锁定；有未回避关系的评委不能签署，已回避评委只能签“弃权” |
| 报价冻结 | `freeze` 把当时全部“有效”报价置为“冻结”并锁定评委名单与回避声明；此后新增/替换/撤回报价一律 409。旧报价版本标记“已替换”但**全部留痕**，供审计复核比价 |
| 部分验收 | `acceptances` 支持多次部分验收；累计验收 ≤ 当前合同承诺 |
| 合同变更 | `contract_changes` 记录带原因的正/负差额；变更后承诺 ≤ 预算且不低于已验收额 |
| 退款冲正 | `refunds` 关联原付款，只追加反向复式分录，**永不改写或删除**原付款；同一付款累计退款 ≤ 原额 |
| 同时签署 | 服务级可重入锁 + SQLite `BEGIN IMMEDIATE`；`review_signoffs` 唯一约束兜底，同一评委并发签署恰好一次成功 |
| 金额平衡 | 整数“分”计算 + `Decimal` 转换；承诺≤预算、验收≤承诺、付款净额≤验收、退款≤付款；终验必须验收额=合同额，结算必须净额=验收额 |
| 分级公开 | 公众端点只返回脱敏摘要（无人员姓名、回避细节、未中标报价）；审计角色可见全量连续台账并自动重算哈希链 |
| 不可变分录 | `ledger_entries` 按采购维度串联 SHA-256 哈希链（`GENESIS → …`），每条借贷平衡；`verify_chain` 重放事件金额并与业务表汇总比对，任何篡改立即暴露 |

## 目录

- `domain/contract.json`：领域角色、状态、不变量、科目、事件分录、冻结与分级公开约定。
- `src/domain_contract/`：契约读取与确定性校验（含新增台账章节校验）。
- `src/ledger_service/`：台账服务端。
  - `storage.py`：SQLite 表结构（含分录哈希链、签署唯一锁、金额 CHECK 约束）。
  - `service.py`：全部领域规则与不可变复式台账。
  - `http_api.py`：标准库 JSON HTTP API（多线程安全）。
  - `server.py`：启动入口。
- `tools/check_contract.py`：命令行摘要检查。
- `tests/`：契约回归、领域流程、并发安全、HTTP 鉴权/脱敏/审计测试。

## 验证

```bash
# 测试（21 个用例，含并发与篡改检测）
python3 -m unittest discover -s tests -v

# 编译检查
python3 -m compileall -q src tools tests

# 契约摘要
python3 tools/check_contract.py domain/contract.json

# 启动 HTTP 服务
PYTHONPATH=src python3 -m ledger_service.server --db ledger.sqlite3 --port 8080
```

## API 概览

鉴权：先 `POST /auth/token`（`{"role","person_id","name"}`），后续请求带
`Authorization: Bearer <token>`。角色：学校采购员 / 评审委员 / 供应方 / 审计人员。

| 阶段 | 端点 |
| --- | --- |
| 需求 | `POST /procurements`（自动登记预算 BUDGET 分录）、`POST /procurements/{id}/inquiry` |
| 回避 | `POST /procurements/{id}/conflicts`（冻结前可补报，最新声明有效，历史留痕） |
| 询价 | `POST /suppliers`、`POST /procurements/{id}/quotes`（自动版本号，旧版留痕） |
| 评审 | `POST /procurements/{id}/reviewers`、`POST /procurements/{id}/freeze`、`POST /procurements/{id}/signoffs` |
| 合同 | `POST /procurements/{id}/contract`（合同价必须等于冻结报价）、`POST /procurements/{id}/contract-changes` |
| 履约 | `POST /procurements/{id}/acceptances`（`final:true` 为终验）、`POST /procurements/{id}/payments`、`POST /procurements/{id}/refunds` |
| 结算 | `POST /procurements/{id}/settle`（终验且结清后进入终态） |
| 复核 | `GET /procurements`、`GET /procurements/{id}`、`GET /procurements/{id}/quotes`（全版本）、`GET /procurements/{id}/ledger`（哈希链）、`GET /procurements/{id}/audit`（审计全量+链校验） |
| 公众 | `GET /public/procurements/{id}`（无需令牌，脱敏摘要） |

## 说明

- 金额一律以整数分存储，接口以“元”字符串收发，避免浮点误差。
- `/auth/token` 为演示用简易令牌；生产环境应对接学校统一身份认证。
- 哈希链按“每个采购”独立成链；跨采购的全局锚定（如定期对外发布链尾哈希）可作为后续增强。
