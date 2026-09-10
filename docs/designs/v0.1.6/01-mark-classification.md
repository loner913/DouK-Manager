# 01：mark 归类

## 目标与非目标

目标是让下载器原生日志中的账号 mark 能够安全地回到冻结任务的 A 编号，同时容纳已经观察到的、不会改变业务含义的引擎显示差异。原始业务 mark 是主基准，别名只用于比较。

非目标包括：改写 `settings.json` 或原生日志、把昵称变化当作同一账号、使用相似度或模糊匹配修复未知文本，以及在证据冲突时替用户选择一个账号。

## 用户入口与数据流

1. 下载任务启动前，从任务版 `settings.json` 的 `accounts_urls` 按数组位置读取启用且 URL 有效的账号，校验 mark 后冻结计划账号和 A 编号映射。
2. 原生日志解析使用冻结映射；开始处理账号时先按原始冻结 mark 精确匹配。
3. 只有在原始值不完全相同时，才尝试与冻结 mark 对应的单向清理别名比较。别名只在当前冻结集合中唯一归属一个 A 编号时生效。
4. 解析结果继续进入下载汇总和任务日志。未知或冲突的 logged mark 标为不可靠，不通过归一化“修好”原始日志。

## 核心数据结构与状态

- `PlannedAccount` 保存任务序号、源数组位置和冻结 mark；冻结入口只对必要的首尾空白做验证和处理，后续别名计算不会回写它。
- `_unique_cleaned_mark_aliases` 生成“任务序号到唯一别名”的内部映射，同时把每个原始值和别名的所有拥有者收集起来。
- `_cleaned_mark_alias` 只镜像已观察到的引擎显示变化：移除控制字符和 presentation selector、折叠空白、移除外层 ASCII 句点。
- 账号日志消费路径保留精确原值；不能映射到冻结值或唯一别名时设置不匹配/不可靠状态，由汇总层继续保留证据边界。

## 必须保持的安全不变量

- 冻结 mark 是唯一业务基准；别名是比较用视图，绝不改写冻结 mark、任务配置或原生日志文本。
- 原始 mark 与另一个账号的原始 mark、或与另一个账号的别名发生所有权冲突时，该别名不生成、不信任。
- 只有“清理结果非空、不同于原值、且所有者恰好为当前 A 编号”的别名可以匹配。
- 未知文本、前缀相似文本、昵称变化和碰撞文本全部按不可靠处理；不能通过未获准的额外清理或模糊归一化扩大匹配范围。
- 账号数组位置和冻结任务顺序不因 mark 解析而重排；解析失败不能把结果转移到另一个 A 编号。

## 失败、取消与降级路径

- `accounts_urls` 不是数组、账号项不是对象、启用账号缺少有效 mark，或没有启用且 URL 有效的账号时，冻结阶段拒绝继续。
- 清理别名为空、等于原值、与其他原始/别名冲突，或日志值仍无法精确对应时，不猜测归属；调用方得到不匹配/不可靠证据。
- 发生解析错误时保留已经读取的可靠边界，不通过重写输入日志或生成模糊替代 mark 恢复。
- 该功能没有独立的“强制采用别名”入口；冲突只能通过新的冻结输入和后续可靠日志证据重新建立。

## 兼容性边界

旧日志仍可使用原始 mark 精确匹配；只有具备明确日志语义且经过唯一性验证的显示差异才会被接受。缺少冻结 mark、出现新型未观察变化或多个账号共享清理结果时，结果保持不可靠，而不是降低安全标准换取更高匹配率。

## 对应源码与测试

- 源码：[download_summary.py](../../../src/douk_manager/core/download_summary.py)
- 测试：[test_download_summary_parser.py](../../../tests/test_download_summary_parser.py)
- 相关测试包括 `test_engine_cleaned_marks_keep_the_frozen_account_identity`、`test_mark_alias_never_normalizes_unexpected_logged_text`、`test_colliding_cleaned_marks_are_not_trusted`、`test_alias_collision_with_another_raw_mark_is_not_trusted` 和 `test_logged_mark_that_does_not_match_frozen_mapping_is_unreliable`。

## 已完成的验收证据

阶段 D 的 D-0 至 D-25 验收为 PASS；完整回归保留 `608 total / 606 passed / 2 skipped / 0 failed / 0 errors`。上述测试覆盖冻结顺序、引擎清理后的安全身份保留、未知 mark、前缀相似文本及别名碰撞；没有通过跳过测试或模糊化路径来规避边界。
