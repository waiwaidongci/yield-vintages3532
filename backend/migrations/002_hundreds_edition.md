# 002 百件版

新增两张表（`Base.metadata.create_all` 会自动创建，已有库升级时按此结构补建）：

`hundred_editions`（百件版工单与整版）

| 字段 | 类型 | 说明 |
|---|---|---|
| id | INT PK | 工单号，页面与接口都用它查询 |
| request_key | VARCHAR(120) UNIQUE | 请求标识；同一标识重试返回同一张单，不重复出版本 |
| run_id | INT | 源报告（必须是已复核、已完成的报告） |
| run_checksum | VARCHAR(64) | 受理时冻结的报告 SHA256 |
| period | VARCHAR(120) | 报告冻结的期间；与 policy_version 一起构成发布槽位 |
| policy_version | VARCHAR(120) | 报告冻结的口径版本 |
| base_edition_id / base_version_no | INT | 生成开始时的“上版”；公布前会再核对一次 |
| status | VARCHAR(20) | queued / running / published / rejected / failed |
| version_no | INT | 公布的版本号；rejected/failed 时为空 |
| reason | TEXT | 拒绝或失败原因（页面可查） |
| result | JSON | 整版：cells、subtotals、total、changes、deviation、proved_optimal |
| checksum | VARCHAR(64) | 整版结果 SHA256 |
| changes | INT | 相对上版改动的格子数（目标值一） |
| deviation | INT | 与四舍五入不一致的格值个数（决胜目标） |
| created / finished | FLOAT | 受理与结束时间 |

唯一约束 `(period, policy_version, version_no)`，同一期间同口径的版本号不重复。

`hundred_slots`（发布槽位，按期间×口径唯一）

| 字段 | 说明 |
|---|---|
| period / policy_version | 联合唯一 |
| latest_edition_id | 当前公布的版本 |
| next_version | 下一个版本号；在 `SELECT ... FOR UPDATE` 行锁内递增 |

升级步骤：备份 → 停 worker → 建表 → 起服务。历史口径与历史报告不受影响；删除百件版工单不会删除源报告。
