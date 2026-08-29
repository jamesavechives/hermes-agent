# bi-gate 部署要点

面向「一人格一 profile、部署到云端」的场景。以下每条都是实测确认过的，不是推测。

## 一、门禁必须在每个 profile 的 config 里显式启用

Hermes 的插件是 **opt-in** 的：`hermes_cli/plugins.py` 的 `_get_enabled_plugins()` 注释写明「Plugins are opt-in by default — only plugins whose name appears in this set are loaded」。

所以每个要门禁的 profile，`$HERMES_HOME/config.yaml` 里都得有：

```yaml
plugins:
  enabled: [bi-gate]
  disabled: []
```

**漏这一行的后果是门禁完全不存在，而且没有任何报错。** 实测：同一个非法调用（未注册指标）在启用的 profile 里被拦、工具体执行 0 次；在没启用的 profile 里直接穿过去、工具体正常执行。两者唯一差别就是 config 里这一行。

`disabled` 的优先级高于 `enabled` —— 一个名字同时出现在两处时不会加载。排查"明明启用了却不生效"时先看这里。

## 二、profile 之间不能共享 HERMES_HOME

上游文档的原话：

> Never point two agent processes at the same profile. Both write memory automatically, and each loads the other's writes into its system prompt at session start — so two writers on one home compound each other's state **until it stops being anything you configured**.

一个 profile = 一个独立 home，内含自己的 `config.yaml`、`.env`、`SOUL.md`、记忆、会话、技能、cron、状态库。需要跨人格共享记忆的场景要用 external memory provider，**不能靠共享目录**。

对容器化的直接含义：**一人格一容器，各挂各的卷**，不要一个容器跑多 profile 共用一个 home。

## 三、存活探针要每个 profile 各跑一份

探针只能看到自己所在 profile 的门禁。`bi` 的门禁挂了，`cs` 的探针发现不了。

```bash
HERMES_HOME=/data/profiles/bi  PYTHONPATH=/opt/hermes  python /opt/hermes/plugins/bi-gate/probe.py
HERMES_HOME=/data/profiles/cs  PYTHONPATH=/opt/hermes  python /opt/hermes/plugins/bi-gate/probe.py
```

**注意别写成 `python -m plugins.bi_gate.probe`** —— 插件目录名是 `bi-gate`（带连字符），
不是合法的 Python 包名，`-m` 那种写法会直接 `ModuleNotFoundError`。这条是本地实跑
（2026-08-24）才发现的：单测全绿、容器里的验证脚本也全绿，因为它们都是用
`importlib` 按文件路径加载的，唯独没人真的按文档写的命令敲一次。

探针进程不需要预先加载插件：`hermes_cli.plugins.invoke_hook` 会在首次调用时自行完成
插件发现与加载，所以这个独立进程走的正是真实加载路径 —— config.yaml 里没显式声明
`plugins.enabled` 照样会被它抓出来（实测：启用的 profile 报 `alive` exit=0，
没启用的报 `gate_down` exit=1）。

这条尤其重要，因为第一节那个「没显式启用就没有门禁」的失效**只有探针能发现**——代码是对的、日志是干净的、测试是绿的，只有真的发一个必然被拦的调用才知道门禁在不在。

## 四、上线时先跑一遍部署自检

探针是给 cron 反复跑的，只回答"门禁此刻还在吗"。**新 profile 上线或排障时**，
先跑一次 `verify.py`：它把整条链路拆成四段逐段报告，断在哪一段一眼能看出来。

```bash
HERMES_HOME=/data/profiles/bi \
BI_GATE_REGISTRY=/data/profiles/bi/bi_registry.json \
BI_GATE_ACTION_POLICY=/data/profiles/bi/action_policy.json \
BI_GATE_ACTION_MAX=L1 \
PYTHONPATH=/opt/hermes \
python /opt/hermes/plugins/bi-gate/verify.py
```

五段是：① `plugins.enabled` 有没有被 Hermes 读到 ② 插件文件在不在
③ 真实派发路径上非法调用有没有让工具体跑起来 ④ 动作分级判得对不对、留痕有没有记上
⑤ 探针能不能出结果。退出码 0 = 全通，1 = 有环节没通，2 = 脚本自身跑不起来。

第 ③ 段是它比探针强的地方：**它会先注册一个会计数的假 `query_metric` 再打**。
探针独立跑时这个工具本身并不存在，于是"门禁不在"和"工具不在"都表现为拦不住；
`verify.py` 里工具确实存在，所以"调用被挡在工具体之前"这个结论是干净的。
判据是工具体的执行计数，不是返回值长什么样。

实测对照（本地，2026-08-24），两个 profile 唯一差别是 config.yaml 里那一行：

| profile | ① 白名单 | ③ 四个非法调用 | ④ 探针 | 退出码 |
|---|---|---|---|---|
| 有 `enabled: [bi-gate]` | `{'bi-gate'}` | 全部拦下，工具体执行 0 次 | `alive` | 0 |
| 没有那一行 | `None` | **全部放行，工具体各执行 1 次** | `gate_down` | 1 |

## 五、人格与授权是两个独立的东西

| 关注点 | 落在哪 |
|---|---|
| 人格（表达） | `$HERMES_HOME/SOUL.md` —— system prompt 的 slot #1，替换默认身份 |
| 授权（工具与动作上限） | `$HERMES_HOME/config.yaml`：toolsets + `plugins.enabled` |

改 `SOUL.md` 不会动到工具权限，反之亦然。这跟《系统 B 技术方案》§8.1「人格和授权必须是两个独立字段」是一致的，不需要我们自己再拆一层。

## 六、上云前要改的四处

仓库自带的 `docker-compose.yml` 是给单机自用设计的，直接搬到云上有问题：

| # | 现状 | 上云要做的 |
|---|---|---|
| 1 | `network_mode: host` | 换成正常网络 + 显式端口映射 |
| 2 | 只挂一个 `~/.hermes:/opt/data` | 每个人格一个 service，各挂各的卷 |
| 3 | API server 默认关；开放需 `API_SERVER_KEY` | 保持强制鉴权，别为图方便去掉 |
| 4 | dashboard 只绑 `127.0.0.1`，文档建议 `ssh -L` 隧道 | 不要直接暴露到公网 |

第 3、4 条是上游的安全默认值，是对的，**不要为了部署方便改掉**。

## 六又二分之一、启动命令（装配期检查的强制点）

**不要直接 `hermes chat`。** 启动一律走包装器：

```bash
/opt/hermes/plugins/bi-gate/preflight.sh /data/profiles/bi -- hermes chat
```

检查不过就 `exec` 不到后面那半截，退出码沿用 `assemble_check.py`
（1 = 有检查未通过，2 = 检查器自身出错）。两种都不启动 ——「检查器坏了」和
「声明不合法」在后果上是一回事：都不知道这份声明合不合法，而查不了的东西不算安全。

为什么要一个包装器，而不是「记得先跑一下 `assemble_check.py`」：这套门禁一路在
纠正同一件事 —— 写在文档里、清单里、配置里给人看的规矩不是强制，强制只能做在
**必经的路径**上。装配期检查也一样。

新建 profile 照着样例来：

```bash
# 从五个声明文件生成（推荐）
/opt/hermes/plugins/bi-gate/build_profile.py \
    /opt/hermes/plugins/bi-gate/profile.source.example /data/profiles/<名字>

# 模型凭据往 .env **分界线以下**追加。分界线长这样：
#   # ==== 以下由部署方维护（模型凭据等），不参与 hash ====
# 以上由工具生成、受 hash 保护；以下随便加，重新生成也不会被抹掉。
grep -E '^DASHSCOPE' /data/profiles/<别的profile>/.env >> /data/profiles/<名字>/.env
```

> **分界线不是后门。** `.env` 是后出现的键覆盖先出现的，所以在下面写一行
> `BI_GATE_TOOLS=...` 本来能绕过整套审批而 hash 还是对的。装配期检查会验
> 分界线以下没有 `BI_GATE_*` / `BI_AUDIT_*` / `HERMES_HOME`，有就判不允许部署。
>
> 这一节原先写的是「用 `make_example_profile.sh`，然后自己补凭据」——
> 那条路会让「生成物没被手改」当场判失败：**DEPLOY.md 让你做的事，
> 和另一条检查禁止的事，是同一件。** 2026-08-28 真去部署一个新 profile 时撞到的。

CI（`.github/workflows/bi-gate.yaml`）跑的是同一个检查器，但输入是样例 profile：
它挡的是「有人把检查器改坏了」，挡不了「这台机器上这份真声明不合法」。
真声明不在仓库里（也不该在，里面有凭据）。两个都要，谁也替不了谁。

## 七、跟着人格走的三份配置

三份都建议放在 profile 自己的 home 里，跟着人格走、随卷挂载：

```bash
# ⚠ 必须写绝对路径。不要写 $HERMES_HOME/... —— 那样是不展开的，见下。
BI_GATE_REGISTRY=/data/profiles/bi/bi_registry.json        # ② facts：这个人格能查哪些指标
BI_GATE_ACTION_POLICY=/data/profiles/bi/action_policy.json # ③ 的一半：L0–L3 怎么分档
BI_GATE_ACTION_MAX=L1                                      # ③ 的另一半：这个人格的动作上限
```

> **这一段原先写的是 `$HERMES_HOME/bi_registry.json`，是错的。**（2026-08-27 实测更正）
>
> `.env` 里的变量**没有任何一层会展开**：Hermes 自己的 `hermes_cli.config.load_env()`
> 原样返回 `$HERMES_HOME/bi_registry.json` 这个字符串，我们的 `_parse_env`
> 也刻意不展开（需要执行才能得出的配置本身就是审批不了的）。
>
> 照原文档建 profile 的后果是注册表读不到 → 空表 → **这个人格什么都查不了**。
> 方向是安全的（fail-closed），但排查起来会绕远路：门禁看着「在工作」，
> 业务方看到的是「它什么都答不上来」。
>
> 现在 `assemble_check.py` 会在部署前直接点出来（`注册表可读 ✗ 文件不存在：$HERMES_HOME/...`）。

对应《人格门禁设计方案》里 Profile 的字段 ② 和 ③。**审批人不同**：注册表由事实层责任人批，
`action_policy` 和 `action_max` 是技术负责人 + 合规双签，等同权限变更。所以它们是三个独立文件，
不合成一份——合在一起就没法分开审批了。

各自坏掉时的方向：

| 配置 | 没设 | 载入失败 |
|---|---|---|
| `BI_GATE_REGISTRY` | 空表，**所有查询被拒** | 同左（fail-closed） |
| `BI_GATE_ACTION_POLICY` | 分级不启用，其余规则照常 | **所有查询被拒**（fail-closed） |
| `BI_GATE_ACTION_MAX` | 按 **L0** 处理（最严），不是"不限制" | 写错级别名同左 |

注册表和策略载入失败都表现为"所有查询都被拒"，而不是"门禁消失"——这个方向是安全的。
所以容器启动挂载卷失败时，现象是业务停摆，不是悄悄放行。

**`BI_GATE_ACTION_POLICY` 没设和设了但坏掉，含义完全不同**：前者是"分档标准还没定"（正常状态，
业务方与合规还在对），后者是"声明了要管却管不了"（故障）。别把两者写成同一种处理。

### 探针看不见过度拦截

存活探针只回答"门禁还拦得住吗"，它发不了"门禁拦过头了"。策略配坏时探针照样报 `alive`
（canary 本来就该被拦），但此刻所有正常查询也在被拒。**过度拦截靠业务侧发现，不靠探针**——
方向上这是可接受的（业务停摆看得见，静默放行看不见），但值班要知道这个盲区。
部署自检 `verify.py` 会抓到，因为它里面有一条本该放行的调用。

---

## 十、接真实数据（2026-08-28）

### 10.1 切换开关

```
BI_QUERY_BACKEND=starrocks      # 默认 stub；切真库必须是显式动作
BI_SR_HOST=<StarRocks FE 主机>
BI_SR_PORT=9030
BI_SR_USER=<只读账号>
BI_SR_PASSWORD=<由部署方填，不进仓库>
```

**任何情况下都不会从真实后端静默退回桩数据。** 连接信息不全、连不上、查询失败，
一律报错。让人以为在看真实数据、其实是假数，比查不到严重得多 —— 而且假数不会
有人来投诉，它会被直接拿去用。

### 10.2 用哪个账号：现状是全库读，应该建专用账号

生产上现有的只读账号（`developer`、`metabase`、`bifufx_ro`）**权限都是
`SELECT ON ALL TABLES IN ALL DATABASES`**，其中 `developer` 还是空密码。
拿任何一个来跑，爆炸半径都是全库 —— 而助手实际只需要 `ads`。

应该建一个专用账号。这条 DDL 需要在生产执行，**不是研发能自己动的**：

```sql
CREATE ROLE bi_agent_ro;
GRANT SELECT ON ALL TABLES IN DATABASE ads TO ROLE bi_agent_ro;
CREATE USER 'bi_agent_ro'@'10.%.%.%' IDENTIFIED BY '<由运维设置>';
GRANT bi_agent_ro TO USER 'bi_agent_ro'@'10.%.%.%';
```

在它建好之前用现有账号跑是可以的，但**必须知道爆炸半径是全库**，
不能靠「反正我们只查 ads」—— 那是约定，不是强制。

### 10.3 网络：dev-frontend-Hermes 目前连不到 StarRocks

实测（2026-08-28）：

| 检查 | 结果 |
|---|---|
| hermes 机器 IP | `10.10.2.57`（匹配账号的 `10.%.%.%`，网段是对的） |
| StarRocks FE 地址 | `starrocks-fe-search.starrocks.svc.cluster.local:9030`（**K8s 集群内**） |
| 机器上有 mysql 客户端 | **没有** |
| 机器上有 tsh | 有（`/usr/local/bin/tsh`），但**未登录** |

所以真实查询目前只能从有 Teleport 代理的机器上跑。要让 agent 在服务器上查真数据，
需要运维给出下面之一：

1. 集群内可达的 FE 地址（NodePort / 内网 LB），或
2. 服务器上的 Teleport 机器身份（Machine ID / bot），让它能常驻一个
   `tsh proxy app new-live-starrocks`

另外服务器上要装 mysql 客户端（当前后端用它连库）。

### 10.4 聚合方式是个已知的坑

现在**一律 SUM**。对「去重人数」（dau/uv 类）和价格类指标，跨天求和是错的。

注册表还给不出正确的聚合方式 —— 数仓的列注释里没写。所以结果里强制带一句
`aggregation_caveat` 说明这个局限。**这不是解决，是把问题摆到台面上**：
正解是让 `ads` 层的列注释（或另一份口径表）声明每个指标该怎么聚合。

---

## 十一、两个定时检查，分工不同（2026-08-29）

| | 存活探针 `probe.py` | 健康检查 `healthcheck.py` |
|---|---|---|
| 发什么 | 一个**必然被拦**的调用 | 一个**应该成功**的查询 |
| 证明什么 | 门禁还在拦 | 整条链路还查得到数 |
| 碰数据库吗 | **不碰** | 碰 |
| 它红了说明 | 门禁失效（安全问题） | 查不到数（可用性问题） |
| 频率 | 5 分钟 | 10 分钟 |
| 单元 | `bi-gate-probe.timer` | `bi-gate-health.timer` |

**为什么只有探针不够。** 2026-08-29 之前探针一直是绿的，但那段时间机器根本连不到
StarRocks —— canary 指标在门禁那层就被拒了，压根走不到后端。
**「门禁在工作」和「助手能用」是两回事。**

（又是那条反复出现的形状：一个检查只覆盖它实际经过的路径。加一层前置门槛，
后面几层就不再被这个检查覆盖，而它照样绿。）

### 健康检查判什么

1. 注册表载得进来且有指标；
2. 挑一个**没有维度**的指标，用**它自己声明的数据范围**里的一小段查一次；
3. 门禁放行、后端返回了行。

时间窗取自注册表声明的范围，**不是「最近 N 天」** —— 后者会让数仓一停更健康检查
就报红，而链路其实是好的。数据新鲜度归 `rejected_no_data_in_range` 管，别混进来。

**返回 0 行也算不健康**：那段本来就该有数，0 行说明声明和实际对不上。

退出码：`0` 健康 / `1` 不健康 / `2` 检查器自身出错。第三个要和第二个分开 ——
「链路坏了」和「检查坏了」在监控上混成一个的话，后者意味着前面那些绿都不算数。

实测五种坏法都抓得住并说清坏在哪一步：端口错 / 密码错 / 注册表丢失 / 名单为空 /
返回 0 行。

### 结果去哪

写 `/data/audit/health.jsonl`，一行一条 JSON，字段 `status` 是
`healthy` / `unhealthy` / `check_error`。
