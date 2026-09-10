# 记忆与学习设计：神经科学 × 唯识论 × Agent 运行时

> 本文档回答一个问题：agent 的"记忆"应该长什么样、"学习"应该如何发生。
> 方法：先把神经科学关于记忆的成熟结论与唯识论的种子/熏习框架逐条对齐，
> 再把对齐结果落到 `seed-society` 的具体机制上。
> 配套总纲见 [`yogacara-architecture.md`](yogacara-architecture.md)。

## 一、先澄清三个含糊点（用户思考不清晰的根源）

1. **"记忆"不是一个东西，是多个系统**。人类没有单一的记忆，神经科学公认
   至少有：感觉登记、工作记忆、情景记忆、语义记忆、程序记忆、自传体记忆、
   元记忆。Agent 亦然——把"给 agent 加个记忆"当作单一功能，是混乱之源。
2. **"学习"发生在两处，节奏不同**。海马体快速绑定当次经验（秒-分），新皮层
   用数个睡眠周期慢慢抽象出规律（天-周）。Agent 的学习同样分两层：当次
   attempt 的缺陷反馈（快），以及目标结束后的离线蒸馏与晋升（慢）。
3. **遗忘不是缺陷，是计算特性**。遗忘曲线淘汰低价值痕迹、腾出检索容量、
   防止过拟合于噪音。Agent 的记忆系统必须内置衰减与再激活，而不是无限堆积。

## 二、神经科学基础（只取与本项目相关的结论）

| 结论 | 出处/概念 | 含义 |
| --- | --- | --- |
| 多储存模型 | Atkinson-Shiffrin | 感觉→短时→长时，容量与保持期逐级放大 |
| 工作记忆 | Baddeley | 中央执行器 + 语音环 + 视空板 + 情景缓冲，容量 4±1 组块 |
| 互补学习系统 | McClelland 等 (CLS) | 海马体快速绑定个别经验；新皮层慢速提取统计规律 |
| 系统巩固 | 睡眠回放研究 | 海马在睡眠中"重放"白天的序列，把可迁移规律写入新皮层 |
| 再巩固 | reconsolidation | 记忆被提取时重新变得可塑，可被修正后再固化 |
| 突触可塑性 | LTP/LTD、STDP | 共激活增强（火在一起连在一起）、失败消退 |
| 奖赏预测误差 | 多巴胺 RPE | 学习强度正比于"意外程度"：结果与预期差越大，编码越深 |
| 遗忘曲线 | Ebbinghaus、间隔效应 | 指数衰减；再激活重固、间隔重复增强保持 |
| 模式分离/完成 | 齿状回/CA3 | 相似经验分开存（分离），部分线索补全整体（完成） |
| 显著性标记 | 杏仁核/去甲肾上腺素 | 情绪与危险标记使记忆优先巩固 |

## 三、唯识论对应（种子与熏习的运行学）

| 唯识概念 | 含义 | 神经科学对应 |
| --- | --- | --- |
| 阿赖耶识 | 含藏一切种子的仓库 | 新皮层：慢速、统计性、长期保持 |
| 种子 (bīja) | 潜在势力，遇缘现行 | 突触权重 / 记忆痕迹（本有=先验，新熏=经验） |
| 熏习 (vāsanā) | 现行反复在赖耶中留下气味 | LTP：活动强化痕迹；重复=巩固 |
| 现行 | 种子遇缘生起 | 检索：痕迹被线索激活 |
| 种子生现行、现行熏种子 | 双向同时 | 检索即再巩固（提取重写） |
| 异熟 (vipāka) | 业种异时而熟 | 系统巩固：经验隔一段时间才变成稳定的抽象知识 |
| 受心所 (vedanā) | 苦乐舍三受 | 效价标记：正/负/中性结果标签 |
| 作意心所 | 引心趣境 | 注意力/显著性门控 |
| 别境心所（欲胜解念定慧） | 五别境 | 动机（goal 选择）、理解、忆念（检索）、专注（预算）、慧（元认知） |
| 转识成智 | 经验转智慧 | 情景→语义的晋升（去情景化、泛化） |

**核心对位**：海马体=意识（第六识）的快速绑定；新皮层=阿赖耶识的慢巩固；
睡眠回放=离线熏习；RPE=现行与种子预期的差异；再巩固=修复循环改写种子。

## 四、七层记忆架构（落点：现有组件 + 本次新增）

| 层 | 神经/唯识 | 内容 | 现行实现 | 本次新增 |
| --- | --- | --- | --- | --- |
| 0 感觉登记 | 前五识现量 | 原始工具输入输出、trace span | `tracing.py` | — |
| 1 工作记忆 | 意识现量+情景缓冲 | 依赖产物+失败评审+相关种子（容量受限） | `MemoryManager.build_context` | 检索即再激活 |
| 2 情景记忆 | 海马快绑定/意识种现 | 每次 attempt 的完整证据链 | `attempts/reviews/artifacts/events` | 显著性(RPE)+效价标记 |
| 3 语义记忆 | 新皮层慢抽象/名言种子 | 去情景化的可复用知识 | `knowledge` | 晋升门（多情景佐证才晋升） |
| 4 经验教训 | 睡眠回放产物/熏习种子 | 成功模式+失败模式 | `experience_records` | 势力(strength)+遗忘曲线+再激活 |
| 5 程序记忆 | 业种子 | (agent, task_type) 绩效、champion 部署 | `performance/deployments` | 沿用（已含快慢分离） |
| 6 自传体/元记忆 | 末那我执+慧 | 自模型、信心、样本量 | `genomes`、selection 的 confidence | 自模型更新建议（纯咨询） |

## 五、八个学习机制与实现位置

| # | 机制 | 神经/唯识依据 | 实现 |
| --- | --- | --- | --- |
| L1 | **显著性编码** | RPE + 作意 | attempt 的 salience = 0.4·基础 + 0.4·|score−预期|/100 + 0.2·缺陷占比；写入 experience 记录 |
| L2 | **效价标记** | 受心所 | valence = 判定符号 × (score/100)，PASS 正 FAIL 负 |
| L3 | **离线回放** | 睡眠回放/熏习 | `consolidate GOAL_ID`：按 attempt 顺序重放 → 蒸馏 lessons |
| L4 | **遗忘曲线** | Ebbinghaus | strength 按半衰期指数衰减；`--apply` 才落盘 |
| L5 | **检索再激活** | 再巩固 | 注入 context 的经验被 re-strengthen、activations+1 |
| L6 | **语义晋升门** | 系统巩固/异熟/转识成智 | 同一 (task_type, lesson) 在 ≥2 个 goal 佐证且势力达标 → 建议晋升为 knowledge；`--apply` 才写入，且限量 |
| L7 | **模式分离/完成** | 齿状回/CA3 | 分离=内容寻址 ID（SHA-256）；完成=检索的词元+标签重叠（已存在，予以正名） |
| L8 | **失败修正** | 再巩固 | FAIL→修复→PASS 的缺陷 lesson 与成功 lesson 同链共存（已有，正名） |

## 六、本次落地范围（v1 巩固引擎）

新增 `consolidation.py`（阿赖耶识插件 `seed-consolidation`）：

- `ConsolidationPolicy`：显著性权重、效价规则、半衰期、势力阈值、晋升门槛
  （最小佐证 goal 数、最大晋升数）；
- `ConsolidationEngine.consolidate(goal_id, apply=False)`：
  1. 重放该 goal 的 attempt 序列，逐条计算 salience/valence（预期分取该
     agent 在本 goal 内的滚动均值 → 纯本地 RPE）；
  2. 蒸馏 lessons（复用 `experience.py` 的确定性规则）并带势力初始化
     `strength = 0.5 + 0.5·salience`；
  3. 全库衰减扫描 + 晋升候选收集（≥2 goal 佐证）；
  4. `apply=True` 才执行：势力落盘、晋升写入 knowledge、`memory.consolidated`
     审计事件；`apply=False` 只出报告（dry-run）；
- `ExperienceRecord` 扩展 `valence/salience/strength/activations/
  last_activated_at`（默认值保证旧库兼容；内容寻址 ID 不变）；
- `MemoryManager.build_context` 检索注入时对经验做再激活（best-effort，
  幂等，势力上限 1.0）；
- 新 CLI：`seed-society consolidate GOAL_ID [--apply] [--json]`。

**不做什么（戒律）**：巩固不重写验收标准、不自动改 genome、不激活部署、
不授予权限；晋升的 knowledge 只是种子，仍受检索排序约束。

## 七、后续路线（本版不实现，留作下一步）

1. 间隔重复调度（对低势力种子计划"提醒复习"）；
2. 跨 goal 全局回放（睡眠不止重放一个 goal）；
3. 末那识自模型更新提案 → operator 审批门；
4. 程序记忆的 STDP 变体（同一任务类型内 agent 间对比学习）；
5. 耳识（音频输入）与视空板（图像记忆）的工作记忆扩展。

## 八、dsh-mneme 整合（插件超市基座）

插件超市 23 个记忆插件中，`@modusensus/dsh-mneme` 与本架构最同构：Markdown
镜像（文件即种子/记忆主权）+ autoDream 后台巩固（离线熏习）+ SHA-256 快照
哈希/CAS/receipt 审计（内容寻址戒律）。已安装到 web profile（bundles 已
对账，重启 DSH 生效）。

双向桥 `seed_society/mneme_bridge.py`（零依赖，WAL 并发安全）：

```bash
seed-society mneme sync    --db society.db --mneme-dir ~/.dsh/memory [--include-experience] [--push]
seed-society mneme import  --db society.db --mneme-dir ~/.dsh/memory [--type summary] [--apply]
seed-society consolidate GOAL --db society.db --mneme-dir ~/.dsh/memory --apply   # 单命令联动
```

- 下行：晋升门产出的语义知识（`source:consolidation`）与**势力 ≥0.5**（注入
  门槛等价线：importance=⌈strength×5⌉ ≥3）的 PASS 教训幂等写入 mneme 的
  `memories` 表（内容寻址行 ID、source 溯源）；**不复活** mneme 侧已
  forget/archive 的行；
- **遗忘=停止现行（decay→importance 联动）**：经验种子的势力被巩固引擎衰减后，
  `mneme sync` 无论是否 `--include-experience` 都会把对应行的 importance 降
  下来（⌈strength×5⌉），跌破 mneme 注入阈值 3 后 autoInject 自动停止浮现——
  遗忘在 DSH 侧真实生效；语义知识行（`source:consolidation`）不衰减（神经
  科学上语义记忆稳定、情景记忆才衰减）；
- 上行：非桥接来源的 mneme 条目（dream 总结、人工决策）导入 society 知识
  种子（标签 `mneme/<type>`，词元重叠去重）；
- 戒律：双向均默认干跑，`--push`/`--apply` 才落盘并写 `memory.mneme_*`
  审计事件；mneme 的 LLM 巩固（autoDream）与其 CAS 审计保持原样，我们的
  确定性晋升门与之互补——LLM 做模糊仲裁，确定性规则做晋升门槛。

## 九、种子修订史与回滚（2026-09）

此前种子是**就地覆盖**：`save_knowledge` / `save_experience` 直接覆写 payload，
晋升与衰减都不可撤销。本节补上"可撤销"这一环。

**机制**（`seed_revisions` 表 + `revisions.py`，零依赖纯确定性）：

- **追加式历史**：每次 knowledge/experience 变更写一行 revision，同时携带
  `before` / `after` 两侧 payload 与 `operator` / `reason`。历史行**永不修改或
  删除**，所以撤销本身也可审计、可再撤销；
- **确定性逆编辑**：回滚把 `before` 写回实体表，不询问任何模型去"猜"旧状态
  （该纪律取自经源码审计的 `dsh-continual-evolve`，非照抄其文件快照方案）；
- **回滚可逆**：若实体已等于 `before`（说明该次修订已被撤销过），回滚改为
  取 `after`，即"撤销的撤销"恢复较新状态，而不是重复自己；
- **不记录幻影变更**：非覆盖写命中已存在行时是 no-op，不追加 revision；
- **范围限定**：只管 knowledge 与 experience。goal/task/artifact/review/
  attempt/event 是审计记录，改写它们会破坏"审计线索只增不改"的不变量。

**操作**：

```bash
seed-society revision list [--kind knowledge|experience] [--seed-id ID]
seed-society revision show REVISION_ID
seed-society revision rollback REVISION_ID --by OPERATOR [--reason TEXT]
```

回滚必须给操作员身份，绝不推断。自动路径也留下有意义的 operator：
`promotion`（晋升门产出）、`consolidate`（重放刷新）、`decay`（遗忘衰减）。

**测试含"必须失败"反例**（`tests/test_revisions.py`，16 项）：未知修订、回滚
一个回滚、空操作员、create 带 before、update 缺 before、rollback 缺目标修订
——全部必须被拒绝，而不是静默成功。

## 十、纯谓词筛查（2026-09）

`predicates.py`：确定性、无副作用、可直接单测，供任何写入方复用。

**近重复阻断 / 相似告警**（0.8 / 0.5，Jaccard over token 集）：

- 晋升门的判据从"单一阈值静默跳过"升级为两级：**≥0.8 硬阻断**（近重复不增加
  信息，应当改为 update 既有种子），**≥0.5 放行但标记**（`similar_to` /
  `similarity` 写入候选，让相似性可见而非静默）；
- 分词对 CJK 做**字符级**补充（中文无词间空格，纯 ASCII 词元会让中文种子评分
  恒为 0）。

**凭据筛查不可配置**：8 条正则（OpenAI/GitHub/Slack/AWS/Google/PEM/JWT/
带标签赋值）。刻意不提供配置项——用户正则笔误绝不能关掉凭据筛查。

**双信号陈旧（仅报告，不自动处置）**：`is_stale` = 老 **且** 从未被检索。
年龄本身不是无用证据（持续被注入的种子无论多老都挣得了位置），所以陈旧只进
`ConsolidationReport.stale_seeds` 供人判断，衰减仍按自身 strength 信号运行。

**测试**（`tests/test_predicates.py`，24 项）覆盖：完全/部分/无重叠、CJK 不断
裂、空语料、五类真实凭据样例、普通散文不误报、仅出现"secret"一词不误报、
边界值、布尔不得当计数。

**未实施**：原计划的"原子写 + 读取 normalize"**已放弃**——审计后确认本项目
种子只存 SQLite、**没有任何手工可编辑的种子文件面**，该模式无适用对象；不为
凑齐清单而发明一个文件层。
