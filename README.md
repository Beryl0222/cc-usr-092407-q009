# 文化企业融资进度穿透

本项目用于整理文化企业融资进度穿透领域中的事件名称、交换字段与脱敏样例，并在其上实现一条**可穿透的拨付链**：从任意一笔付款都能沿只追加（append-only）的事件流回溯预算来源、证据指纹、跨申请重叠的判定依据、历次余额调整与仍可使用的额度。资料只包含领域约定与纯内存领域服务，不包含真实个人信息、生产连接或外部账号。

## 审计发现与领域机制的对应

| 审计抽查发现 | 拨付链机制 |
| --- | --- |
| 一张设备发票同时出现在扶持资金与银行贷款材料中 | 合同/发票/交付物按规范业务字段生成**稳定指纹**；同一指纹跨申请出现只产生 `OVERLAP_FLAGGED` 提示 |
| 是否重复列支现有资料无法回答 | 提示不自动定性；由专属授权角色作出 `OVERLAP_DECIDED`（允许/拒绝）并**必须留书面依据**，裁决只追加、不可改判 |
| 已验收里程碑因企业配套款未到账不应放款 | 里程碑固定前置条件（如 `MATCHING_FUNDS`）；`release_blockers` 未清空时无法进入拨付 |
| 额度究竟被谁占用 | 付款先 `QUOTA_RESERVED` 形成占用，`occupation_board` 逐申请列出预算/预留/拨付/追回/可用余额 |
| 审批、验收、付款可能混岗 | 里程碑固定验收人；审批人、验收人、付款人（另有重叠裁决人）为不同角色账号，放款前强制校验三权分立 |
| 部分验收、企业重组、配套款迟到、追回 | 全部是**追加事实**：余额由事件流重算，原验收/拨付结论永不被覆盖或删除 |
| 银行回执重放导致重复拨付 | 回执号全局唯一；同一回执重放幂等返回，不产生新事件、不再次拨付 |
| 批量材料中的冲突连带影响 | `run_batch` 逐条隔离，单条失败只标记该条目/项目，其余正常落账 |
| 执行中断后无法接续 | 付款按检查点 `RESERVED → RELEASED → SETTLED` 推进；`resume_payment` 从所在检查点幂等续作，条件未满足时返回阻碍而非抛错 |
| 审计要从付款向前穿透 | `trace_payment(payment_id)` 一次返回预算来源、证据指纹与共享方、重叠判定、里程碑条款与三权链路、历次调整、当前可用余额 |

## 目录

- `src/culture_finance_progress.py`：事件种类（14 种）与最小字段校验。
- `src/disbursement_chain.py`：拨付链领域服务（无外部依赖，状态可由事件流整体重放）。
- `data/sample.json`：用于核对资料格式的虚构事件。
- `tests/`：契约一致性与拨付链规则测试（44 个用例，逐条对应审计叙述）。

## 拨付链模型要点

### 只追加事件与余额重算

任何余额变化都是新事件：部分验收（`MILESTONE_ACCEPTED`，带 `accepted_delta_ratio` / `accepted_cumulative_ratio`）、企业重组（`ENTITY_RESTRUCTURED`，带预算调整额）、配套款到账（`MATCHING_FUNDS_RECEIVED`）、追回（`RECOVERY_RECONCILED`）。可用余额始终由全量事件折叠得出：

```
可用余额 = 有效预算（含重组调整） − 活跃预留 − 已拨付 + 已追回
```

`DisbursementChain.replay(events)` 可从事件流重建完全一致的状态（角色目录来自外部 IAM，重放后需重新登记账号）。

### 证据指纹与重叠裁决

`evidence_fingerprint(kind, canonical_fields)` 对规范化（去空白、键排序）后的业务字段取 SHA-256，与文件版式无关；同一证据重复提交指纹不变。系统发现跨申请同指纹只**提示**；是否重复由 `ROLE_OVERLAP_OFFICER` 裁决：

- 未裁决的重叠同时拦住双方付款，强制先定性；
- 裁决**拒绝**只阻断复用方（后登记申请），原始登记方不被连坐；
- 裁决一经作出不可覆盖，需要纠错只能再追加新事实。

### 固定条款里程碑与三权分立

里程碑定义时即固定适用协议、验收人、释放比例、用途与前置条件，重复定义被拒绝。资金动作链路上：审批（`ROLE_APPROVER`）→ 验收（`ROLE_ACCEPTOR`，须为固定验收人）→ 预留/拨付（`ROLE_PAYER`），三者账号必须两两不同。部分验收按 `预算 × 释放比例 × 累计验收比例 − 已释放` 计算里程碑可释放容量。

### 检查点续作

付款先预留额度（占用），条件齐备才拨付，拨付后凭银行回执结清。任一步中断（如释放时配套款尚未到账、银行通道抖动未收回执）都可用同一付款号调用 `resume_payment`，从当前检查点继续；预留请求用幂等键去重，回执用回执号去重。

## 最小用法

```python
from src.disbursement_chain import (
    DisbursementChain, ROLE_APPROVER, ROLE_ACCEPTOR, ROLE_PAYER,
    ROLE_OVERLAP_OFFICER, EVIDENCE_INVOICE, CONDITION_MATCHING_FUNDS, OVERLAP_ALLOWED,
)

chain = DisbursementChain()
chain.register_actor("apv", ROLE_APPROVER)
chain.register_actor("acc", ROLE_ACCEPTOR)
chain.register_actor("pay", ROLE_PAYER)
chain.register_actor("off", ROLE_OVERLAP_OFFICER)

chain.submit_application("app-1", "ent-1", "1000000", "GRANT",
                         use_restrictions=["EQUIPMENT"],
                         related_parties=["bank-alpha"],
                         other_commitments=[{"source": "BANK_LOAN",
                                             "amount": "500000", "status": "PROMISED"}],
                         matching_required="200000")
chain.register_evidence("app-1", "inv-1", EVIDENCE_INVOICE,
                        {"invoice_code": "044", "invoice_no": "12345678",
                         "seller": "设备公司", "buyer": "ent-1",
                         "issued_at": "2026-09-01", "amount": "300000.00"})
# 若另一申请登记了同指纹发票，chain.flags 中出现 OVERLAP_FLAGGED 提示
# chain.decide_overlap(flag_id, OVERLAP_ALLOWED, "off", "书面裁决依据……")

chain.define_milestone("app-1", "m1", "proto-A", "acc", "0.5", "EQUIPMENT",
                       preconditions=[CONDITION_MATCHING_FUNDS], evidence_ids=["inv-1"])
chain.approve_milestone("app-1", "m1", "apv")
chain.accept_milestone("app-1", "m1", "1", "acc")          # 或分多次部分验收
chain.reserve_quota("app-1", "m1", "500000", "pay", "idem-key-1")
chain.release_blockers("pay:app-1:m1:idem-key-1")          # 阻碍清单，空即可拨付
chain.record_matching_funds("app-1", "200000")             # 配套款（迟到也只是追加事实）
chain.resume_payment("pay:app-1:m1:idem-key-1", "RCPT-1")  # 从检查点一路结清
chain.trace_payment("pay:app-1:m1:idem-key-1")             # 审计穿透视图
```

领域规则冲突统一抛出 `ChainError`（带 `code`，批量处理时附在失败条目上）；金额一律用 `Decimal` 规整为两位小数。

## 本地核对

```bash
python3 -m compileall -q src tests
python3 -m unittest discover -s tests
```
