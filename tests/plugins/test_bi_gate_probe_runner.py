"""探针调度器的测试。

调度器本身是「发现门禁失效」这条链路的最后一环。它挂了或判错了，前面所有的
拦截、留痕、告警都没人看得见 —— 所以它的失败模式要一条条钉住。

这里的 run_one 测试**不打桩 subprocess**，而是把 ``PROBE`` 指向一个受控的假
探针脚本，让真实的子进程路径跑一遍。理由和调度器自己用子进程跑 probe.py 一样：
调研 §二第六条那次栽跟头，就是因为所有测试都绕过了真实执行路径。
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest

PLUGIN_DIR = Path(__file__).resolve().parents[2] / "plugins" / "bi-gate"


def _load():
    spec = importlib.util.spec_from_file_location(
        "bi_gate_probe_runner_under_test",
        PLUGIN_DIR / "probe_runner.py",
        submodule_search_locations=[str(PLUGIN_DIR)],
    )
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture()
def rn():
    return _load()


def _fake_probe(tmp_path: Path, *, stream: str, status: str, code: int, sleep: float = 0.0) -> Path:
    """造一个行为可控的假探针。

    ``stream`` 决定它把 JSON 打到 stdout 还是 stderr —— 真的 probe.py 两种结果
    都走 stderr，这是调度器第一版踩过的坑，所以两种都要能解析。
    """
    payload = json.dumps({"event": "bi_gate_probe", "status": status,
                          "detail": f"假探针：{status}"}, ensure_ascii=False)
    script = tmp_path / f"fake_probe_{stream}_{status}.py"
    script.write_text(
        "import sys, time, json\n"
        f"time.sleep({sleep})\n"
        f"print({payload!r}, file=sys.{stream})\n"
        f"sys.exit({code})\n",
        encoding="utf-8",
    )
    return script


@pytest.fixture()
def profile(tmp_path):
    p = tmp_path / "profiles" / "bi"
    p.mkdir(parents=True)
    (p / ".env").write_text("BI_GATE_TOOLS=query_metric\n", encoding="utf-8")
    return p


# ── profile 的声明：.env 解析 ───────────────────────────────────────────

def test_env_file_basic(rn, tmp_path):
    f = tmp_path / ".env"
    f.write_text(
        "# 注释\n"
        "\n"
        "BI_GATE_ACTION_MAX=L1\n"
        '  BI_GATE_TOOLS = "query_metric,foo"  \n'
        "EMPTY=\n"
        "没有等号的行\n",
        encoding="utf-8",
    )
    got = rn.load_env_file(f)
    assert got["BI_GATE_ACTION_MAX"] == "L1"
    assert got["BI_GATE_TOOLS"] == "query_metric,foo", "引号要剥掉，前后空格要去掉"
    assert got["EMPTY"] == ""
    assert "没有等号的行" not in got


def test_env_file_does_not_execute_anything(rn, tmp_path):
    """只做静态解析，不 source shell、不展开变量。

    profile 的声明必须是能被静态读懂的东西 —— 需要执行才能得出的配置，本身
    就是审批不了的。
    """
    f = tmp_path / ".env"
    f.write_text("X=$(touch /tmp/should_not_exist_bi_gate)\nY=${HOME}/x\n", encoding="utf-8")
    got = rn.load_env_file(f)
    assert got["X"] == "$(touch /tmp/should_not_exist_bi_gate)"
    assert got["Y"] == "${HOME}/x", "变量不展开"
    assert not Path("/tmp/should_not_exist_bi_gate").exists()


def test_missing_env_file_is_not_an_error(rn, tmp_path):
    assert rn.load_env_file(tmp_path / "nope.env") == {}


def test_build_env_sets_home_and_pythonpath(rn, profile):
    env = rn.build_env(profile)
    assert env["HERMES_HOME"] == str(profile)
    assert str(rn.REPO) in env["PYTHONPATH"]
    assert env["BI_GATE_TOOLS"] == "query_metric", ".env 的声明要进环境"


def test_profile_env_overrides_process_env(rn, profile, monkeypatch):
    """profile 的声明优先于进程环境 —— 与 Hermes CLI 的行为一致（实测确认）。"""
    monkeypatch.setenv("BI_GATE_TOOLS", "从进程来的")
    assert rn.build_env(profile)["BI_GATE_TOOLS"] == "query_metric"


# ── 跑一次：状态判定 ────────────────────────────────────────────────────

@pytest.mark.parametrize("stream", ["stdout", "stderr"])
def test_parses_json_from_either_stream(rn, profile, tmp_path, monkeypatch, stream):
    """两个流都要解析。

    这条钉住的是调度器第一版的 bug：只看了 stdout，而真的 probe.py 两种结果
    都走 stderr（alive 走 logger.info，logging 默认 handler 就是 stderr）。
    表现是每次都报「输出解析不了，按退出码推断」—— 状态碰巧还对，所以不看
    细节发现不了。
    """
    monkeypatch.setattr(rn, "PROBE", _fake_probe(tmp_path, stream=stream, status="alive", code=0))
    rec = rn.run_one(profile)
    assert rec["status"] == "alive"
    assert rec["detail"] == "假探针：alive", "细节要来自探针输出，不是推断出来的"
    assert "stdout_tail" not in rec, "解析成功就不该留原始输出"


def test_gate_down_is_reported(rn, profile, tmp_path, monkeypatch):
    monkeypatch.setattr(rn, "PROBE", _fake_probe(tmp_path, stream="stderr",
                                                 status="gate_down", code=1))
    rec = rn.run_one(profile)
    assert rec["status"] == "gate_down"
    assert rec["exit_code"] == 1


def test_unparseable_output_falls_back_to_exit_code_and_keeps_raw(rn, profile, tmp_path, monkeypatch):
    """解析不了就不猜，如实记原始输出 —— 两个流都要留。"""
    bad = tmp_path / "bad_probe.py"
    bad.write_text("import sys\nprint('不是 JSON')\nprint('也不是', file=sys.stderr)\n"
                   "sys.exit(1)\n", encoding="utf-8")
    monkeypatch.setattr(rn, "PROBE", bad)
    rec = rn.run_one(profile)
    assert rec["status"] == "gate_down"
    assert "解析不了" in rec["detail"]
    assert "不是 JSON" in rec["stdout_tail"]
    assert "也不是" in rec["stderr_tail"]


def test_unknown_exit_code_is_probe_error(rn, profile, tmp_path, monkeypatch):
    bad = tmp_path / "crash_probe.py"
    bad.write_text("import sys\nsys.exit(2)\n", encoding="utf-8")
    monkeypatch.setattr(rn, "PROBE", bad)
    assert rn.run_one(profile)["status"] == "probe_error"


@pytest.mark.live_system_guard_bypass
def test_timeout_counts_as_gate_down_not_probe_error(rn, profile, tmp_path, monkeypatch):
    """卡住时按门禁失效处理。

    探针卡住，我们并不知道门禁是死是活；而「不知道」在这套体系里一贯当成
    不安全（同 UNDECIDABLE 的处理方向）。报成 probe_error 会让人以为只是
    监控自己的毛病，从而忽略它。

    为什么要 live_system_guard_bypass：``subprocess.run(timeout=...)`` 超时后
    要真的 kill 掉子进程，而仓库的 conftest 守卫默认拦下测试进程子树之外的
    ``os.kill``。这条用例确实需要真实的信号投递——它验的就是"探针卡住时会
    发生什么"，打桩 subprocess 就等于不验这条路径。
    """
    slow = _fake_probe(tmp_path, stream="stderr", status="alive", code=0, sleep=5)
    monkeypatch.setattr(rn, "PROBE", slow)
    rec = rn.run_one(profile, timeout=0.4)
    assert rec["status"] == "gate_down"
    assert rec["exit_code"] == 1
    assert "未返回" in rec["detail"]


def test_missing_profile_dir_is_probe_error(rn, tmp_path):
    rec = rn.run_one(tmp_path / "不存在")
    assert rec["status"] == "probe_error"
    assert rec["exit_code"] == 2


def test_record_carries_identity_and_timing(rn, profile, tmp_path, monkeypatch):
    monkeypatch.setattr(rn, "PROBE", _fake_probe(tmp_path, stream="stderr", status="alive", code=0))
    rec = rn.run_one(profile)
    assert rec["event"] == "bi_gate_probe_run"
    assert rec["source"] == "bi-gate-probe-runner"
    assert rec["profile"] == "bi"
    assert isinstance(rec["ts"], int) and rec["ts"] > 0
    assert isinstance(rec["duration_ms"], int)


# ── 落盘 ────────────────────────────────────────────────────────────────

def test_writes_one_json_line_and_appends(rn, profile):
    for i in range(3):
        rn.write_record(profile, {"event": "bi_gate_probe_run", "n": i})
    lines = (profile / "probe.jsonl").read_text(encoding="utf-8").splitlines()
    assert len(lines) == 3
    assert [json.loads(l)["n"] for l in lines] == [0, 1, 2]


def test_probe_log_path_can_be_overridden(rn, profile, tmp_path, monkeypatch):
    target = tmp_path / "elsewhere" / "probe.jsonl"
    monkeypatch.setenv("BI_PROBE_LOG", str(target))
    assert rn.probe_log_path(profile) == target
    rn.write_record(profile, {"x": 1})
    assert target.exists(), "父目录要自动建出来"


def test_write_failure_does_not_raise(rn, profile, tmp_path, monkeypatch):
    blocked = tmp_path / "afile"
    blocked.write_text("x", encoding="utf-8")
    monkeypatch.setenv("BI_PROBE_LOG", str(blocked / "probe.jsonl"))
    assert rn.write_record(profile, {"x": 1}) is False


# ── 上报 ────────────────────────────────────────────────────────────────

def test_sink_payload_is_the_record_itself(rn):
    """上报的就是落盘那条记录，只多两个路由字段。

    落盘和上报共用一份数据是有意的：两套格式意味着两处会漂移，而「盘上写的」
    和「报上去的」对不上，是事后对账时最难查的一类问题。
    """
    rec = {"event": "bi_gate_probe_run", "profile": "bi", "status": "alive",
           "exit_code": 0, "detail": "门禁在工作", "ts": 1787800000}
    doc = json.loads(rn.render_sink_payload([rec]).strip())
    for k, v in rec.items():
        assert doc[k] == v, f"原记录的 {k} 不该被改动"
    assert doc["app"] == "bi-gate-probe"
    assert doc["time"] == "2026-08-27T03:06:40Z", "VictoriaLogs 要 RFC3339"


def test_sink_payload_is_one_line_per_record(rn):
    out = rn.render_sink_payload([
        {"profile": "bi", "status": "alive", "ts": 1787800000},
        {"profile": "cs", "status": "gate_down", "ts": 1787800000},
    ])
    lines = [l for l in out.splitlines() if l.strip()]
    assert len(lines) == 2
    assert {json.loads(l)["profile"] for l in lines} == {"bi", "cs"}
    assert out.endswith("\n")


def test_ts_stays_an_integer(rn):
    """``ts`` 保持整数 epoch，另加 ``time`` 给 VictoriaLogs 用。

    时间字段有两个不是冗余：整数好做算术（比如算探针间隔），RFC3339 是
    VictoriaLogs 的要求。合成一个就得在某一边做转换。
    """
    doc = json.loads(rn.render_sink_payload([{"profile": "bi", "ts": 1787800000}]).strip())
    assert isinstance(doc["ts"], int)
    assert isinstance(doc["time"], str)


def test_query_string_is_appended_by_code_not_config(rn, monkeypatch):
    """字段名由代码补齐，不写在配置里。

    这几个字段名是我们和 Grafana 查询之间的契约。写在配置里的话，改一处忘
    一处就会「数据进去了但查不到」—— 那种故障在告警上和「没数据」长得一样。
    """
    monkeypatch.setenv("BI_PROBE_SINK_URL", "https://vlogs.example.com/insert/jsonline")
    url = rn._sink_url()
    assert "_stream_fields=app,profile" in url
    assert "_msg_field=detail" in url
    assert "_time_field=time" in url


def test_explicit_query_string_is_respected(rn, monkeypatch):
    custom = "https://vlogs.example.com/insert/jsonline?_stream_fields=app"
    monkeypatch.setenv("BI_PROBE_SINK_URL", custom)
    assert rn._sink_url() == custom, "操作者显式给了参数就照他的来"


def test_no_url_means_skip_not_failure(rn, monkeypatch):
    """没配地址就不推 —— 不报错、不重试。

    上报失败是遥测链路的问题，把它算成门禁失效会制造假警报。
    """
    monkeypatch.delenv("BI_PROBE_SINK_URL", raising=False)
    ok, note = rn.push_records("{}\n")
    assert ok is False
    assert "BI_PROBE_SINK_URL" in note


def test_push_failure_is_reported_not_raised(rn, monkeypatch):
    monkeypatch.setenv("BI_PROBE_SINK_URL", "http://127.0.0.1:1/nope")
    ok, note = rn.push_records("{}\n")
    assert ok is False and note


def test_push_failure_does_not_change_exit_code(rn, profile, tmp_path, monkeypatch):
    """上报挂了，探针结论不变 —— 门禁是好的就还是 0。"""
    monkeypatch.setenv("BI_PROBE_SINK_URL", "http://127.0.0.1:1/nope")
    monkeypatch.setattr(rn, "PROBE", _fake_probe(tmp_path, stream="stderr",
                                                 status="alive", code=0))
    assert rn.main(["x", str(profile)]) == 0


# ── 退出码 ──────────────────────────────────────────────────────────────

def test_main_exit_codes(rn, profile, tmp_path, monkeypatch):
    monkeypatch.delenv("BI_PROBE_SINK_URL", raising=False)

    monkeypatch.setattr(rn, "PROBE", _fake_probe(tmp_path, stream="stderr", status="alive", code=0))
    assert rn.main(["x", str(profile)]) == 0

    monkeypatch.setattr(rn, "PROBE", _fake_probe(tmp_path, stream="stderr",
                                                 status="gate_down", code=1))
    assert rn.main(["x", str(profile)]) == 1, "任一 profile 失效，整体就必须非 0"


def test_no_arguments_is_an_error_not_a_silent_success(rn):
    """不给参数不能悄悄返回 0 —— 那会让一个配错的 timer 看起来一切正常。"""
    assert rn.main(["x"]) == 2


def test_one_bad_profile_fails_the_whole_run(rn, profile, tmp_path, monkeypatch):
    monkeypatch.delenv("BI_PROBE_SINK_URL", raising=False)
    monkeypatch.setattr(rn, "PROBE", _fake_probe(tmp_path, stream="stderr", status="alive", code=0))
    assert rn.main(["x", str(profile), str(tmp_path / "不存在")]) == 1
