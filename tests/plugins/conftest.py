"""bi-gate / bi-query 测试的共用夹具。

为什么要有这个文件
------------------
2026-08-28 加了身份透传之后，门禁对每次查询都要求「以谁的名义查」。而所有
既有测试都是在无身份状态下跑的，于是 32 个测试同时变红 —— 行为是对的，但
那些测试测的是指标/维度/时区/预算，身份跟它们无关。

**这里绑的是一个真实身份，走的是生产同一条路**（``set_session_vars`` 绑
ContextVar + 一张真的主体映射表），不是给测试开的后门。开后门的话，哪天有人
把身份检查整个删掉，这 32 个测试照样全绿。

守住身份检查本身的，是 ``test_bi_gate_identity.py`` 里那几条 —— 它们用
``@pytest.mark.no_bi_identity`` 退出这个夹具，专门验「没身份会怎样」。
"""

from __future__ import annotations

import json

import pytest

#: 夹具绑定的测试主体。故意用一眼看得出是假的的值。
TEST_PLATFORM_ID = "ou_test_open_id"
TEST_SUBJECT = "bi_test_principal"


def pytest_configure(config):
    config.addinivalue_line(
        "markers",
        "no_bi_identity: 不要自动绑定 BI 测试身份（专门验没有身份时的行为）",
    )


@pytest.fixture(autouse=True)
def bi_identity(request, tmp_path_factory, monkeypatch):
    """给 bi 相关测试绑一个合法身份。

    只对 ``test_bi_*`` 生效 —— 别的插件测试不该被这个影响。
    """
    if not request.node.nodeid.split("/")[-1].startswith("test_bi_"):
        yield
        return
    if request.node.get_closest_marker("no_bi_identity"):
        yield
        return

    map_path = tmp_path_factory.mktemp("bi_principals") / "principals.json"
    map_path.write_text(json.dumps({
        "principals": {
            TEST_PLATFORM_ID: {"subject": TEST_SUBJECT, "display": "测试主体"},
        }
    }, ensure_ascii=False), encoding="utf-8")
    monkeypatch.setenv("BI_GATE_PRINCIPAL_MAP", str(map_path))

    try:
        from gateway.session_context import set_session_vars, clear_session_vars
    except Exception:                      # pragma: no cover
        pytest.skip("拿不到 gateway.session_context —— 身份夹具没法绑")

    tokens = set_session_vars(
        platform="feishu",
        user_id=TEST_PLATFORM_ID,
        user_name="测试主体",
        session_id="bi-test-session",
    )
    try:
        yield
    finally:
        clear_session_vars(tokens)
