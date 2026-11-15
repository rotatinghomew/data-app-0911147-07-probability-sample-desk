# 本地工作台接口

服务默认仅监听回环地址，供本机使用，不是公开多租户系统。所有 JSON 响应使用 UTF-8，出错时有 error 文本；业务输入错误为 400，未知路径为 404。静态资源仅允许根页面和固定资源名。

| 路径 | 方法 | 当前含义 |
| --- | --- | --- |
| /health | GET | 进程存活 |
| /api/study | GET | 当前完整业务资料 |
| /api/summary | GET | 现行描述与资料核对 |
| /api/action | POST | 执行网页表单对应的操作（见下表 operation） |
| /api/notes | GET | 本地工作批注 |
| /api/notes | POST | 保存 text 和 selection，返回新增批注 |

/api/action 的 operation 字段：

| operation | 含义 | 关键输入 | 关键输出 |
| --- | --- | --- | --- |
| draw | 现行简单随机抽签（保留兼容） | size、seed | selected、first_order_probability |
| plan | 在工时与稀少专业覆盖约束下搜索可行设计 | limits（缺省取资料 limits） | feasible、designs（含 spec、最坏工时、保证覆盖、精确设计方差、方差可估计性）；不可行时 conflicts 与 relaxations，不给名单 |
| draw_v2 | 按选定设计抽正式名单与同层备用顺序并留存批次 | design（plan 返回的 spec 或 design_id）、seed、可选 limits | draw_id、main（每人 π 与入样链说明）、reserves |
| receipt | 对批次内学生登记现场状态 | draw_id、student、status ∈ responded/refused/absent/out_of_frame | 当前现场计数；重复登记拒绝 |
| substitute | 对缺席/拒访者启用同班同专业层下一名备用 | draw_id、original | 替补记录与概率说明；框外个体拒绝替补 |
| estimate | 沿原设计给出当前估计依据 | draw_id | 估计总体、design_based 点估计与方差（或 null 及原因）、substitution_view、覆盖核对、事件链 |
| draws | 抽签历史复查 | 无 | 批次摘要列表 |
| draw_detail | 单批次完整记录 | draw_id | 设计、名单、回执、替补全量 |

现行操作的具体字段在 data/default-action.json，可通过网页直接提交。已有接口应保持可用；新的分析接口、结果表示与交互方式可按产品需要设计，不要求沿用现行描述性结果的字段。数值未定义应使用 null 并解释原因，不以 NaN 或 Infinity 冒充 JSON 数值。抽签批次与现场事件持久化在 APP_STATE_DIR（默认 .local）的 draws.json，删除该目录即回到干净状态。
