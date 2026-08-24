# bi-gate — 系统 B 的门禁

在 `query_metric` 派发之前做一轮确定性校验，不通过就拦下并落审计。

## 它拦什么

| 拒因 | 判定 |
|---|---|
| `rejected_unknown_metric` | 指标不在受控事实层 |
| `rejected_bad_param` | 维度不是该指标声明过的 |
| `rejected_no_time_window` | 缺时间窗，或时间窗不是绝对区间 |
| `rejected_scan` | EXPLAIN 预估扫描量超过该指标上限 |

时间窗只收绝对区间（`2026-08-01`），不收 `最近七天` 这类相对表述。相对时间必须在调用前解析成具体日期，否则同一个问题在不同时刻问会得到不同的数，评估集就无法回归。

## 它明确不拦什么

**行列级权限（ACL）不在这一层。** 谁能看哪些行、哪些列，必须由数据层的独立库账号与行级权限保证。放在 agent 层的权限本质上是提示词级约束——绕过一个 hook 就没了。本插件在理由里可以提示权限问题，但它不是防线。

`run_sql` 的降级路径也不在本插件范围内，那套围栏另做。

## 为什么是插件

仓库是 `nousresearch/hermes-agent` 的 fork，上游非常活跃。门禁只用 `pre_tool_call` / `post_tool_call` 两个既有扩展点，核心文件一行不动，同步上游时不会冲突。

## 拒绝理由为什么带来源

每条拒绝都以「BI 门禁（bi-gate 插件，在调用发出前拦截）」开头。

实测依据：理由里只写"命中规则"时，模型会自行编造归因——两次实验里它分别把 harness 的拦截说成「本地代理的安全策略」和「远端服务检测到」，最后给用户一个错误的解释。见《评估与 Reward v0.1》§2.4。

## 门禁自身故障时会怎样

判定过程内部出任何未预料的异常，插件都会**转成拦截**（拒因 `rejected_gate_error`），并在理由里说明是门禁故障而非调用有问题。

这条兜底是必需的，因为 Hermes 侧对 hook 异常是 **fail-open**：`model_tools.py` 里对 `_dispatch_pre_tool_call_hooks` 的调用包在 `except Exception` 中，只记一条 debug 日志然后继续执行。异常一旦逃出插件，门禁就静默消失，日志里几乎看不出来。

**但兜底挡不住所有情况。** 如果整个 hook 函数本身坏掉（插件加载失败、签名不匹配、被从配置里摘掉），插件内部的 try/except 根本没机会执行，Hermes 仍然放行。这种失效没有任何异常日志，只能靠探针发现。

两种情形分别有测试钉住，见 `tests/plugins/test_bi_gate_e2e.py::TestFailureModes`。

## 存活探针

定期发一个必然被拦的调用，检查它真的被拦了：

```bash
PYTHONPATH=/path/to/hermes-agent python /path/to/hermes-agent/plugins/bi-gate/probe.py
```

**别写成 `python -m plugins.bi_gate.probe`**：插件目录名是 `bi-gate`（带连字符），
不是合法的 Python 包名，`-m` 会直接 `ModuleNotFoundError`。

退出码：`0` 存活 / `1` **门禁失效，必须告警** / `2` 探针自身出错（环境问题，与门禁无关）。三者分开是为了让监控能区别处理——把环境故障当成安全事件会消耗对告警的信任。

建议挂 cron，失效时才出声：

```cron
*/10 * * * * cd /path/to/hermes-agent && HERMES_HOME=/data/profiles/bi PYTHONPATH=. python plugins/bi-gate/probe.py >/dev/null || <告警命令>
```

探针**刻意不 import 插件的注册逻辑**，只驱动真实派发路径、观察结果。写在插件内部就成了循环论证：插件没加载时探针也不会跑，于是永远报"正常"。

实测验证过两种状态：门禁挂载时 `alive`；把 hook 摘掉后探针返回 `gate_down`，且工具体确实被执行了 1 次——正是它要抓的那种静默失效。

## 动作分级（action_max 的 L0–L3）

其它规则问"这个调用合不合法"，这一层问"**这个人格被授权做到多重的动作**"。
同一个 `query_metric`，查汇总和导明细的风险不是一回事，但工具名是同一个——
只按工具名授权在这里就不够用。

机制是：**输入工具名 + 参数，输出一个级别，再和人格声明的 `action_max` 比。**
分档标准写在配置里，不写在代码里——改分档不该要发版，而且定分档的是业务方和合规，不是技术侧。

```bash
export BI_GATE_ACTION_POLICY=$HERMES_HOME/action_policy.json   # 怎么分档
export BI_GATE_ACTION_MAX=L1                                   # 这个人格的上限
```

格式见 `policy.example.json`。三条要点：

**没配 `BI_GATE_ACTION_POLICY` = 这一层不启用**，其余门禁规则照常。分档标准要业务方与合规定，
没定之前技术侧不该塞一套默认值假装有授权控制。「还没定」和「配坏了」必须能分开。

**配了但载入失败 = 全拒**。声明了要管却管不了，只能停摆。和注册表载入失败是同一个方向。
**坏配置不会"跳过坏规则、其余照用"**——一条规则被静默跳过，就是一档授权被静默放宽，
而这是最难在事后发现的那类问题。

**没声明 `BI_GATE_ACTION_MAX` 按 L0 处理（最严），不是"不限制"。** 漏声明的后果应该是做不了事。

### 条件语言

刻意做得很小。表达力再往上加一点就变成"没有测试的代码"，而这份配置是合规要审的东西，
看不懂等于没审。

| 算子 | 含义 |
|---|---|
| `param_present` | 这些参数出现即匹配 |
| `param_absent` | 这些参数不出现即匹配 |
| `param_equals` | 参数等于给定值 |
| `param_in` | 参数在给定集合里 |
| `param_gte` | 数值参数 >= 给定值 |
| `dimensions_count_gte` | 维度个数 >= N |

同一条规则里的多个条件是 AND；**命中多条规则时取最高级别**（就高不就低），
所以规则顺序不影响结果。用了表外算子 → 整份策略判为不可用（全拒），不是跳过那条。

参数类型与规则对不上（比如规则要求 `limit` 是数字、实际传了字符串）叫**判定不了**，
一律拒绝。把它当成"不匹配"会让级别被悄悄降下来——这是这套机制最容易出错的地方，
单测里专门有一组覆盖。

### 人工审批

`human_review_from` 设成某个级别后，达到该级别的调用一律拒绝并说明需要人审，
**无论 `action_max` 是多少**。对齐系统 B 方案 §7.2「L3（不可逆或涉资金）一律人审」。
BI 场景没有 L3，默认不启用；客服人格应设成 `"L3"`。

本层只负责拦下并说明，**真正转给谁属于 Profile 的 fallback 字段**，不在这里。

### 已知弱点

`default_level` 设成 `L0` 时，如果工具新增了危险参数而没人补规则，新参数组合会被判成 L0 放行。
反过来把 `default_level` 设成 `L3`、用规则显式把安全调用降下来，则任何新参数组合先被拦。
哪种更合适由业务方定——这条写在 `policy.example.json` 的注释里，别让它被无声继承。

## 部署自检

探针是给 cron 反复跑的。**新 profile 上线或排障时**先跑一次 `verify.py`，
它把整条链路拆成五段逐段报告：① `plugins.enabled` 有没有被 Hermes 读到
② 插件文件在不在 ③ 真实派发路径上非法调用有没有让工具体跑起来
④ 动作分级判得对不对、留痕有没有记上 ⑤ 探针能不能出结果。

```bash
HERMES_HOME=/data/profiles/bi \
BI_GATE_REGISTRY=/data/profiles/bi/bi_registry.json \
BI_GATE_ACTION_POLICY=/data/profiles/bi/action_policy.json \
BI_GATE_ACTION_MAX=L1 \
PYTHONPATH=/path/to/hermes-agent \
python /path/to/hermes-agent/plugins/bi-gate/verify.py
```

它比探针强在第 ③ 段：**会先注册一个会计数的假 `query_metric` 再打**。
探针独立跑时这个工具本身并不存在，"门禁不在"和"工具不在"都表现为拦不住；
`verify.py` 里工具确实存在，所以"调用被挡在工具体之前"的结论是干净的。
判据是工具体的执行计数，不是返回值长什么样。

退出码 0 = 五段全通 / 1 = 有环节没通 / 2 = 脚本自身跑不起来。

## 配置

指标注册表路径由环境变量给出：

```bash
export BI_GATE_REGISTRY=/path/to/registry.json
```

格式见 `registry.example.json`。字段只有门禁需要的那几项——口径描述、责任人、新鲜度在指标层维护，不在这里。

**载入失败时按空表处理，即所有 `query_metric` 调用都被拦截。** 这是有意的 fail-closed：门禁配置坏掉时应该停摆，而不是放行。

## 现状与缺口

- 扫描量预检的 `estimated_rows` 目前恒为 `None`（即该条恒放行）。执行层接上 EXPLAIN 后传入即可，`rules.check_scan_budget` 已经写好。
- 动作分级的分档标准还没拍板。`policy.example.json` 里那套（汇总 L0 / 明细 L1 / 导出 L2）是《人格门禁设计方案》第八节第二条的**建议**，落地前必须替换成业务方与合规签过字的版本。
- `policy.example.json` 里 `when` 用的参数名（`granularity` / `export` / `destination` / `limit`）是按当前工具 schema 写的。**工具改参数名时这份策略要一起改**——对不上会让规则永远不命中，表现为授权被静默放宽，不会有任何报错。这一条目前没有自动检查。
- 审计当前只写结构化日志。接 `ai_cs.agent_audit` 时替换 `_audit` 的实现，调用点不用改。
- 注册表从 JSON 文件读。指标层稳定后应改为从指标服务拉取并带版本号，否则无法回答"这次判定用的是哪一版口径"。

## 测试

```bash
pytest tests/plugins/test_bi_gate.py tests/plugins/test_bi_gate_e2e.py \
       tests/plugins/test_bi_gate_probe.py tests/plugins/test_bi_gate_action_level.py
```

四个文件分工：`test_bi_gate.py` 验判定逻辑算得对，`test_bi_gate_e2e.py` 驱动真实
`handle_function_call` 验拦得住（硬证据是工具体的执行计数），`test_bi_gate_probe.py`
验探针本身，`test_bi_gate_action_level.py` 验动作分级（含分级算得对、越权被拦、配置坏掉时全拒）。**但它们都是用 `importlib` 按文件路径加载插件的**，所以证明不了
「按文档写的命令能不能跑起来」—— 那个只能靠 `verify.py` 和 `probe.py` 真的跑一次。
