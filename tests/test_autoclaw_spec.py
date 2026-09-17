"""AutoClaw spec（specs/autoclaw.py）的翻译契约。

spec 移植自 autoclaw2api（https://github.com/ZFXing-lite/autoclaw2api，Go 网关）——本测试
锁住移植层的行为契约，防止后续改框架钩子模型时悄悄破坏：
1. spec 能被 code_loader 加载（普通类 + 可识别钩子）；
2. 类级开关（token 自动续期 / 定时刷新 / 关伪装头 / 渠道地址可空）；
3. 模型归一对齐 Go NormalizeModel（agent 目标名透传 / 后端模型 → openclaw + 头）；
4. relay base 归一对齐 Go RelayBase（ws→http、去 query、截断 /autoclaw-cloud、proxy 形态）；
5. 错误分类对齐 Go Classify（410000/unauthorized → 401、402/余额不足 → 402、429 透传）；
6. 凭证 JSON 解析（autoclaw-*.json 全文 → 拆字段，camelCase/snake_case 双认）；
7. relay 事件流转换（agent:stream 累积快照前缀差分 / chat 事件 toolCalls+usage / error 处置）；
8. relay 通道模型经 x-openclaw-model 头指定、message 必须是字符串；
9. 双通道端到端：OpenAI 端点出站形态 + NotEnabled 回退 relay。

specs/ 在 .gitignore 内、由使用者自持，文件不存在时整个模块跳过。
"""
from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path

import pytest
from fastapi import HTTPException

from providers.code_loader import load_code_provider_class, invalidate_cache

_SPEC_PATH = Path(__file__).resolve().parent.parent / "specs" / "autoclaw.py"

if not _SPEC_PATH.exists():
    pytest.skip("specs/autoclaw.py 不存在（使用者自持）", allow_module_level=True)

_SOURCE = _SPEC_PATH.read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def channel_cls():
    cls = load_code_provider_class("autoclaw-test", _SOURCE)
    yield cls
    invalidate_cache("autoclaw-test")


def _make_provider(channel_cls, *, password: str = "", **extra):
    """造一个池外实例（不挂渠道），需要的字段全部 kwargs 注入。"""
    return channel_cls(username="test", password=password, _api_key=password, **extra)


def _module():
    """加载 spec 模块本身（拿模块级 helper；与 code_loader 的 exec 命名空间同一套定义）。"""
    import importlib.util
    spec = importlib.util.spec_from_file_location("autoclaw_spec_module", _SPEC_PATH)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _run(coro):
    return asyncio.run(coro)


def _patch_namespace(channel_cls, **overrides):
    """把假函数塞进 spec 函数的 exec 命名空间（钩子运行时按 globals 查名）。"""
    hook = channel_cls._spec_hooks.get("stream_chat") or channel_cls._spec_hooks.get("refresh_auth")
    for name, fn in overrides.items():
        hook.__globals__[name] = fn


# ==================== 加载与声明 ====================

def test_spec_loads_with_expected_hooks(channel_cls):
    hooks = channel_cls._spec_hooks
    assert {
        "account_schema", "is_init", "init_auth", "check_auth", "health_check",
        "refresh_auth", "fetch_models", "stream_chat",
    }.issubset(hooks)


def test_class_flags(channel_cls):
    assert channel_cls.SUPPORTS_MULTI_MESSAGES is True
    assert channel_cls.SUPPORTS_TOKEN_AUTO_REFRESH is True
    assert channel_cls.SCHEDULED_REFRESH is True
    # 沙箱网关只认用户 Bearer token，必须关掉伪装头。
    assert channel_cls.APPLY_CLIENT_PRESET is False
    # userapi 基址按 region 官方域名兜底，渠道地址可留空。
    assert channel_cls.REQUIRES_BASE_URL is False


def test_account_schema_declares_device_code_loopback(channel_cls):
    schema = channel_cls.account_schema()
    assert schema["provider_name"] == "autoclaw-test"
    # 短信（cn）授权已接入：device_code + loopback 补投；global 走手动填凭证。
    assert "device_code" in schema["add_methods"]
    assert schema["auth_start"]["mode"] == "device_code"
    assert schema["auth_start"]["completion"] == "loopback"
    keys = {f["key"] for f in schema["fields"]}
    assert {"password", "refresh_token", "region", "phone", "device_id", "quota_text",
            "sandbox_id", "sandbox_endpoint"} <= keys


def test_account_fields_cover_runtime_state(channel_cls):
    fields = set(channel_cls.ACCOUNT_FIELDS)
    assert {"refresh_token", "expires_at", "device_id", "region",
            "sandbox_id", "sandbox_endpoint", "sandbox_end_ts", "quota_text"} <= fields


def test_region_is_first_field_and_select(channel_cls):
    """region 必须是首个字段的下拉框：cn/global 决定授权方式与域名，文本框没人看得懂。

    前端按 section 首次出现顺序分组、组内保持后端数组顺序，故 fields[0] 即「供应商字段」
    区第一行（对齐 codebuddy.py 的 region 写法）。
    """
    fields = channel_cls.account_schema()["fields"]
    assert fields[0]["key"] == "region"
    region = fields[0]
    assert region["type"] == "select"
    assert region["default_value"] == "cn"
    assert {opt["value"] for opt in region["options"]} == {"cn", "global"}
    # 每个 option 都要有中文标签（前端直接渲染 label，不渲染裸值）
    assert all(opt.get("label") for opt in region["options"])


# ==================== 凭证 JSON 解析 ====================

def _credential_json() -> str:
    return json.dumps({
        "accessToken": "Bearer at-xyz",
        "refreshToken": "rt-xyz",
        "expiresAt": int(time.time()) + 3600,
        "userId": 12345,
        "userName": "张三",
        "phone": "138****8000",
        "deviceId": "dev-1",
        "region": "global",
        "sandboxId": "sb-1",
        "sandboxEndpoint": "wss://gw.example.com/autoclaw-cloud/ws?sandbox_id=sb-1&port=1",
        "endTimestamp": int(time.time()) + 7200,
    })


def test_credential_json_parses_into_fields(channel_cls):
    mod = _module()
    p = _make_provider(channel_cls, password=_credential_json())
    assert mod._maybe_parse_credential_json(p) is True
    assert p.password == "Bearer at-xyz"
    assert p.refresh_token == "rt-xyz"
    assert p.user_id == "12345"
    assert p.region == "global"
    assert p.device_id == "dev-1"
    assert p.sandbox_id == "sb-1"
    # expiresAt 有值就保留，不覆盖成 28 天
    assert int(p.expires_at) > int(time.time())


# ==================== 写死的 cn / global 凭证 ====================

def test_hardcoded_credential_used_when_account_blank(channel_cls):
    """账号页 password 留空 → 按 region 套用写死凭证（cn/global 各一份）。"""
    mod = _module()
    saved = dict(mod.HARDCODED_CREDENTIALS)
    mod.HARDCODED_CREDENTIALS = {
        "cn": {"accessToken": "Bearer hard-cn", "refreshToken": "rt-cn",
               "deviceId": "dev-cn", "region": "cn"},
        "global": {"accessToken": "Bearer hard-gl", "refreshToken": "rt-gl",
                   "deviceId": "dev-gl", "userName": "g@example.com", "region": "global"},
    }
    try:
        p = _make_provider(channel_cls, password="")   # 不传 region → 默认 cn
        assert mod._maybe_parse_credential_json(p) is True
        assert p.password == "Bearer hard-cn"
        assert p.refresh_token == "rt-cn"
        assert p.device_id == "dev-cn"   # device_id 必须写死，refresh 才对得上

        g = _make_provider(channel_cls, password="", region="global")
        assert mod._maybe_parse_credential_json(g) is True
        assert g.password == "Bearer hard-gl"
        assert g.region == "global"
    finally:
        mod.HARDCODED_CREDENTIALS = saved


def test_hardcoded_credential_unset_is_empty(channel_cls):
    """常量为空占位（未配置）→ 不认凭证，账号视为未授权。"""
    mod = _module()
    saved = dict(mod.HARDCODED_CREDENTIALS)
    mod.HARDCODED_CREDENTIALS = {"cn": {"accessToken": ""}, "global": {"accessToken": ""}}
    try:
        p = _make_provider(channel_cls, password="")
        assert mod._hardcoded_credential(p) == {}
        assert mod._maybe_parse_credential_json(p) is False
    finally:
        mod.HARDCODED_CREDENTIALS = saved


def test_pasted_credential_beats_hardcoded(channel_cls):
    """账号页粘贴的凭证永远优先，写死常量不覆盖它。"""
    mod = _module()
    saved = dict(mod.HARDCODED_CREDENTIALS)
    mod.HARDCODED_CREDENTIALS = {"cn": {"accessToken": "Bearer hard-cn"}}
    try:
        p = _make_provider(channel_cls, password="Bearer typed-token")
        assert mod._maybe_parse_credential_json(p) is False
        assert p.password == "Bearer typed-token"
    finally:
        mod.HARDCODED_CREDENTIALS = saved


def test_check_auth_accepts_hardcoded_credential(channel_cls):
    """账号页留空但写死凭证已配 → check_auth/health_check/is_init 都认。"""
    # 类钩子在自己的 exec 命名空间里跑，常量要打到那里才生效
    ns = channel_cls._spec_hooks["check_auth"].__globals__
    saved = ns["HARDCODED_CREDENTIALS"]
    ns["HARDCODED_CREDENTIALS"] = {"cn": {"accessToken": "Bearer hard-cn"}, "global": {}}
    try:
        p = _make_provider(channel_cls, password="")
        assert _run(channel_cls.check_auth(p)) is True
        assert _run(channel_cls.health_check(p)) is True
        assert channel_cls.is_init(p) is True

        blank = _make_provider(channel_cls, password="", region="global")
        assert _run(channel_cls.check_auth(blank)) is False   # global 那份是空的
    finally:
        ns["HARDCODED_CREDENTIALS"] = saved


def test_credential_json_without_token_is_noop(channel_cls):
    mod = _module()
    p = _make_provider(channel_cls, password='{"foo": 1}')
    assert mod._maybe_parse_credential_json(p) is False


def test_bearer_value_idempotent():
    mod = _module()
    assert mod._bearer_value("Bearer abc") == "Bearer abc"
    assert mod._bearer_value("abc") == "Bearer abc"
    assert mod._strip_bearer("Bearer abc") == "abc"
    assert mod._strip_bearer("abc") == "abc"


# ==================== 模型归一（对齐 Go NormalizeModel 向量）===================

def test_normalize_model_vectors():
    mod = _module()
    cases = [
        ("openclaw", "openclaw", ""),
        ("openclaw/default", "openclaw/default", ""),
        ("openclaw/designer", "openclaw/designer", ""),
        ("agent:analyst", "agent:analyst", ""),
        ("glm-5.3-flash", "openclaw", "glm-5.3-flash"),
        ("glm-5.2", "openclaw", "glm-5.2"),
        ("", "openclaw", ""),
    ]
    for raw, model, xmodel in cases:
        assert mod._normalize_model(raw) == (model, xmodel), raw


# ==================== relay base 归一（对齐 Go RelayBase 向量）===================

def test_relay_base_normalization_vectors():
    mod = _module()
    cases = [
        ("https://sb1.example.com", "https://sb1.example.com/autoclaw-cloud"),
        ("https://sb1.example.com/autoclaw-cloud", "https://sb1.example.com/autoclaw-cloud"),
        ("wss://sb1.example.com/", "https://sb1.example.com/autoclaw-cloud"),
        ("https://sb1.example.com/autoclaw-cloud/", "https://sb1.example.com/autoclaw-cloud"),
        ("https://sb1.example.com/foo", "https://sb1.example.com/foo/autoclaw-cloud"),
        ("wss://autoglm-api.zhipuai.cn/autoclaw-cloud/ws?sandbox_id=sb1&port=29000&path=/ws",
         "https://autoglm-api.zhipuai.cn/autoclaw-cloud"),
        ("https://sb.example.com/autoclaw-cloud/ws?x=1&y=2", "https://sb.example.com/autoclaw-cloud"),
        ("ws://h.example.com/autoclaw-cloud/ws?p=1", "http://h.example.com/autoclaw-cloud"),
    ]
    for raw, want in cases:
        assert mod._relay_base(raw) == want, raw


def test_relay_proxy_and_bare_base():
    mod = _module()
    endpoint = "wss://autoglm-api.zhipuai.cn/autoclaw-cloud/ws?sandbox_id=sb1&port=1"
    proxy = mod._relay_proxy_base(endpoint, "sb1")
    assert proxy == "https://autoglm-api.zhipuai.cn/autoclaw-cloud/proxy/sb1"
    assert mod._relay_bare_base(proxy) == "https://autoglm-api.zhipuai.cn/autoclaw-cloud"
    assert mod._relay_bare_base("https://gw/autoclaw-cloud") == "https://gw/autoclaw-cloud"


def test_sign_headers_shape():
    mod = _module()
    headers = mod._sign_headers("Bearer at-1")
    sign = headers["X-Auth-Sign"]
    assert len(sign) == 32 and all(c in "0123456789abcdef" for c in sign)
    assert headers["X-Auth-Appid"] == "100003"
    assert headers["X-Product"] == "autoclaw"
    assert headers["X-Version"] == "1.18.5"
    assert headers["Authorization"] == "Bearer at-1"
    # WAF 要求：overseasv1 系端点必须带浏览器 UA + Origin/Referer，否则 405
    assert "Mozilla/5.0" in headers["User-Agent"]
    assert headers["Origin"] == "https://autoclaw.z.ai"
    assert headers["Referer"] == "https://autoclaw.z.ai/web/"
    # 无 token 时（send-code 类）不带 Authorization
    assert "Authorization" not in mod._sign_headers("")


# ==================== 错误分类（对齐 Go Classify 向量）===================

def test_classify_upstream_error_vectors():
    mod = _module()

    def status_of(exc):
        return exc.status_code

    # 410000 业务码 / unauthorized 文案 → 会话死亡 401
    assert status_of(mod._classify_upstream_error(200, 410000, "Not logged in")) == 401
    assert status_of(mod._classify_upstream_error(200, 0, "Unauthorized web bridge request")) == 401
    # 余额不足 → 402
    assert status_of(mod._classify_upstream_error(402, 0, "insufficient credit")) == 402
    assert status_of(mod._classify_upstream_error(200, 0, "积分不足")) == 402
    # 429 透传
    assert status_of(mod._classify_upstream_error(429, 0, "rate limit")) == 429
    # 其余透传
    assert status_of(mod._classify_upstream_error(404, 0, "no route")) == 404
    assert status_of(mod._classify_upstream_error(500, 0, "boom")) == 500


def test_endpoint_not_enabled_detection():
    mod = _module()
    assert mod._is_endpoint_not_enabled(HTTPException(404, "no route")) is True
    assert mod._is_endpoint_not_enabled(HTTPException(501, "not implemented")) is True
    assert mod._is_endpoint_not_enabled(
        HTTPException(400, "chatCompletions endpoint is not enabled")) is True
    assert mod._is_endpoint_not_enabled(HTTPException(400, "invalid model")) is False
    assert mod._is_endpoint_not_enabled(HTTPException(500, "boom")) is False


# ==================== relay 事件流转换 ====================

def _sse_frame(event: str, payload: dict) -> str:
    return f"event: {event}\ndata: {json.dumps({'event': event, 'payload': payload}, ensure_ascii=False)}\n\n"


def test_relay_feed_agent_stream_prefix_diff():
    """累积快照 delta → 前缀差分出增量（对齐 Go emitText）。"""
    mod = _module()
    state = mod._new_relay_state("r1", "aclaw-x")
    out = mod._relay_feed(state, _sse_frame("agent:stream", {
        "runId": "r1", "type": "text", "delta": "Hello"}))
    assert [f["content"] for f in out] == ["Hello"]
    out = mod._relay_feed(state, _sse_frame("agent:stream", {
        "runId": "r1", "type": "text", "delta": "Hello world"}))
    assert [f["content"] for f in out] == [" world"]
    # 重复快照不重复发
    assert mod._relay_feed(state, _sse_frame("agent:stream", {
        "runId": "r1", "type": "text", "delta": "Hello world"})) == []


def test_relay_feed_filters_other_runs():
    mod = _module()
    state = mod._new_relay_state("r1", "aclaw-x")
    assert mod._relay_feed(state, _sse_frame("agent:stream", {
        "runId": "r-other", "type": "text", "delta": "noise"})) == []


def test_relay_feed_chat_event_tool_calls_usage_done():
    """chat 事件：message.content 前缀差分 + toolCalls 帧 + final usage + done。"""
    mod = _module()
    state = mod._new_relay_state("r1", "main")
    out = mod._relay_feed(state, _sse_frame("agent", {
        "runId": "r1", "stream": "tool",
        "data": {"phase": "start", "name": "sql_query", "toolCallId": "t-1", "args": {"x": 1}}}))
    assert len(out) == 1
    tc = out[0]["tool_calls"][0]
    assert tc["id"] == "t-1" and tc["function"]["name"] == "sql_query"
    assert json.loads(tc["function"]["arguments"]) == {"x": 1}
    out = mod._relay_feed(state, _sse_frame("chat", {
        "runId": "r1", "sessionKey": "agent:main:main", "state": "final",
        "message": {"content": "Hello world"},
        "usage": {"input": 9, "output": 7}}))
    assert "".join(f.get("content") or "" for f in out) == "Hello world"
    done = out[-1]
    assert done["done"] is True
    # 有工具调用 → finish=tool_calls（让编码客户端正确执行工具）
    assert done["finish_reason"] == "tool_calls"
    assert done["usage"] == {"prompt_tokens": 9, "completion_tokens": 7, "total_tokens": 16}


def test_relay_feed_done_without_usage_falls_to_stop():
    mod = _module()
    state = mod._new_relay_state("r1", "aclaw-x")
    out = mod._relay_feed(state, _sse_frame("agent:stream", {"runId": "r1", "type": "done"}))
    assert out[-1]["done"] is True
    assert out[-1]["finish_reason"] == "stop"
    assert state["done"] is True
    # done 之后的帧全部丢弃
    assert mod._relay_feed(state, _sse_frame("agent:stream", {
        "runId": "r1", "type": "text", "delta": "late"})) == []


def test_relay_feed_error_before_content_raises():
    mod = _module()
    state = mod._new_relay_state("r1", "aclaw-x")
    with pytest.raises(HTTPException) as excinfo:
        mod._relay_feed(state, _sse_frame("agent:stream", {
            "runId": "r1", "type": "error", "message": "LLM request failed"}))
    assert "LLM request failed" in str(excinfo.value.detail)


def test_relay_feed_error_after_content_emits_text_and_finish():
    mod = _module()
    state = mod._new_relay_state("r1", "aclaw-x")
    mod._relay_feed(state, _sse_frame("agent:stream", {"runId": "r1", "type": "text", "delta": "partial"}))
    out = mod._relay_feed(state, _sse_frame("agent:stream", {
        "runId": "r1", "type": "error", "message": "mid-stream failure"}))
    assert "mid-stream failure" in out[0]["content"]
    assert out[-1]["done"] is True


# ==================== relay 通道请求构造 ====================

def test_relay_send_request_backend_model_header(channel_cls):
    mod = _module()
    p = _make_provider(channel_cls, password="Bearer at-1")
    body, headers = mod._relay_send_request(p, "glm-5.3-flash", [
        {"role": "system", "content": "you are helpful"},
        {"role": "user", "content": "hi"},
    ], {})
    # message 必须是字符串（对象会被上游拒）
    assert isinstance(body["args"][0]["message"], str)
    assert "[system]" in body["args"][0]["message"]
    assert "[user]" in body["args"][0]["message"]
    # 后端模型经头指定，body 不带 model
    assert headers["x-openclaw-model"] == "glm-5.3-flash"
    assert "model" not in body["args"][0]
    assert headers["Authorization"] == "Bearer at-1"
    # sessionKey 每请求随机
    _, headers2 = mod._relay_send_request(p, "glm-5.2", [{"role": "user", "content": "hi"}], {})
    assert body["args"][0]["sessionKey"] != _relay_send_request_session(mod, p)


def _relay_send_request_session(mod, p):
    body, _ = mod._relay_send_request(p, "glm-5.2", [{"role": "user", "content": "hi"}], {})
    return body["args"][0]["sessionKey"]


def test_relay_send_request_agent_target_no_header(channel_cls):
    mod = _module()
    p = _make_provider(channel_cls, password="at-1")
    body, headers = mod._relay_send_request(p, "openclaw", [{"role": "user", "content": "hi"}], {})
    assert "x-openclaw-model" not in headers


def test_relay_transcript_empty_messages_raises(channel_cls):
    mod = _module()
    p = _make_provider(channel_cls, password="at-1")
    with pytest.raises(HTTPException):
        mod._relay_transcript([{"role": "user", "content": [{"type": "image_url", "image_url": {"url": "x"}}]}])


# ==================== 双通道端到端 ====================

_OPENAI_SSE = (
    'data: {"id":"c1","choices":[{"index":0,"delta":{"content":"he"},"finish_reason":null}],"usage":null}\n\n'
    'data: {"id":"c1","choices":[{"index":0,"delta":{"content":"llo"},"finish_reason":null}],"usage":null}\n\n'
    'data: {"id":"c1","choices":[{"index":0,"delta":{},"finish_reason":"stop"}],'
    '"usage":{"prompt_tokens":5,"completion_tokens":2,"total_tokens":7}}\n\n'
    "data: [DONE]\n\n"
)


def _ready_provider(channel_cls, *, password="Bearer at-1", **extra):
    p = _make_provider(channel_cls, password=password, **extra)
    now = int(time.time())
    p.expires_at = str(now + 7200)          # token 新鲜，不触发 refresh
    p.refresh_token = "rt-1"
    p.device_id = "dev-1"
    p.sandbox_id = "sb1"
    p.sandbox_endpoint = "https://gw.example.com/autoclaw-cloud/proxy/sb1"
    p.sandbox_end_ts = str(now + 7200)
    return p


async def _collect_stream(channel_cls, p, model="glm-5.3-flash", messages=None):
    out = []
    async for f in channel_cls._do_stream_chat(
            p, model, messages or [{"role": "user", "content": "hi"}]):
        out.append(f)
    return out


def test_stream_chat_openai_endpoint_end_to_end(channel_cls):
    """通道 1 全链路：出站 URL/模型头 + 标准 OpenAI SSE → 统一帧（content/usage/finish/done）。"""
    p = _ready_provider(channel_cls)

    async def fake_send(method, url, headers, **kw):
        assert url == "https://gw.example.com/autoclaw-cloud/proxy/sb1/v1/chat/completions"
        assert headers["Authorization"] == "Bearer at-1"
        # 后端模型 → body model=openclaw + x-openclaw-model 头
        assert headers["x-openclaw-model"] == "glm-5.3-flash"
        assert kw["json"]["model"] == "openclaw"
        assert kw["json"]["stream"] is True
        for line in _OPENAI_SSE.split("\n\n"):
            if line:
                yield line + "\n\n"

    p.send_sse_request = fake_send
    _patch_namespace(channel_cls, _wake_sandbox=lambda *a, **k: _noop())

    frames = [f for f in _run(_collect_stream(channel_cls, p)) if isinstance(f, dict)]
    assert frames[0] == {}  # 首帧空字典：上游接通信号
    contents = "".join(f.get("content") or "" for f in frames)
    assert contents == "hello"
    usage_frame = next(f for f in frames if isinstance(f.get("usage"), dict))
    assert usage_frame["usage"] == {"prompt_tokens": 5, "completion_tokens": 2, "total_tokens": 7}
    assert any(f.get("finish_reason") == "stop" for f in frames)
    assert any(f.get("done") for f in frames)


async def _noop():
    return None


def test_stream_chat_agent_target_model_no_header(channel_cls):
    """agent 目标名：body 原样透传、无 x-openclaw-model 头。"""
    p = _ready_provider(channel_cls)

    async def fake_send(method, url, headers, **kw):
        assert "x-openclaw-model" not in headers
        assert kw["json"]["model"] == "openclaw/default"
        yield 'data: {"id":"c","choices":[{"index":0,"delta":{"content":"ok"},"finish_reason":"stop"}]}\n\n'
        yield "data: [DONE]\n\n"

    p.send_sse_request = fake_send
    _patch_namespace(channel_cls, _wake_sandbox=lambda *a, **k: _noop())
    frames = [f for f in _run(_collect_stream(channel_cls, p, model="openclaw/default")) if isinstance(f, dict)]
    assert "".join(f.get("content") or "" for f in frames) == "ok"


class _FakeResp:
    def __init__(self, status=200, text="", json_obj=None, chunks=b""):
        self.status = status
        self._text = text
        self._json = json_obj
        self._chunks = chunks

    async def text(self):
        # _relay_json 读 text 再 json.loads：json_obj 优先序列化
        if self._text:
            return self._text
        return json.dumps(self._json) if self._json is not None else ""

    async def json(self):
        return self._json

    @property
    def content(self):
        return self

    async def iter_any(self):
        yield self._chunks


class _FakeCtx:
    def __init__(self, resp):
        self._resp = resp

    async def __aenter__(self):
        return self._resp

    async def __aexit__(self, *a):
        return False


class _FakeSession:
    def __init__(self, get_resp=None, post_resp=None, request_resp=None):
        self._get_resp = get_resp
        self._post_resp = post_resp
        self._request_resp = request_resp
        self.get_urls = []
        self.post_calls = []
        self.request_calls = []

    def get(self, url, headers=None, timeout=None, **kw):
        self.get_urls.append(url)
        return _FakeCtx(self._get_resp)

    def post(self, url, headers=None, json=None, **kw):
        self.post_calls.append({"url": url, "headers": headers, "json": json})
        return _FakeCtx(self._post_resp)

    def request(self, method, url, headers=None, json=None, params=None, timeout=None, **kw):
        self.request_calls.append({"method": method, "url": url, "json": json, "params": params})
        return _FakeCtx(self._request_resp)

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False


_RELAY_EVENTS = (
    _sse_frame("agent:stream", {"runId": "r1", "type": "text", "delta": "Hello"}).encode()
    + _sse_frame("agent:stream", {"runId": "r1", "type": "text", "delta": "Hello world"}).encode()
    + _sse_frame("agent:stream", {"runId": "r1", "type": "done", "usage": {"input": 3, "output": 2}}).encode()
)


def test_stream_chat_falls_back_to_relay_on_not_enabled(channel_cls):
    """通道 1 报 404（端点未启用）→ 自动回退 relay bridge：WS 跳过、events+send 走通。"""
    p = _ready_provider(channel_cls)

    async def not_enabled_send(method, url, headers, **kw):
        raise HTTPException(404, "chatCompletions endpoint is not enabled")
        yield  # pragma: no cover

    p.send_sse_request = not_enabled_send

    events_resp = _FakeResp(status=200, chunks=_RELAY_EVENTS)
    send_resp = _FakeResp(status=200, json_obj={"ok": True, "data": {"runId": "r1"}})
    session = _FakeSession(get_resp=events_resp, post_resp=send_resp)
    p._make_session = lambda timeout=None: session

    async def no_ws(pp, bare):
        return None

    _patch_namespace(channel_cls, _device_online_ws=no_ws, _wake_sandbox=lambda *a, **k: _noop())

    frames = [f for f in _run(_collect_stream(channel_cls, p)) if isinstance(f, dict)]
    # relay 通道走通：events GET + agent/send
    assert session.get_urls == ["https://gw.example.com/autoclaw-cloud/proxy/sb1/api/events"]
    assert session.post_calls[0]["url"] == "https://gw.example.com/autoclaw-cloud/proxy/sb1/api/electron/agent/send"
    assert session.post_calls[0]["headers"]["x-openclaw-model"] == "glm-5.3-flash"
    # 前缀差分 + usage + done
    contents = "".join(f.get("content") or "" for f in frames)
    assert contents == "Hello world"
    done = frames[-1]
    assert done["done"] is True
    assert done["usage"] == {"prompt_tokens": 3, "completion_tokens": 2, "total_tokens": 5}


def test_stream_chat_propagates_non_fallback_errors(channel_cls):
    """通道 1 非 NotEnabled 错误（如 401）原样上抛，不回退 relay。"""
    p = _ready_provider(channel_cls)

    async def unauthorized_send(method, url, headers, **kw):
        raise HTTPException(401, "Unauthorized web bridge request")
        yield  # pragma: no cover

    p.send_sse_request = unauthorized_send
    _patch_namespace(channel_cls, _wake_sandbox=lambda *a, **k: _noop())
    with pytest.raises(HTTPException) as excinfo:
        _run(_collect_stream(channel_cls, p))
    assert excinfo.value.status_code == 401


def test_stream_chat_requires_credential(channel_cls):
    p = _make_provider(channel_cls, password="")
    with pytest.raises(HTTPException) as excinfo:
        _run(_collect_stream(channel_cls, p))
    assert "access_token" in str(excinfo.value.detail)


# ==================== refresh_auth 编排 ====================

def test_refresh_auth_orchestrates_token_points_sandbox(channel_cls):
    """refresh 三件套：token 续期 + 积分写 quota_text + 沙箱缓存归一，字段快照返回。"""
    p = _ready_provider(channel_cls)
    p.expires_at = "0"  # 强制临期 → 触发 refresh

    async def fake_userapi_json(pp, method, path, **kw):
        if path.endswith("/userapi/v1/refresh"):
            return {"access_token": "Bearer at-new", "refresh_token": "rt-new"}
        if path.endswith("/agent-assetmgr/api/v2/wallets"):
            return {"total_balance": "12345"}
        raise AssertionError(f"unexpected path {path}")

    async def fake_ensure_sandbox(pp):
        pp.sandbox_id = "sb2"
        pp.sandbox_endpoint = "https://gw/autoclaw-cloud/proxy/sb2"
        return "sb2", "https://gw/autoclaw-cloud/proxy/sb2"

    hook = channel_cls._spec_hooks["refresh_auth"]
    saved = {name: hook.__globals__.get(name) for name in ("_userapi_json", "_ensure_sandbox")}
    hook.__globals__["_userapi_json"] = fake_userapi_json
    hook.__globals__["_ensure_sandbox"] = fake_ensure_sandbox
    try:
        fields = _run(hook(p, {"password": "Bearer at-old"}))
    finally:
        for name, fn in saved.items():
            hook.__globals__[name] = fn

    assert p.password == "Bearer at-new"
    assert p.refresh_token == "rt-new"
    assert fields["password"] == "Bearer at-new"
    assert fields["quota_text"] == "积分 12,345"
    assert fields["sandbox_id"] == "sb2"


def test_refresh_auth_steps_fail_independently(channel_cls):
    """三件套任一步失败不阻断其余：token 挂了积分照刷。"""
    p = _ready_provider(channel_cls)
    p.expires_at = "0"

    async def failing_refresh(pp, method, path, **kw):
        if path.endswith("/userapi/v1/refresh"):
            raise HTTPException(401, "session dead")
        if path.endswith("/agent-assetmgr/api/v2/wallets"):
            return {"total_balance": 7}
        raise AssertionError(path)

    async def ok_sandbox(pp):
        return "sb1", "https://gw/autoclaw-cloud/proxy/sb1"

    hook = channel_cls._spec_hooks["refresh_auth"]
    saved = {name: hook.__globals__.get(name) for name in ("_userapi_json", "_ensure_sandbox")}
    hook.__globals__["_userapi_json"] = failing_refresh
    hook.__globals__["_ensure_sandbox"] = ok_sandbox
    try:
        fields = _run(hook(p, {}))
    finally:
        for name, fn in saved.items():
            hook.__globals__[name] = fn

    assert fields["quota_text"] == "积分 7"
    assert fields["sandbox_id"] == "sb1"


# ==================== fetch_models ====================

def test_fetch_models_fallback_static_without_credential(channel_cls):
    p = _make_provider(channel_cls, password="")
    models = _run(channel_cls.fetch_upstream_model_list(p))
    ids = [m["id"] for m in models]
    assert "openclaw" in ids and "glm-5.3-flash" in ids and "zai_auto" in ids
    assert p._last_fetch_models_error


def test_fetch_models_dynamic_from_sandbox(channel_cls):
    p = _ready_provider(channel_cls)

    session = _FakeSession(request_resp=_FakeResp(status=200, json_obj={
        "object": "list", "data": [{"id": "openclaw"}, {"id": "openclaw/designer"}]}))
    p._make_session = lambda timeout=None: session
    _patch_namespace(channel_cls, _wake_sandbox=lambda *a, **k: _noop())

    models = _run(channel_cls.fetch_upstream_model_list(p))
    assert [m["id"] for m in models] == ["openclaw", "openclaw/designer"]
    assert p._last_fetch_models_error == ""
    assert session.request_calls[0]["url"].endswith("/v1/models")


# ==================== is_init / check_auth ====================

def test_is_init_and_check_auth(channel_cls):
    mod = _module()
    p = _make_provider(channel_cls, password="")
    assert channel_cls.is_init(p) is False
    assert _run(channel_cls.check_auth(p)) is False
    p2 = _make_provider(channel_cls, password=_credential_json())
    assert channel_cls.is_init(p2) is True
    assert _run(channel_cls.check_auth(p2)) is True
    # 未解析的 JSON（init_auth 未跑）也认已配置
    p3 = _make_provider(channel_cls, password='{"accessToken": "at-1"}')
    assert _run(channel_cls.check_auth(p3)) is True


# ==================== 登录授权（短信 / Google）====================

def test_begin_device_flow_sms_mode(channel_cls):
    """cn + 手机号：发短信、poll_params 带 login=sms + phone，并给出短信交互提示。"""
    p = _ready_provider(channel_cls)
    p.phone = "13800001234"
    sent = {}

    async def fake_send_code(pp, phone):
        sent["phone"] = phone

    _patch_namespace(channel_cls, _sms_send_code=fake_send_code)
    result = _run(channel_cls.begin_device_flow(p))
    assert sent["phone"] == "13800001234"
    assert result["task_type"] == "device_code"
    assert result["poll_params"]["login"] == "sms"
    assert result["poll_params"]["phone"] == "13800001234"
    # 交互提示：补投框要填的是验证码，不是回调 URL
    assert "6 位" in result["replay_hint"]
    assert result["replay_label"]
    assert result["polling_hint"]
    # 短信流程没有要打开的网页——返回 auth_url 会把用户误导去点官网首页
    assert not result.get("auth_url")
    assert not result.get("verification_uri")


def test_begin_device_flow_requires_phone_for_cn(channel_cls):
    """cn 无手机号：明确报错引导填手机号（站内会话没有可粘贴的回调 URL）。"""
    p = _ready_provider(channel_cls)
    p.phone = ""
    with pytest.raises(HTTPException) as excinfo:
        _run(channel_cls.begin_device_flow(p))
    assert "手机号" in str(excinfo.value.detail)


def test_begin_device_flow_global_waits_for_credential_paste(channel_cls):
    """global：不再报错，改为「网页登录 + 凭证补投」的等待态（有完成路径才返回 auth_url）。

    旧实现直接 400，把用户丢在死胡同里——海外区等于无法授权。现在返回 pending 设备码态，
    提示文案直接给出控制台命令，用户照着做即可。
    """
    p = _ready_provider(channel_cls)
    p.phone = ""
    p.region = "global"

    async def fail_send(pp, phone):
        raise AssertionError("global 不应发短信")

    _patch_namespace(channel_cls, _sms_send_code=fail_send)
    result = _run(channel_cls.begin_device_flow(p))

    assert result["task_type"] == "device_code"
    assert result["poll_params"]["login"] == "credential"
    assert result["poll_params"]["region"] == "global"
    # 用户要先在官网登录才能拿到 localStorage，现在有完成路径了，返回它不再误导
    assert "autoclaw.z.ai" in result["auth_url"]
    # 文案必须给可照抄的动作：一句全浏览器可用的 JS（prompt 弹框，不是 copy()）+ 三步指引
    msg = result["message"]
    # 真形态：access token 只在 autoclaw.web.loginInfo 这个 JSON 字符串里（**没有**顶层
    # autoclaw.web.accessToken 键），所以那句 JS 必须解 loginInfo 一起拼，只挑平铺键会漏 token
    assert "prompt(" in msg
    assert "loginInfo" in msg
    assert "autoclaw.web." in msg
    # copy() 只在 Chrome DevTools 控制台有，Firefox/地址栏/书签里会 ReferenceError，不能用
    assert "copy(" not in msg
    assert result["replay_hint"] and result["replay_label"] and result["polling_hint"]


def test_begin_device_flow_global_never_sends_sms_even_with_phone(channel_cls):
    """global 填了手机号也一样走凭证补投：海外短信登录未验证，不能悄悄走没测过的路。"""
    p = _ready_provider(channel_cls)
    p.phone = "13800001234"
    p.region = "global"

    async def fail_send(pp, phone):
        raise AssertionError("global 不应发短信")

    _patch_namespace(channel_cls, _sms_send_code=fail_send)
    result = _run(channel_cls.begin_device_flow(p))
    assert result["poll_params"]["login"] == "credential"


def test_sms_login_body_shape(channel_cls):
    """agent-login：无斜杠路径、code 必须 int、platform=web。"""
    mod = _module()
    p = _make_provider(channel_cls, password="")
    p.device_id = "dev-1"
    calls = {}

    async def fake_userapi(pp, method, path, **kw):
        calls.update({"path": path, "body": kw.get("json_body")})
        return {"access_token": "Bearer at-new", "refresh_token": "rt-new",
                "user_id": "u1", "user_name": "张三", "phone": "13800001234"}

    hook = mod._sms_login
    saved = hook.__globals__.get("_userapi_json")
    hook.__globals__["_userapi_json"] = fake_userapi
    try:
        account = _run(mod._sms_login(p, "13800001234", "123456"))
    finally:
        hook.__globals__["_userapi_json"] = saved

    assert calls["path"] == "/userapi/v1/agent-login"  # 带尾斜杠会 307
    assert calls["body"]["code"] == 123456 and isinstance(calls["body"]["code"], int)
    assert calls["body"]["platform"] == "web"
    assert account["password"] == "Bearer at-new"
    assert account["user_id"] == "u1"
    assert int(account["expires_at"]) > int(time.time())


def test_sms_login_rejects_non_numeric_code(channel_cls):
    mod = _module()
    p = _make_provider(channel_cls, password="")
    with pytest.raises(HTTPException) as excinfo:
        _run(mod._sms_login(p, "13800001234", "abc123"))
    assert "验证码" in str(excinfo.value.detail)


def test_google_oauth_exchange_body_shape(channel_cls):
    """google-oauth-login：免验证码（无 rid），body 对齐官方 web 端 google 分支。"""
    mod = _module()
    p = _make_provider(channel_cls, password="")
    p.device_id = "dev-1"
    calls = {}

    async def fake_userapi(pp, method, path, **kw):
        calls.update({"path": path, "body": kw.get("json_body")})
        return {"access_token": "at-g", "refresh_token": "rt-g",
                "user_id": "u-g", "user_name": "g@example.com"}

    hook = mod._google_oauth_exchange
    saved = hook.__globals__.get("_userapi_json")
    hook.__globals__["_userapi_json"] = fake_userapi
    try:
        account = _run(mod._google_oauth_exchange(p, "code-x", "state-x"))
    finally:
        hook.__globals__["_userapi_json"] = saved

    assert calls["path"] == "/userapi/overseasv1/google-oauth-login"
    body = calls["body"]
    assert body["code"] == "code-x" and body["state"] == "state-x"
    # navigate_uri 跟随 region 的 userapi 域名（测试实例默认 region=cn）
    assert body["navigate_uri"] == "https://autoglm-acceleration-api.zhipuai.cn/userapi/oauth/google/callback"
    assert body["source_id"] == "web"
    assert body["flow_type"] == "web" and body["client_type"] == "web"
    assert account["password"] == "at-g"
    assert account["region"] == "cn"  # 测试实例默认 region=cn（落库时由会话 region 覆盖）


def test_google_oauth_exchange_navigate_uri_follows_region(channel_cls):
    """region=global 的账号用海外域名换 token——写死域名会让它「登录成功但识别不出来」。"""
    mod = _module()
    p = _make_provider(channel_cls, password="", region="global")
    p.device_id = "dev-1"
    seen = {}

    async def fake_userapi(pp, method, path, **kw):
        seen["uri"] = kw.get("json_body", {}).get("navigate_uri")
        return {"access_token": "at-g", "refresh_token": "rt-g"}

    hook = mod._google_oauth_exchange
    saved = hook.__globals__.get("_userapi_json")
    hook.__globals__["_userapi_json"] = fake_userapi
    try:
        _run(mod._google_oauth_exchange(p, "code-x", "state-x"))
    finally:
        hook.__globals__["_userapi_json"] = saved

    assert seen["uri"] == "https://autoglm-api.autoglm.ai/userapi/oauth/google/callback"


def test_google_oauth_exchange_retries_other_state_shape(channel_cls):
    """官方 state 末段为空时会省略尾 '_'：首投失败换另一种形态重试。"""
    mod = _module()
    p = _make_provider(channel_cls, password="")
    p.device_id = "dev-1"
    tried: list[str] = []

    async def fake_userapi(pp, method, path, **kw):
        tried.append(kw.get("json_body", {}).get("state"))
        if len(tried) == 1:
            raise HTTPException(status_code=400, detail="state 不匹配")
        return {"access_token": "at-g", "refresh_token": "rt-g"}

    hook = mod._google_oauth_exchange
    saved = hook.__globals__.get("_userapi_json")
    hook.__globals__["_userapi_json"] = fake_userapi
    try:
        account = _run(mod._google_oauth_exchange(p, "code-x", "state-x_"))
    finally:
        hook.__globals__["_userapi_json"] = saved

    assert tried == ["state-x_", "state-x"]
    assert account["password"] == "at-g"


def test_google_oauth_exchange_does_not_retry_on_401(channel_cls):
    """401 = code 已被消费/授权过期，换 state 形态也救不回来，不能重复打上游。"""
    mod = _module()
    p = _make_provider(channel_cls, password="")
    p.device_id = "dev-1"
    tried: list[str] = []

    async def fake_userapi(pp, method, path, **kw):
        tried.append(kw.get("json_body", {}).get("state"))
        raise HTTPException(status_code=401, detail="code 已失效")

    hook = mod._google_oauth_exchange
    saved = hook.__globals__.get("_userapi_json")
    hook.__globals__["_userapi_json"] = fake_userapi
    try:
        with pytest.raises(HTTPException) as excinfo:
            _run(mod._google_oauth_exchange(p, "code-x", "state-x_"))
    finally:
        hook.__globals__["_userapi_json"] = saved

    assert excinfo.value.status_code == 401
    assert tried == ["state-x_"]  # 只投一次


def test_loopback_sms_claims_and_persists_phone(channel_cls):
    """补投 code=数字 → 短信换 token → account_data 带 phone。"""
    p = _ready_provider(channel_cls)

    async def fake_sms_login(pp, phone, code):
        assert phone == "13800001234" and code == "654321"
        return {"password": "Bearer at-1", "refresh_token": "rt-1",
                "user_id": "u1", "user_name": "张三", "phone": "13800001234",
                "expires_at": "1", "device_id": "dev-1", "region": "cn"}

    _patch_namespace(channel_cls, _sms_login=fake_sms_login)
    outcome = _run(channel_cls.handle_loopback_callback(
        p, {"code": "654321"}, {"login": "sms", "phone": "13800001234"}))
    assert outcome["status"] == "authorized"
    assert outcome["account_data"]["phone"] == "13800001234"


def test_loopback_google_claims_code_without_state(channel_cls):
    """google 会话只粘到 code（没 state）也必须认领——否则海外登录成功却一直 pending。"""
    p = _ready_provider(channel_cls)
    seen = {}

    async def fake_exchange(pp, code, state):
        seen["code"] = code
        seen["state"] = state
        return {"password": "at-g", "refresh_token": "rt-g", "user_id": "u-g",
                "expires_at": "1", "device_id": "dev-1", "region": "global"}

    _patch_namespace(channel_cls, _google_oauth_exchange=fake_exchange)
    outcome = _run(channel_cls.handle_loopback_callback(
        p, {"code": "4/0AY-code"}, {"login": "google"}))
    assert outcome["status"] == "authorized"
    assert outcome["account_data"]["password"] == "at-g"
    assert seen == {"code": "4/0AY-code", "state": ""}


def test_loopback_sms_session_not_hijacked_by_state_param(channel_cls):
    """sms 会话误粘带 state 的 URL：按会话流程走短信，不能被打进 google 换 token。"""
    p = _ready_provider(channel_cls)
    called: list[str] = []

    async def fake_sms_login(pp, phone, code):
        called.append("sms")
        return {"password": "Bearer at-1", "refresh_token": "rt-1", "user_id": "u1",
                "expires_at": "1", "device_id": "dev-1", "region": "cn"}

    async def fake_exchange(pp, code, state):
        called.append("google")
        raise AssertionError("sms 会话不应走 google 换 token")

    _patch_namespace(channel_cls, _sms_login=fake_sms_login,
                     _google_oauth_exchange=fake_exchange)
    outcome = _run(channel_cls.handle_loopback_callback(
        p, {"code": "654321", "state": "leaked"}, {"login": "sms", "phone": "13800001234"}))
    assert outcome["status"] == "authorized"
    assert called == ["sms"]


def test_loopback_unknown_login_marker_not_claimed(channel_cls):
    """认不出的会话标记（既非 sms 也非 google）→ 不认领，交下一个候选会话。"""
    p = _ready_provider(channel_cls)
    outcome = _run(channel_cls.handle_loopback_callback(
        p, {"code": "whatever"}, {"login": "something-else"}))
    assert outcome is None


def test_loopback_google_exchanges_code_state(channel_cls):
    """补投 Google 回调 URL（code+state）→ 免验证码换 token。"""
    p = _ready_provider(channel_cls)

    async def fake_exchange(pp, code, state):
        assert code == "4/0AY" and state == "st-1"
        return {"password": "at-g", "refresh_token": "rt-g", "user_id": "u-g",
                "user_name": "g@example.com", "expires_at": "1",
                "device_id": "dev-1", "region": "global"}

    _patch_namespace(channel_cls, _google_oauth_exchange=fake_exchange)
    outcome = _run(channel_cls.handle_loopback_callback(
        p, {"code": "4/0AY", "state": "st-1"}, {"login": "google"}))
    assert outcome["status"] == "authorized"
    assert outcome["account_data"]["user_id"] == "u-g"


def test_loopback_error_returns_error_status(channel_cls):
    """换 token 失败 → {"status": "error"}（会话终止、前端显示原因），不静默。"""
    p = _ready_provider(channel_cls)

    async def failing_exchange(pp, code, state):
        raise HTTPException(400, "code 已过期")

    _patch_namespace(channel_cls, _google_oauth_exchange=failing_exchange)
    outcome = _run(channel_cls.handle_loopback_callback(
        p, {"code": "x", "state": "y"}, {"login": "google"}))
    assert outcome["status"] == "error"
    assert "过期" in outcome["error"]


# ==================== 补投凭证识别（localStorage / JSON / 裸 token）====================

def test_credential_payload_from_localstorage_json():
    """官网控制台 copy(localStorage.getItem('autoclaw.web')) → 严格 JSON，直接认。"""
    mod = _module()
    doc = mod._credential_doc_from_payload(json.dumps({
        "accessToken": "Bearer at-ls", "refreshToken": "rt-ls",
        "deviceId": "dev-ls", "userId": "u-ls", "region": "global",
    }))
    assert doc["accessToken"] == "Bearer at-ls"
    assert doc["refreshToken"] == "rt-ls"


def test_credential_payload_drills_into_nested_json():
    """整份 localStorage dump / 带外层 key 的形态：递归下钻找到凭证对象。"""
    mod = _module()
    # {"autoclaw.web": {...}} —— 直接嵌对象
    doc = mod._credential_doc_from_payload(json.dumps({
        "autoclaw.web": {"accessToken": "Bearer at-n", "refreshToken": "rt-n"},
        "some.other.key": "noise",
    }))
    assert doc["accessToken"] == "Bearer at-n"
    # {"autoclaw.web": "<JSON 字符串>"} —— localStorage 存的是字符串
    inner = json.dumps({"accessToken": "Bearer at-s", "deviceId": "dev-s"})
    doc = mod._credential_doc_from_payload(json.dumps({"autoclaw.web": inner}))
    assert doc["accessToken"] == "Bearer at-s"
    assert doc["deviceId"] == "dev-s"


def test_credential_payload_from_flat_namespaced_keys(channel_cls):
    """AutoClaw 网页真形态：localStorage 平铺键 + 嵌套 JSON 字符串混排（实测 dump）。

    关键点：**没有顶层 accessToken**——access token 藏在 ``autoclaw.web.loginInfo``
    这个 JSON 字符串里；refreshToken/deviceId/userId 是顶层平铺键；还有键名里嵌 JWT 的
    （``oauthNavigateUri.zai.<JWT>``）。识别必须**合并**各处字段，不能「找到第一个含
    token 的对象就返回」（那样只会拿到 loginInfo、丢掉 deviceId，device_id 被自动重生成
    会导致 refresh 续不上期）。
    """
    mod = _module()
    payload = json.dumps({
        "autoclaw.web.userId": "1487730d62f74187a181e8ad9859febd",
        "autoclaw.web.oauthResult": json.dumps(
            {"ok": True, "provider": "google", "msg": "登录成功", "id": "e8aa5fd1"}),
        "autoclaw.web.runtimeSandboxId": "sb_23a6f18afe234c42b14c3be857498295",
        "autoclaw.web.refreshToken": "Bearer rt-FLAT",
        "autoclaw.web.oauthNavigateUri.zai.eyJ2IjoxLCJwIjoiemFpIn0.abc":
            json.dumps({"navigateUri": "https://autoclaw.z.ai/web/?webOAuthCallback=zai"}),
        "autoclaw.web.authToken": "Bearer auth-WEB",
        "autoclaw.web.loginInfo": json.dumps({
            "sub_id": "108", "user_id": "14877", "user_name": "wu xin",
            "access_token": "Bearer at-LOGIN", "refresh_token": "Bearer rt-LOGIN"}),
        "autoclaw.web.userInfo": json.dumps({
            "id": 147417, "user_phone": "", "user_name": "wu xin",
            "email": "wuxin1903@gmail.com", "config": {"vm_init": False}}),
        "autoclaw.web.deviceId": "646d6b4c-96a4-4138-bd5b-3ba159fc836a",
        "autoclaw.web.riskAgreementRead": "1",
    })
    doc = mod._credential_doc_from_payload(payload)
    # access token 取自 loginInfo（STRICT 档）；authToken 是网页会话 token，不抢首选
    assert doc["accessToken"] == "Bearer at-LOGIN"
    # refreshToken/deviceId/userId 取自顶层平铺键（先到先得，平铺键是实时状态）
    assert doc["refreshToken"] == "Bearer rt-FLAT"
    assert doc["deviceId"] == "646d6b4c-96a4-4138-bd5b-3ba159fc836a"
    assert doc["userId"] == "1487730d62f74187a181e8ad9859febd"
    assert doc["userName"] == "wu xin"
    # runtimeSandboxId 刻意不映射成 sandboxId（运行时沙箱 ≠ 持久沙箱，拼错 proxy base 全挂）
    assert "sandboxId" not in doc
    # 未识别的键（riskAgreementRead 等）不进凭证 doc
    assert "riskAgreementRead" not in doc

    # 端到端：合并出的 doc 真能建号，且 deviceId 用的是 dump 里的（refresh 才续得上期）
    p = _make_provider(channel_cls, password="")
    assert mod._apply_credential_doc(p, doc) is True
    assert p.password == "Bearer at-LOGIN"
    assert p.refresh_token == "Bearer rt-FLAT"
    assert p.device_id == "646d6b4c-96a4-4138-bd5b-3ba159fc836a"
    assert p.user_name == "wu xin"

    # 包裹引号（控制台字符串回显形态 '{"..."}'）也要认
    assert mod._credential_doc_from_payload("'" + payload + "'") == doc


def test_credential_payload_auth_token_fallback(channel_cls):
    """dump 里没有 loginInfo、只有 authToken → 兜底档补位，不至于建不出号。"""
    mod = _module()
    doc = mod._credential_doc_from_payload(json.dumps({
        "autoclaw.web.authToken": "Bearer auth-ONLY",
        "autoclaw.web.deviceId": "dev-2",
    }))
    assert doc["accessToken"] == "Bearer auth-ONLY"
    assert doc["deviceId"] == "dev-2"



def test_credential_payload_from_js_object_literals():
    """控制台直接复制对象（单引号/无引号键）不是合法 JSON → JS 字面量兜底。"""
    mod = _module()
    doc = mod._credential_doc_from_payload(
        "{accessToken: 'Bearer at-js', refreshToken: \"rt-js\", deviceId: 'dev-js'}")
    assert doc["accessToken"] == "Bearer at-js"
    assert doc["refreshToken"] == "rt-js"
    # 带命名空间前缀的键名（控制台展开 localStorage 时常见）同样归一
    doc = mod._credential_doc_from_payload(
        "{autoclaw.web.accessToken: 'Bearer at-ns', autoclaw.web.deviceId: 'dev-ns'}")
    assert doc["accessToken"] == "Bearer at-ns"
    assert doc["deviceId"] == "dev-ns"


def test_credential_payload_bare_token():
    mod = _module()
    token = "eyJhbGciOiJIUzI1NiJ9" + "x" * 40
    assert mod._credential_doc_from_payload(token)["accessToken"] == token
    assert mod._credential_doc_from_payload(f"Bearer {token}")["accessToken"] == f"Bearer {token}"


def test_credential_payload_never_mistakes_callback_url_for_token():
    """回调 URL / 短文本不能被当成裸 token——否则会把垃圾写进 access_token。"""
    mod = _module()
    for raw in (
        "http://127.0.0.1:8001/oauth/callback?code=abc&secret=s1",
        "https://autoglm-api.autoglm.ai/userapi/oauth/google/callback?state=st&code=4/0AY",
        "123456",           # 短信验证码
        "", "   ", None,
        "not a url",
    ):
        assert mod._credential_doc_from_payload(raw) == {}, raw


def test_loopback_credential_paste_creates_account(channel_cls):
    """海外区主路径：粘凭证 JSON（params 为空，原文走 callback_url）→ 自动建号。"""
    p = _ready_provider(channel_cls)
    p.region = "global"
    raw = json.dumps({
        "accessToken": "Bearer at-gl", "refreshToken": "rt-gl",
        "deviceId": "dev-gl", "userId": "u-gl", "userName": "g@example.com",
        "region": "global",
    })
    outcome = _run(channel_cls.handle_loopback_callback(
        p, {}, {"login": "credential", "region": "global"}, raw))

    assert outcome["status"] == "authorized"
    account = outcome["account_data"]
    assert account["password"] == "Bearer at-gl"
    assert account["refresh_token"] == "rt-gl"
    assert account["device_id"] == "dev-gl"
    assert account["user_id"] == "u-gl"
    assert account["region"] == "global"


def test_loopback_credential_paste_without_session_marker_still_claimed(channel_cls):
    """凭证 JSON 的 base64 '=' 会被 query 解析切碎 → params 空也必须能认（靠原文）。"""
    p = _ready_provider(channel_cls)
    raw = json.dumps({"accessToken": "Bearer at-x", "refreshToken": "rt-x"})
    outcome = _run(channel_cls.handle_loopback_callback(p, {}, {}, raw))
    assert outcome["status"] == "authorized"
    assert outcome["account_data"]["password"] == "Bearer at-x"


def test_loopback_credential_session_rejects_unrecognized_paste(channel_cls):
    """等凭证的会话粘了别的东西 → 明确报错终止，别让前端一直 pending。"""
    p = _ready_provider(channel_cls)
    outcome = _run(channel_cls.handle_loopback_callback(
        p, {}, {"login": "credential"}, "just some random text"))
    assert outcome["status"] == "error"
    assert "凭证" in outcome["error"]


def test_loopback_sms_accepts_bare_code(channel_cls):
    """前端占位符提示「输入 6 位验证码」→ 用户直接敲 123456，必须认。"""
    p = _ready_provider(channel_cls)
    seen = {}

    async def fake_sms_login(pp, phone, code):
        seen["phone"], seen["code"] = phone, code
        return {"password": "Bearer at-1", "refresh_token": "rt-1", "user_id": "u1",
                "expires_at": "1", "device_id": "dev-1", "region": "cn"}

    _patch_namespace(channel_cls, _sms_login=fake_sms_login)
    outcome = _run(channel_cls.handle_loopback_callback(
        p, {}, {"login": "sms", "phone": "13800001234"}, "654321"))
    assert outcome["status"] == "authorized"
    assert seen == {"phone": "13800001234", "code": "654321"}
