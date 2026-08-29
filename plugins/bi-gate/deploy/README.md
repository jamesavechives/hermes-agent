# 体验页的容器化部署

## 为什么要搬进 K8s

现有 98 个 Teleport 应用的 `uri` **无一例外**都是 `*.svc.cluster.local`，
说明 app service 跑在集群里，出不到 `dev-frontend-Hermes`(10.10.2.57) 这台
集群外的 VM —— 这解释了 SRE 说的「从 teleport 探过去不通」。

搬进集群是顺着他们既有模式走，不用为我们单开网络。顺带解决另一件事：
现在服务是 `nohup` 起的裸进程，机器一重启就没了。

## 镜像

```bash
cd /opt/hermes
cp plugins/bi-gate/deploy/.dockerignore .dockerignore
docker build -f plugins/bi-gate/deploy/Dockerfile -t <registry>/bi-agent-demo:<tag> .
rm .dockerignore
```

实测 **428MB**。只装了 4 个第三方包（`cryptography` / `jiter` / `jwt` / `yaml`，
约 20MB），**不装 Hermes 全套依赖**（venv 有 331MB，光 `lark_oapi` 就 100MB）——
那些是 agent 跑模型要的，体验页不跑模型。

这 4 个是量出来的，不是猜的：

```python
import gateway.session_context          # webui 用它读会话身份
load("w_gate",  "plugins/bi-gate")
load("w_query", "plugins/bi-query")
import jwt                              # Teleport 验签
# 然后扫 sys.modules，看哪些 __file__ 落在 site-packages 里
```

（注意 `/opt/hermes` 是软链，`__file__` 里是真实路径 —— 第一次量的时候前缀
匹配错了，结果是「0 个依赖」。）

上游 import 链变了会缺包启动失败，那是对的失败方向：**起不来，而不是悄悄
少了什么**。

## 部署

```bash
kubectl apply -f plugins/bi-gate/deploy/k8s.yaml
```

改这几处：

| 位置 | 改成 |
|---|---|
| `namespace` | 按你们的规范（清单里默认 `crypto`） |
| `image: REGISTRY/bi-agent-demo:TAG` | 实际镜像地址 |
| Secret 里的 `BI_SR_PASSWORD` | dev StarRocks 的 `bi_agent_ro` 密码 |
| ConfigMap 的 `principals.json` | 要能用的人的 Teleport 用户名 |

## 注册表在镜像里，不在 ConfigMap

`/app/registries/` 下有两份：

| 文件 | 内容 |
|---|---|
| `registry.dev.json` | dev 复刻库的口径，**数值是造的**（体验用这个，默认） |
| `registry.ads.json` | 生产 `ads` 层口径，真实数据（需要生产账号才用得上） |

**为什么不放 ConfigMap**：注册表是和代码一起演进的东西（由
`build_registry_from_ads.py` 从数仓元数据生成，口径变了要重新生成），
不是部署方要改的配置。放 ConfigMap 会让「镜像里的代码」和「集群里的口径」
各走各的版本 —— 那正是这套东西一路在防的漂移。60KB，打进去的代价可以忽略。

切换用 `.env` 里的 `BI_GATE_REGISTRY` 指向哪一份，不用重建镜像。

**注册表读不到会拒绝启动并说清原因**（不是抛一段栈）：

```
拒绝启动：注册表 /app/registries/不存在.json 不存在。
容器部署时它在镜像的 /app/registries/ 下，不是 ConfigMap；
裸进程部署时看 profile 的 .env。
```

这条在容器里特别要紧 —— 不然表现就是 Pod 崩溃循环，看的人得先猜是哪种原因。

顺带去掉了 initContainer 里的 `cp /cfg/bi_registry.json ... || true`：
那个 `|| true` 会在文件不存在时静默跳过，于是 initContainer 是绿的、
主容器起来才崩。**「悄悄跳过」正是这套东西一路在防的那种失效。**

## 部署后请运维加一条 Teleport 应用

```yaml
- name: bi-agent-demo
  uri: http://bi-agent-demo.<namespace>.svc.cluster.local:8800
  public_addr: bi-agent.dev.decodebackoffice.com
  labels: {env: dev, owner: james.qian}
```

这样 `uri` 就和其它 98 个一样是集群内地址。

## 已验证

在 dev 机上真构建 + 真跑了一次容器（挂 profile、映射 8801）：

```
容器状态    Up
启动日志    身份模式 teleport（Teleport JWT 验签）／指标 80 个
首页        HTTP 200
catalog     mode=teleport, 80 指标
无 JWT 查询  被拒于「身份」——「这次请求没有带 Teleport-Jwt-Assertion 头」
```

行为和裸进程完全一致。

打进注册表后又验了一轮：

```
镜像里         registry.ads.json 66586 / registry.dev.json 62644
启动           指标 80 个（来自镜像，profile 里没有注册表文件）
catalog        mode=teleport, 80 指标
注册表路径写错  拒绝启动，报错指明「在镜像的 /app/registries/ 下，不是 ConfigMap」
```

## 两个已知待办

1. **审计现在落 `emptyDir`，Pod 重启就没了。** 上线前要换 PVC，或者直接推
   VictoriaLogs（`audit_shipper.py` 已经有了）。清单里留了注释，是为了让这件事
   被看见而不是默默丢。
2. **`readOnlyRootFilesystem: true`** 已开，profile 和 audit 走独立挂载。
   如果将来有别的写盘需求要单独加卷，不要图省事把这个关掉。
