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
插件发现与加载，所以这个独立进程走的正是真实加载路径 —— config.yaml 里漏配
`plugins.enabled` 照样会被它抓出来（实测：启用的 profile 报 `alive` exit=0，
没启用的报 `gate_down` exit=1）。

这条尤其重要，因为第一节那个「漏配一行就没有门禁」的失效**只有探针能发现**——代码是对的、日志是干净的、测试是绿的，只有真的发一个必然被拦的调用才知道门禁在不在。

## 四、上线时先跑一遍部署自检

探针是给 cron 反复跑的，只回答"门禁此刻还在吗"。**新 profile 上线或排障时**，
先跑一次 `verify.py`：它把整条链路拆成四段逐段报告，断在哪一段一眼能看出来。

```bash
HERMES_HOME=/data/profiles/bi \
BI_GATE_REGISTRY=/data/profiles/bi/bi_registry.json \
PYTHONPATH=/opt/hermes \
python /opt/hermes/plugins/bi-gate/verify.py
```

四段是：① `plugins.enabled` 有没有被 Hermes 读到 ② 插件文件在不在
③ 真实派发路径上非法调用有没有让工具体跑起来 ④ 探针能不能出结果。
退出码 0 = 全通，1 = 有环节没通，2 = 脚本自身跑不起来。

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

## 七、指标注册表的位置

注册表路径由 `BI_GATE_REGISTRY` 给出，建议放在 profile 自己的 home 里，跟着人格走：

```bash
BI_GATE_REGISTRY=$HERMES_HOME/bi_registry.json
```

载入失败按空表处理，即全部拦截（fail-closed）。所以容器启动时挂载卷失败会表现为"所有查询都被拒"，而不是"门禁消失"——这个方向是安全的。
