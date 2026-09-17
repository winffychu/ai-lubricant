"""AutoClaw 无状态推理代理 spec（specs/autoclaw_proxy.py）的翻译契约。

spec 移植自 autoclawpi（https://github.com/hirotomasato/autoclawpi，Go 自托管代理）——
本测试锁住移植层的行为契约，防止后续改框架钩子模型时悄悄破坏：
1. spec 能被 code_loader 加载（普通类 + 可识别钩子）；
2. 类级开关（token 自动续期 / 定时刷新 / 关伪装头 / 渠道地址可空）；
3. 模型路由对齐 Go RouteID / BodyModel（zai_* / zaicoding_* / tdpsk_* / 去前缀）；
4. 签名头对齐 Go sign.Headers（md5(APP_ID & ts & APP_KEY)）；
5. 推理头对齐 Go InferenceHeader（X-Authorization + X-Request-Model + X-Harness-Type）；
6. 错误分类（410000/unauthorized → 401、402/余额不足 → 402、429 透传）；
7. 凭证解析（JSON 全文 / 裸 token，camelCase/snake_case 双认）；
8. 签到任务清单对齐 Go checkinTasks；
9. 流式聊天出站形态（URL / 头 / body.model 去前缀 / SSE 统一帧）。

specs/ 在 .gitignore 内、由使用者自持，文件不存在时整个模块跳过。
"""
from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path

import pytest

from providers.code_loader import load_code_provider_class, invalidate_cache

_SPEC_PATH = Path(__file__).resolve().parent.parent / "specs" / "autoclaw_proxy.py"

if not _SPEC_PATH.exists():
    pytest.skip("specs/autoclaw_proxy.py 不存在（使用者自持）", allow_module_level=True)

_SOURCE = _SPEC_PATH.read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def channel_cls():
    cls = load_code_provider_class("autoclaw-proxy-test", _SOURCE)
    yield cls
    invalidate_cache("autoclaw-proxy-test")


def _make_provider(channel_cls, *, password: str = "", **extra):
    """造一个池外实例（不挂渠道），需要的字段全部 kwargs 注入。"""
    return channel_cls(username="test", password=password, _api_key=password, **extra)


def _module():
    """加载 spec 模块本身（拿模块级 helper；与 code_loader 的 exec 命名空间同一套定义）。"""
    import importlib.util
    spec = importlib.util.spec_from_file_location("autoclaw_proxy_spec_module", _SPEC_PATH)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _run(coro):
    return asyncio.run(coro)


# ==================== 加载与声明 ====================

def test_spec_loads_with_expected_hooks(channel_cls):
    hooks = channel_cls._spec_hooks
    assert {
        "account_schema", "is_init", "init_auth", "check_auth", "health_check",
        "refresh_auth", "fetch_models", "stream_chat",
        "begin_device_flow", "poll_device_flow", "handle_loopback_callback",
    }.issubset(hooks)


def test_class_flags(channel_cls):
    assert channel_cls.SUPPORTS_MULTI_MESSAGES is True
    assert channel_cls.SUPPORTS_TOKEN_AUTO_REFRESH is True
    assert channel_cls.SCHEDULED_REFRESH is True
    # 推理网关认自己的 X-Authorization / X-Harness-Type 头，必须关掉伪装头。
    assert channel_cls.APPLY_CLIENT_PRESET is False
    # userapi 基址按 region 官方域名兜底，渠道地址可留空。
    assert channel_cls.REQUIRES_BASE_URL is False


def test_account_schema_declares_device_code_loopback(channel_cls):
    schema = channel_cls.account_schema()
    assert "device_code" in schema["add_methods"]
    assert schema["auth_start"]["mode"] == "device_code"
    assert schema["auth_start"]["completion"] == "loopback"
    keys = {f["key"] for f in schema["fields"]}
    assert {"password", "refresh_token", "region", "phone", "device_id", "quota_text"} <= keys
    # 无沙箱：不该再声明 sandbox_* 字段
    assert not any(k.startswith("sandbox") for k in keys)


def test_account_fields_cover_runtime_state(channel_cls):
    fields = set(channel_cls.ACCOUNT_FIELDS)
    assert {"refresh_token", "expires_at", "device_id", "region", "quota_text"} <= fields


def test_region_is_first_field_and_select(channel_cls):
    """region 必须是首个字段的下拉框：决定授权方式与 userapi 域名。"""
    fields = channel_cls.account_schema()["fields"]
    assert fields[0]["key"] == "region"
    assert fields[0]["type"] == "select"
    assert fields[0]["default_value"] == "cn"
    assert {opt["value"] for opt in fields[0]["options"]} == {"cn", "global"}
    assert all(opt.get("label") for opt in fields[0]["options"])


# ==================== 模型路由（对齐 Go RouteID / BodyModel）===================

def test_route_id_vectors():
    """RouteID 向量：无下划线才映射；已含下划线视为 route id 原样透传。"""
    mod = _module()
    cases = [
        ("auto", "zai_auto"),
        ("auto-fast", "zai_auto-fast"),
        ("glm-5-turbo", "zai_glm-5-turbo"),
        ("glm-5.3-flash", "zai_glm-5.3-flash"),
        ("glm-5.3", "zaicoding_glm-5.3"),
        ("glm-5.2", "zaicoding_glm-5.2"),
        ("deepseek-v4-pro", "tdpsk_deepseek-v4-pro-202606"),
        ("deepseek-v4-flash", "tdpsk_deepseek-v4-flash-202605"),
        # 已路由：原样
        ("zai_auto", "zai_auto"),
        ("zaicoding_glm-5.3", "zaicoding_glm-5.3"),
        ("tdpsk_deepseek-v4-pro-202606", "tdpsk_deepseek-v4-pro-202606"),
    ]
    for raw, expected in cases:
        assert mod._route_id(raw) == expected, raw


def test_body_model_strips_first_prefix():
    """BodyModel：去第一个下划线前缀（zai_auto → auto）。"""
    mod = _module()
    assert mod._body_model("zai_auto") == "auto"
    assert mod._body_model("zai_glm-5.3-flash") == "glm-5.3-flash"
    assert mod._body_model("zaicoding_glm-5.3") == "glm-5.3"
    assert mod._body_model("tdpsk_deepseek-v4-pro-202606") == "deepseek-v4-pro-202606"
    # 无下划线：原样
    assert mod._body_model("auto") == "auto"


# ==================== 签名头（对齐 Go sign.Headers）===================

def test_sign_headers_match_go_vector():
    """X-Auth-Sign = md5(APP_ID & timestamp & APP_KEY)；APP_ID/密钥对齐 Go 常量。"""
    mod = _module()
    assert mod.APP_ID == "100003"
    assert mod.APP_SECRET == "38d2391985e2369a5fb8227d8e6cd5e5"

    headers = mod._sign_headers("")
    ts = headers["X-Auth-TimeStamp"]
    expected = mod.hashlib.md5(f"100003&{ts}&38d2391985e2369a5fb8227d8e6cd5e5".encode()).hexdigest()
    assert headers["X-Auth-Sign"] == expected
    assert headers["X-Auth-Appid"] == "100003"
    assert headers["X-Product"] == "autoclaw"
    # 未传 token 时不带 Authorization
    assert "Authorization" not in headers
    # WAF 要求浏览器身份
    assert "Mozilla" in headers["User-Agent"]
    assert headers["Origin"] == "https://autoclaw.z.ai"


def test_sign_headers_add_bearer_when_token_present():
    mod = _module()
    headers = mod._sign_headers("tok-1")
    assert headers["Authorization"] == "Bearer tok-1"
    # 幂等：已带前缀不重复
    assert mod._sign_headers("Bearer tok-1")["Authorization"] == "Bearer tok-1"


# ==================== 推理头（对齐 Go InferenceHeader）===================

def test_inference_headers_match_go_shape(channel_cls):
    mod = _module()
    p = _make_provider(channel_cls, password="tok-1")
    headers = mod._inference_headers(p, "zai_glm-5.3-flash")
    assert headers["X-Authorization"] == "Bearer tok-1"
    assert headers["X-Request-Model"] == "zai_glm-5.3-flash"
    assert headers["X-Product"] == "autoclaw"
    assert headers["X-Harness-Type"] == "zcode"
    assert headers["X-Tm"] == "linux"
    # 每次调用新 UUID（追踪头）
    assert headers["X-Request-Id"] != headers["x_trace_id"]
    # 不带 userapi 的签名头
    assert "X-Auth-Sign" not in headers


# ==================== 地址解析 ====================

def test_inference_base_derives_from_userapi(channel_cls):
    """推理网关基址 = userapi 基址 + /autoclaw-proxy/proxy/autoclaw，随 region 联动。"""
    mod = _module()
    cn = _make_provider(channel_cls, password="t")            # 默认 cn
    assert mod._userapi_base(cn) == mod.USERAPI_BASE_CN
    assert mod._inference_base(cn) == mod.USERAPI_BASE_CN + "/autoclaw-proxy/proxy/autoclaw"

    gl = _make_provider(channel_cls, password="t", region="global")
    assert mod._userapi_base(gl) == mod.USERAPI_BASE_GLOBAL
    assert mod._inference_base(gl) == mod.USERAPI_BASE_GLOBAL + "/autoclaw-proxy/proxy/autoclaw"


def test_channel_base_url_overrides_userapi(channel_cls):
    """渠道地址填了则覆盖 userapi 基址（自建反代用）。"""
    mod = _module()
    p = _make_provider(channel_cls, password="t")
    p._channel = type("FakeChannel", (), {
        "get": lambda self, k, d=None: "https://my-proxy.example.com" if k == "base_url" else d,
    })()

    assert mod._userapi_base(p) == "https://my-proxy.example.com"
    assert mod._inference_base(p) == "https://my-proxy.example.com/autoclaw-proxy/proxy/autoclaw"


def test_inference_base_can_be_overridden(channel_cls):
    """渠道配置 inference_base 可整段覆盖推理网关地址（上游路径形态实测不符时的逃生口）。"""
    mod = _module()
    p = _make_provider(channel_cls, password="t")
    p._channel = type("FakeChannel", (), {
        "get": lambda self, k, d=None: "https://gw.example.com/dup/proxy/autoclaw" if k == "inference_base" else d,
    })()
    assert mod._inference_base(p) == "https://gw.example.com/dup/proxy/autoclaw"


# ==================== 错误分类 ====================

def test_classify_not_logged_in_is_401():
    mod = _module()
    exc = mod._classify_upstream_error(200, 410000, "Please log in to continue.")
    assert exc.status_code == 401


def test_classify_insufficient_credit_is_402():
    mod = _module()
    for body in ("insufficient credit", "余额不足", "quota exceeded"):
        assert mod._classify_upstream_error(200, 0, body).status_code == 402


def test_classify_rate_limit_passthrough():
    mod = _module()
    assert mod._classify_upstream_error(429, 0, "too many requests").status_code == 429


def test_classify_invalid_token_is_401():
    """推理网关的 {"error":"Invalid token"} → 401（框架冻结账号）。"""
    mod = _module()
    assert mod._classify_upstream_error(401, 0, '{"error":"Invalid token"}').status_code == 401


# ==================== 凭证解析 ====================

def test_credential_json_parses_into_fields(channel_cls):
    mod = _module()
    doc = json.dumps({
        "accessToken": "Bearer at-xyz",
        "refreshToken": "rt-xyz",
        "userId": 12345,
        "userName": "张三",
        "phone": "13800008000",
        "deviceId": "dev-1",
        "region": "global",
    })
    p = _make_provider(channel_cls, password=doc)
    assert mod._apply_credential_text(p, doc) is True
    assert p.password == "Bearer at-xyz"
    assert p.refresh_token == "rt-xyz"
    assert p.user_id == "12345"
    assert p.user_name == "张三"
    assert p.device_id == "dev-1"
    assert p.region == "global"


def test_credential_bare_token(channel_cls):
    mod = _module()
    p = _make_provider(channel_cls, password="")
    # 裸 token 必须长得像 token（base64url、长度≥20）才收：否则误收任意字符串当账号
    assert mod._apply_credential_text(p, "Bearer rawtoken1234567890abcde") is True
    assert p.password == "rawtoken1234567890abcde"          # 裸 token 存去前缀形态
    assert p.device_id                        # 自动补 device_id（refresh 要对得上）
    # 短字符串不算 token
    assert mod._apply_credential_text(_make_provider(channel_cls), "short") is False
    # 带中文/空白的也不算
    assert mod._apply_credential_text(_make_provider(channel_cls), "这不是凭证") is False


def test_credential_json_without_token_is_noop(channel_cls):
    mod = _module()
    p = _make_provider(channel_cls, password="")
    assert mod._apply_credential_text(p, '{"foo": 1}') is False
    assert mod._apply_credential_text(p, "") is False


def test_bearer_helpers_idempotent():
    mod = _module()
    assert mod._bearer_value("Bearer abc") == "Bearer abc"
    assert mod._bearer_value("abc") == "Bearer abc"
    assert mod._strip_bearer("Bearer abc") == "abc"
    assert mod._strip_bearer("abc") == "abc"


# ==================== 签到任务清单 ====================

def test_checkin_tasks_match_go():
    """对齐 checkin.go checkinTasks 的 4 个任务。"""
    mod = _module()
    assert mod.CHECKIN_TASKS == (
        "daily_signin",
        "daily_inspiration_center",
        "newbie_cloud_lobster",
        "newbie_local_lobster",
    )


def test_task_headers_match_checkin_go(channel_cls):
    """签到/余额用 commonHeaders 一套：小写 authorization、X-Client-Type: pc、X-Lang: en。

    和 userapi 那套（大写 Authorization、web 身份、带 Origin/Referer）**不是同一套**——
    混用会让上游解不出 user_id（实测 task-complete 返回 500 task user_id required）。
    """
    mod = _module()
    p = _make_provider(channel_cls, password="tok-1")
    headers = mod._task_headers(p)
    assert headers["authorization"] == "Bearer tok-1"   # 小写！
    assert "Authorization" not in headers
    assert headers["X-Client-Type"] == "pc"
    assert headers["X-Lang"] == "en"
    assert headers["X-Product"] == "autoclaw"
    assert "Accept" not in headers          # checkin.go 显式 delete(Accept)
    assert "Origin" not in headers          # 不是 web 身份
    assert "Referer" not in headers
    ts = headers["X-Auth-TimeStamp"]
    expected = mod.hashlib.md5(f"100003&{ts}&38d2391985e2369a5fb8227d8e6cd5e5".encode()).hexdigest()
    assert headers["X-Auth-Sign"] == expected


def test_task_json_accepts_bare_envelope(channel_cls):
    """task-complete / wallet 回的是裸 JSON，不一定套 {code,msg,data} 统一信封。"""
    mod = _module()
    p = _make_provider(channel_cls, password="t")

    class _Resp:
        def __init__(self, status, text):
            self.status = status
            self._text = text

        async def text(self):
            return self._text

    class _Ctx:
        def __init__(self, resp):
            self._resp = resp

        async def __aenter__(self):
            return self._resp

        async def __aexit__(self, *a):
            return False

    class _Session:
        def __init__(self, resp):
            self._resp = resp

        def request(self, *a, **k):
            return _Ctx(self._resp)

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

    def _with(status, text):
        p._make_session = lambda *a, **k: _Session(_Resp(status, text))
        return _run(mod._task_json(p, "POST", mod.TASK_COMPLETE_PATH, json_body={"task_id": "x"}))

    # 裸成功体：没有 code 字段 → 整体当 data
    assert _with(200, '{"success":true,"reward_points":400}')["reward_points"] == 400
    # 业务码非 0 → 分类后的 HTTPException（410000 → 401）
    with pytest.raises(mod.HTTPException) as ei:
        _with(200, '{"code":410000,"msg":"Please log in to continue."}')
    assert ei.value.status_code == 401
    # HTTP 500 + 裸 message → 透传为 HTTPException，不静默吞掉
    with pytest.raises(mod.HTTPException) as ei2:
        _with(500, '{"message":"task user_id required"}')
    assert ei2.value.status_code == 500


def test_claim_checkin_tasks_accumulates_rewards(channel_cls):
    """签到：success 记积分、already_completed 记 already、异常任务跳过不中断。"""
    mod = _module()
    p = _make_provider(channel_cls, password="t")
    seen: list[str] = []

    async def fake_task_json(_p, _method, path, *, json_body=None, params=None):
        assert path == mod.TASK_COMPLETE_PATH
        task = json_body["task_id"]
        seen.append(task)
        if task == "daily_signin":
            return {"success": True, "reward_points": 400}
        if task == "daily_inspiration_center":
            return {"already_completed": True}
        if task == "newbie_cloud_lobster":
            raise mod.HTTPException(status_code=502, detail="boom")
        return {"success": True, "reward_points": 500}

    hook = channel_cls._spec_hooks["refresh_auth"]
    g = hook.__globals__
    saved = g["_task_json"]
    g["_task_json"] = fake_task_json
    try:
        result = _run(g["_claim_checkin_tasks"](p))
    finally:
        g["_task_json"] = saved

    assert seen == list(mod.CHECKIN_TASKS)          # 全部任务都尝试过（异常不中断）
    assert result["daily_signin"] == 400
    assert result["daily_inspiration_center"] == "already"
    assert "newbie_cloud_lobster" not in result     # 抛异常的任务不记
    assert result["newbie_local_lobster"] == 500


# ==================== 模型列表 ====================

def test_fetch_models_returns_static_table(channel_cls):
    mod = _module()
    p = _make_provider(channel_cls, password="t")
    models = _run(channel_cls.fetch_upstream_model_list(p))
    ids = [m["id"] for m in models]
    assert ids == [mid for mid, _ in mod.STATIC_MODELS]
    assert "auto" in ids and "glm-5.3-flash" in ids and "deepseek-v4-pro" in ids
    assert all(m["object"] == "model" for m in models)


# ==================== 认证判定 ====================

def test_auth_predicates(channel_cls):
    assert channel_cls.is_init(_make_provider(channel_cls, password="tok")) is True
    assert channel_cls.is_init(_make_provider(channel_cls, password="")) is False
    assert _run(channel_cls.check_auth(_make_provider(channel_cls, password="tok"))) is True
    assert _run(channel_cls.check_auth(_make_provider(channel_cls, password=""))) is False
    # 未解析的凭证 JSON：含 accessToken 也认
    raw = '{"accessToken":"x"}'
    assert _run(channel_cls.check_auth(_make_provider(channel_cls, password=raw))) is True
    assert _run(channel_cls.health_check(_make_provider(channel_cls, password="tok"))) is True


# ==================== 设备码授权 ====================

def test_begin_device_flow_cn_requires_phone(channel_cls):
    """cn 未填手机号 → 400 提示先填（不发短信）。"""
    mod = _module()
    p = _make_provider(channel_cls, password="t", region="cn")
    with pytest.raises(mod.HTTPException) as ei:
        _run(channel_cls.begin_device_flow(p))
    assert ei.value.status_code == 400
    assert "手机号" in str(ei.value.detail)


def test_begin_device_flow_cn_sends_sms(channel_cls):
    """cn + 手机号 → 发短信，补投框收 6 位验证码。"""
    mod = _module()
    p = _make_provider(channel_cls, password="t", region="cn", phone="13800008000")
    sent: dict = {}

    async def fake_userapi_json(_p, method, path, *, json_body=None, params=None):
        sent["path"] = path
        sent["body"] = json_body
        return {}

    hook = channel_cls._spec_hooks["begin_device_flow"]
    saved = hook.__globals__["_userapi_json"]
    hook.__globals__["_userapi_json"] = fake_userapi_json
    try:
        out = _run(channel_cls.begin_device_flow(p))
    finally:
        hook.__globals__["_userapi_json"] = saved

    assert sent["path"] == mod.SEND_CODE_PATH
    assert sent["body"]["phone"] == "13800008000"
    assert sent["body"]["source_id"] == "autoclaw"
    assert out["poll_params"]["login"] == "sms"
    assert out["replay_label"] == "提交验证码"
    assert "auth_url" not in out            # 短信流程没有要打开的网页


def test_begin_device_flow_global_is_credential_paste(channel_cls):
    """global → 不发请求，纯等待态；提示里给控制台 JS。"""
    mod = _module()
    p = _make_provider(channel_cls, password="t", region="global")

    async def boom(*a, **k):
        raise AssertionError("global 授权不应发请求")

    hook = channel_cls._spec_hooks["begin_device_flow"]
    saved = hook.__globals__["_userapi_json"]
    hook.__globals__["_userapi_json"] = boom
    try:
        out = _run(channel_cls.begin_device_flow(p))
    finally:
        hook.__globals__["_userapi_json"] = saved

    assert out["poll_params"]["login"] == "credential"
    assert out["auth_url"].startswith("https://autoclaw.z.ai")
    assert "localStorage" in out["message"]
    assert "prompt(" in out["message"]       # 用 prompt 而非 Chrome 专有 copy()


def test_poll_device_flow_is_always_pending(channel_cls):
    p = _make_provider(channel_cls, password="t")
    assert _run(channel_cls.poll_device_flow(p, {})) == {"status": "pending"}


def test_loopback_credential_paste_authorizes(channel_cls):
    """凭证补投：粘贴 JSON → 拆字段 → authorized。"""
    mod = _module()
    p = _make_provider(channel_cls, password="", region="global")
    pasted = json.dumps({"accessToken": "at-new", "refreshToken": "rt-new", "deviceId": "d1"})
    out = _run(channel_cls.handle_loopback_callback(
        p, {}, {"login": "credential", "region": "global"}, pasted))
    assert out["status"] == "authorized"
    assert out["account_data"]["password"] == "at-new"
    assert out["account_data"]["refresh_token"] == "rt-new"


def test_loopback_credential_paste_garbage_errors(channel_cls):
    """本会话在等凭证，粘了别的东西 → error 终止，别静默 pending。"""
    p = _make_provider(channel_cls, password="", region="global")
    out = _run(channel_cls.handle_loopback_callback(
        p, {}, {"login": "credential"}, "这不是凭证"))
    assert out["status"] == "error"


def test_loopback_sms_bare_code(channel_cls):
    """sms 会话：用户直接敲 123456（没写成 code=123456）也要认。"""
    mod = _module()
    p = _make_provider(channel_cls, password="t", region="cn", phone="13800008000")

    async def fake_userapi_json(_p, method, path, *, json_body=None, params=None):
        assert path == mod.LOGIN_PATH
        assert json_body["code"] == 123456      # 必须是 int（字符串被上游 400001 拒）
        return {"access_token": "at-sms", "refresh_token": "rt-sms", "user_id": "u1"}

    hook = channel_cls._spec_hooks["handle_loopback_callback"]
    saved = hook.__globals__["_userapi_json"]
    hook.__globals__["_userapi_json"] = fake_userapi_json
    try:
        out = _run(channel_cls.handle_loopback_callback(
            p, {}, {"login": "sms", "phone": "13800008000"}, "123456"))
    finally:
        hook.__globals__["_userapi_json"] = saved

    assert out["status"] == "authorized"
    assert out["account_data"]["password"] == "at-sms"
    assert out["account_data"]["phone"] == "13800008000"


def test_loopback_unrelated_returns_none(channel_cls):
    """认不出是本会话的补投 → None（框架试下一个会话）。"""
    p = _make_provider(channel_cls, password="t")
    assert _run(channel_cls.handle_loopback_callback(p, {}, {"login": "sms"}, "not-a-code")) is None


# ==================== token 续期 ====================

def test_refresh_access_token_rotates_and_persists(channel_cls):
    mod = _module()
    p = _make_provider(channel_cls, password="old-at", refresh_token="rt-1")
    persisted: dict = {}
    seen_paths: list[str] = []

    async def fake_userapi_json(_p, method, path, *, json_body=None, params=None):
        seen_paths.append(path)
        assert path == mod.REFRESH_PATH          # 主路径：agent-refresh
        assert json_body["refresh_token"] == "rt-1"
        assert json_body["source_id"] == "autoclaw"
        return {"access_token": "new-at", "refresh_token": "rt-2"}

    async def fake_persist(fields):
        persisted.update(fields)
        return True

    p.persist_account_fields = fake_persist
    hook = channel_cls._spec_hooks["refresh_auth"]
    g = hook.__globals__
    saved = g["_userapi_json"]
    g["_userapi_json"] = fake_userapi_json
    try:
        _run(g["_refresh_access_token"](p))
    finally:
        g["_userapi_json"] = saved

    assert p.password == "new-at"
    assert p.refresh_token == "rt-2"          # 轮换后的 refresh_token 也留存
    assert int(p.expires_at) > time.time()
    assert persisted["password"] == "new-at"
    assert seen_paths == [mod.REFRESH_PATH]   # 主路径成功，不回退


def test_refresh_falls_back_to_legacy_on_endpoint_missing(channel_cls):
    """主路径 404/405/501（端点不存在）→ 回退老 web 端点；业务错误不回退。"""
    mod = _module()
    p = _make_provider(channel_cls, password="old-at", refresh_token="rt-1")
    calls: list[str] = []

    async def fake_userapi_json(_p, method, path, *, json_body=None, params=None):
        calls.append(path)
        if path == mod.REFRESH_PATH:
            raise mod.HTTPException(404, "404 page not found")
        assert path == mod.REFRESH_PATH_LEGACY
        return {"access_token": "new-at", "refresh_token": "rt-2"}

    p.persist_account_fields = lambda fields: asyncio.sleep(0)
    hook = channel_cls._spec_hooks["refresh_auth"]
    g = hook.__globals__
    saved = g["_userapi_json"]
    g["_userapi_json"] = fake_userapi_json
    try:
        _run(g["_refresh_access_token"](p))
    finally:
        g["_userapi_json"] = saved

    assert calls == [mod.REFRESH_PATH, mod.REFRESH_PATH_LEGACY]
    assert p.password == "new-at"


def test_refresh_does_not_fall_back_on_business_error(channel_cls):
    """主路径 400000（token 真死）→ 不回退，直接上抛：两个端点会给出同样答复，回退只会掩盖原因。"""
    mod = _module()
    p = _make_provider(channel_cls, password="old-at", refresh_token="rt-1")
    calls: list[str] = []

    async def fake_userapi_json(_p, method, path, *, json_body=None, params=None):
        calls.append(path)
        raise mod.HTTPException(401, "code=400000 User not logged in")

    p.persist_account_fields = lambda fields: asyncio.sleep(0)
    hook = channel_cls._spec_hooks["refresh_auth"]
    g = hook.__globals__
    saved = g["_userapi_json"]
    g["_userapi_json"] = fake_userapi_json
    try:
        with pytest.raises(mod.HTTPException) as ei:
            _run(g["_refresh_access_token"](p))
    finally:
        g["_userapi_json"] = saved

    assert ei.value.status_code == 401
    assert calls == [mod.REFRESH_PATH]          # 没打老端点


def test_ensure_access_token_skips_when_fresh(channel_cls):
    """未临期 + 有 refresh_token → 不刷新。"""
    mod = _module()
    p = _make_provider(channel_cls, password="at", refresh_token="rt")
    p.expires_at = str(int(time.time()) + 86400)

    async def boom(*a, **k):
        raise AssertionError("未临期不该刷新")

    hook = channel_cls._spec_hooks["refresh_auth"]
    saved = hook.__globals__["_refresh_access_token"]
    hook.__globals__["_refresh_access_token"] = boom
    try:
        _run(mod._ensure_access_token(p))
    finally:
        hook.__globals__["_refresh_access_token"] = saved


def test_ensure_access_token_without_refresh_token_is_noop(channel_cls):
    """纯 token 手动建号：无 refresh_token 就不刷（失效由真实请求报 401）。"""
    mod = _module()
    p = _make_provider(channel_cls, password="at")
    _run(mod._ensure_access_token(p))          # 不抛即通过


def test_refresh_auth_returns_fields_best_effort(channel_cls):
    """refresh_auth：任一步失败不阻断其余，返回可窄写的字段快照。"""
    mod = _module()
    p = _make_provider(channel_cls, password="at", refresh_token="rt", region="cn")
    p.persist_account_fields = lambda fields: asyncio.sleep(0)

    async def fake_ensure(_p):
        return None

    async def fake_points(_p):
        return 12345

    async def fake_checkin(_p):
        return {"daily_signin": 400}

    hook = channel_cls._spec_hooks["refresh_auth"]
    g = hook.__globals__
    saved = (g["_ensure_access_token"], g["_fetch_points"], g["_claim_checkin_tasks"])
    g["_ensure_access_token"] = fake_ensure
    g["_fetch_points"] = fake_points
    g["_claim_checkin_tasks"] = fake_checkin
    try:
        fields = _run(hook(p, {"password": "at", "refresh_token": "rt"}))
    finally:
        g["_ensure_access_token"], g["_fetch_points"], g["_claim_checkin_tasks"] = saved

    assert fields["quota_text"] == "积分 12,345"
    assert fields["password"] == "at"


def test_refresh_auth_survives_all_failures(channel_cls):
    """三件套全炸也要返回快照，不把异常抛给 admin。"""
    mod = _module()
    p = _make_provider(channel_cls, password="at", refresh_token="rt")

    async def boom(*a, **k):
        raise RuntimeError("network down")

    hook = channel_cls._spec_hooks["refresh_auth"]
    g = hook.__globals__
    saved = (g["_ensure_access_token"], g["_fetch_points"], g["_claim_checkin_tasks"])
    g["_ensure_access_token"] = boom
    g["_fetch_points"] = boom
    g["_claim_checkin_tasks"] = boom
    try:
        fields = _run(hook(p, {"password": "at", "refresh_token": "rt"}))
    finally:
        g["_ensure_access_token"], g["_fetch_points"], g["_claim_checkin_tasks"] = saved

    assert fields["password"] == "at"
    assert "quota_text" not in fields


# ==================== 聊天出站形态 ====================

class _FakeSSE:
    """假 send_sse_request：记录出站形态，回放预设帧。"""

    def __init__(self, frames):
        self.frames = frames
        self.calls: list[dict] = []

    def __call__(self, method, url, headers, **kwargs):
        self.calls.append({"method": method, "url": url, "headers": headers, "kwargs": kwargs})
        frames = self.frames

        async def _gen():
            for frame in frames:
                yield frame

        return _gen()


def _sse_chunk(content: str = "", finish: str | None = None) -> str:
    delta = {"content": content} if content else {}
    choice = {"index": 0, "delta": delta}
    if finish:
        choice["finish_reason"] = finish
    payload = {"id": "chatcmpl-1", "object": "chat.completion.chunk",
               "created": int(time.time()), "model": "auto", "choices": [choice]}
    return f"data: {json.dumps(payload)}\n\n"


def _run_stream(channel_cls, p, model_id, messages, fake, **kwargs):
    """跑 stream_chat 钩子并把出站 send_sse_request 换成假的。"""
    p.send_sse_request = fake
    hook = channel_cls._spec_hooks["stream_chat"]

    async def _collect():
        return [frame async for frame in hook(p, model_id, messages, **kwargs)]

    return asyncio.run(_collect())


def test_stream_chat_outbound_shape(channel_cls):
    """出站：URL 打推理网关、头带 X-Request-Model、body.model 去前缀。"""
    mod = _module()
    p = _make_provider(channel_cls, password="tok-1", region="global")
    fake = _FakeSSE([_sse_chunk("hello"), _sse_chunk("", finish="stop")])

    frames = _run_stream(channel_cls, p, "glm-5.3-flash", [{"role": "user", "content": "hi"}], fake)

    assert len(fake.calls) == 1
    call = fake.calls[0]
    assert call["method"] == "POST"
    assert call["url"] == mod.USERAPI_BASE_GLOBAL + "/autoclaw-proxy/proxy/autoclaw/v1/chat/completions"
    assert call["headers"]["X-Request-Model"] == "zai_glm-5.3-flash"
    assert call["headers"]["X-Authorization"] == "Bearer tok-1"
    body = call["kwargs"]["json"]
    assert body["model"] == "glm-5.3-flash"      # route id 去前缀
    assert body["stream"] is True
    # 首帧空字典（通知上游已接通）+ 两帧内容
    assert frames[0] == {}
    assert any(f.get("content") == "hello" for f in frames)


def test_stream_chat_deepseek_route(channel_cls):
    """deepseek 模型走 tdpsk_ 前缀 route，body.model 带日期后缀。"""
    mod = _module()
    p = _make_provider(channel_cls, password="tok-1", region="cn")
    fake = _FakeSSE([_sse_chunk("ok", finish="stop")])

    _run_stream(channel_cls, p, "deepseek-v4-pro", [{"role": "user", "content": "hi"}], fake)

    call = fake.calls[0]
    assert call["headers"]["X-Request-Model"] == "tdpsk_deepseek-v4-pro-202606"
    assert call["kwargs"]["json"]["model"] == "deepseek-v4-pro-202606"
    assert call["url"].startswith(mod.USERAPI_BASE_CN)


def test_stream_chat_requires_token(channel_cls):
    """无 access_token → 400，不打上游。"""
    mod = _module()
    p = _make_provider(channel_cls, password="")
    fake = _FakeSSE([])
    with pytest.raises(mod.HTTPException) as ei:
        _run_stream(channel_cls, p, "auto", [{"role": "user", "content": "hi"}], fake)
    assert ei.value.status_code == 400
    assert not fake.calls


def test_stream_chat_propagates_upstream_error_frame(channel_cls):
    """上游 error 帧 → HTTPException（框架冻结/换号接管）。"""
    mod = _module()
    p = _make_provider(channel_cls, password="tok-1")
    err = 'data: {"error":{"message":"Invalid token","code":401}}\n\n'
    fake = _FakeSSE([err])
    with pytest.raises(mod.HTTPException):
        _run_stream(channel_cls, p, "auto", [{"role": "user", "content": "hi"}], fake)


def test_stream_chat_skips_keepalive_comments(channel_cls):
    """纯注释帧（keep-alive）不产内容帧。"""
    p = _make_provider(channel_cls, password="tok-1")
    fake = _FakeSSE([": ping\n\n", _sse_chunk("hi", finish="stop")])
    frames = _run_stream(channel_cls, p, "auto", [{"role": "user", "content": "x"}], fake)
    assert frames[0] == {}
    assert [f for f in frames[1:] if f.get("content")] == [{"content": "hi", "thinking": "",
                                                            "tool_calls": [], "usage": None,
                                                            "finish_reason": "stop", "done": True}]


def test_stream_chat_handles_full_json_response(channel_cls):
    """上游回非 SSE 的完整 chat.completion JSON → 拆单帧收尾。"""
    p = _make_provider(channel_cls, password="tok-1")
    full = json.dumps({
        "id": "chatcmpl-2", "object": "chat.completion", "model": "auto",
        "choices": [{"index": 0, "message": {"role": "assistant", "content": "完整回复"},
                     "finish_reason": "stop"}],
        "usage": {"prompt_tokens": 3, "completion_tokens": 5, "total_tokens": 8},
    })
    fake = _FakeSSE([full])
    frames = _run_stream(channel_cls, p, "auto", [{"role": "user", "content": "x"}], fake)
    tail = frames[-1]
    assert tail["content"] == "完整回复"
    assert tail["done"] is True
    assert tail["usage"]["total_tokens"] == 8


def test_stream_chat_strips_forbidden_waf_prefix(channel_cls):
    """WAF 前缀 {"message":"forbidden"} 混在帧里时仍能解析出内容。"""
    p = _make_provider(channel_cls, password="tok-1")
    waf = '{"message":"forbidden"}' + _sse_chunk("after-waf", finish="stop")
    fake = _FakeSSE([waf])
    frames = _run_stream(channel_cls, p, "auto", [{"role": "user", "content": "x"}], fake)
    assert any(f.get("content") == "after-waf" for f in frames)
