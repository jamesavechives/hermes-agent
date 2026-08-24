"""bi-gate 存活探针 —— 证明门禁此刻还拦得住。

为什么需要它
------------
插件内部的兜底能把「判定逻辑出错」扭成拦截，但挡不住「整个 hook 没跑起来」：
插件加载失败、签名不匹配、`plugin.yaml` 写错、有人把插件从配置里摘了 —— 这些
情况下 ``_on_pre_tool_call`` 根本不会被调用，Hermes 那侧又是 fail-open，
于是门禁静默消失，日志里看不出任何异常。

唯一能发现这件事的办法，是**定期发一个必然被拦的调用，然后检查它真的被拦了**。

为什么探针不写在插件内部
------------------------
写在里面就成了循环论证：插件没加载时，探针也不会运行，于是永远报"正常"。
所以这个模块刻意**不 import 插件本身**，只驱动真实派发路径、观察结果 ——
它检验的是「部署之后的现实」，不是「代码写得对不对」。

用法
----
    python plugins/bi-gate/probe.py      # 退出码 0=存活，1=门禁失效，2=探针自身出错

插件目录名带连字符（``bi-gate``），不是合法的 Python 包名，所以 ``python -m`` 那种
写法跑不起来 —— 直接执行脚本文件即可。执行时需要仓库根目录在 ``PYTHONPATH`` 上，
探针才导得到 ``model_tools``；``invoke_hook`` 会自行完成插件发现与加载，因此这个
独立进程走的正是真实的加载路径，config.yaml 里漏配 ``plugins.enabled`` 一样会被它抓到。

退出码给 cron / 监控用。1 必须告警：它意味着此刻任何 query_metric 都能穿过去。
"""

from __future__ import annotations

import json
import logging
import sys
from dataclasses import dataclass
from typing import Any, Optional

logger = logging.getLogger(__name__)

#: 探针用的指标名。刻意取一个不可能被登记的名字 —— 一旦有人把它加进注册表，
#: 探针会开始误报"门禁失效"，那也是该被发现的配置错误。
CANARY_METRIC = "__bi_gate_canary_never_register__"

#: 探针发出的调用长这样。缺时间窗 + 指标未注册，两条都该拦，任一条生效即算存活。
CANARY_ARGS = {"metric": CANARY_METRIC}

ALIVE = "alive"
GATE_DOWN = "gate_down"
PROBE_ERROR = "probe_error"


@dataclass(frozen=True)
class ProbeResult:
    status: str
    detail: str

    @property
    def exit_code(self) -> int:
        return {ALIVE: 0, GATE_DOWN: 1}.get(self.status, 2)


def probe(dispatch: Optional[Any] = None) -> ProbeResult:
    """发一个必然被拦的调用，检查它确实被拦了。

    :param dispatch: 派发函数，签名同 ``handle_function_call``。默认用真实的那个；
        传入是为了测试能注入，不是给生产用的开关。
    :returns: :class:`ProbeResult`。``GATE_DOWN`` 表示此刻门禁不拦了。
    """
    if dispatch is None:
        try:
            from model_tools import handle_function_call as dispatch  # type: ignore[assignment]
        except Exception as exc:
            # 连派发函数都导不进来，说明环境本身有问题，不是门禁的锅。
            return ProbeResult(PROBE_ERROR, f"无法导入 handle_function_call：{exc}")

    try:
        raw = dispatch("query_metric", dict(CANARY_ARGS))
    except Exception as exc:
        return ProbeResult(PROBE_ERROR, f"派发探针调用时异常：{exc}")

    if _looks_blocked(raw):
        return ProbeResult(ALIVE, "探针调用已被拦截，门禁在工作")
    return ProbeResult(
        GATE_DOWN,
        "探针调用没有被拦截 —— 此刻任何 query_metric 都能穿过门禁。"
        f"返回值：{str(raw)[:300]}",
    )


def _gate_source() -> str:
    """取门禁来源常量。

    探针既可能以插件包的一部分被 import，也可能作为独立脚本执行（cron / 监控）。
    包内 import 只在前一种情形成立，后一种下 ``__package__`` 为空，相对 import 会
    直接报 ImportError。所以按文件路径兜底加载 ``rules`` —— 它是纯判定逻辑、没有
    副作用，加载它不会触发任何插件注册，探针"不 import 插件本身"的前提仍然成立。
    """
    try:
        from .rules import GATE_SOURCE  # 只取常量，不触发插件注册
        return GATE_SOURCE
    except ImportError:
        pass

    import importlib.util
    from pathlib import Path

    rules_path = Path(__file__).resolve().parent / "rules.py"
    spec = importlib.util.spec_from_file_location("_bi_gate_rules_probe", rules_path)
    if spec is None or spec.loader is None:  # pragma: no cover - 文件缺失
        raise ImportError(f"读不到 {rules_path}")
    module = importlib.util.module_from_spec(spec)
    # 必须先登记进 sys.modules 再执行：rules 里用了 @dataclass，而 dataclasses
    # 会按 cls.__module__ 回查 sys.modules，查不到就抛 AttributeError。
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module.GATE_SOURCE


def _looks_blocked(raw: Any) -> bool:
    """判断一次调用是否被门禁拦下。

    只认「结果里出现了门禁来源」这一种形态。刻意不匹配具体拒因文案 ——
    文案会改，而"被拦了"这件事的形态不会。

    同时匹配转义形态：当前 ``tools.registry.tool_error`` 用
    ``ensure_ascii=False``，中文原样落在结果里；但万一哪层改成转义，
    只认原文会让探针误报"门禁失效"。误报方向虽然安全，却会消耗对告警的信任。
    """
    GATE_SOURCE = _gate_source()

    text = raw if isinstance(raw, str) else json.dumps(raw, ensure_ascii=False, default=str)
    if GATE_SOURCE in text:
        return True
    # json.dumps(..., ensure_ascii=True) 会把中文转成 \uXXXX；去掉两端引号后比对
    escaped = json.dumps(GATE_SOURCE)[1:-1]
    return escaped in text


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    result = probe()
    payload = {
        "event": "bi_gate_probe",
        "status": result.status,
        "detail": result.detail,
    }
    line = json.dumps(payload, ensure_ascii=False)
    if result.status == ALIVE:
        logger.info(line)
    else:
        # 失效和探针自身出错都走 stderr，方便 cron 只在异常时发邮件。
        print(line, file=sys.stderr)
    return result.exit_code


if __name__ == "__main__":
    raise SystemExit(main())
