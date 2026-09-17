import asyncio
from types import SimpleNamespace

import aiohttp
import pytest
from fastapi import HTTPException

import providers.proxy_manager as pm
from channel import Channel
from providers.base import BaseProvider
from providers.custom import CustomProvider


class DummyProvider(BaseProvider):
    PROVIDER_NAME = "dummy"
    BASE_URL = "https://example.com"

    async def init_auth(self, is_check: bool = False) -> bool:
        return True

    async def check_auth(self) -> bool:
        return True

    async def fetch_upstream_model_list(self, retry=0) -> list[dict]:
        return []

    async def _do_stream_chat(self, model_id: str, messages: list[dict], **kwargs):
        if False:
            yield {}

    async def _do_non_stream_chat(self, model_id: str, messages: list[dict], **kwargs) -> dict:
        return {}


class FakeReqCtx:
    """模拟 aiohttp session.request(...) 返回的请求上下文（async with 语义）。"""

    def __init__(self, response):
        self._response = response

    async def __aenter__(self):
        return self._response

    async def __aexit__(self, exc_type, exc, tb):
        return False


class FakeResponse:
    def __init__(self):
        self.status = 200
        self.headers = {}
        self.charset = "utf-8"
        self.cookies = {}  # 收口后 _AiohttpOutboundResponse 会透传 resp.cookies
        self.content = SimpleNamespace(iter_any=self._iter_any)

    async def _iter_any(self):
        if False:
            yield b""


class FakeSession:
    """模拟真实 aiohttp.ClientSession：记录构造时的 timeout 与每次 request 的 timeout。"""

    def __init__(self, **kwargs):
        self.closed = False
        self.init_timeout = kwargs.get("timeout")
        self.request_calls = []

    def request(self, method, url, **kwargs):
        self.request_calls.append((method, url, kwargs))
        return FakeReqCtx(FakeResponse())

    async def close(self):
        self.closed = True


class FakeClientSessionFactory:
    """替换 proxy_manager 里真正建 session 的 aiohttp.ClientSession。"""

    def __init__(self):
        self.sessions: list[FakeSession] = []

    def __call__(self, *args, **kwargs):
        session = FakeSession(**kwargs)
        self.sessions.append(session)
        return session


def _install_fake_pm(monkeypatch) -> FakeClientSessionFactory:
    """收口后出站真实 session 建在 providers.proxy_manager。patch 该 seam + 用全新
    ProxyManager 单例，隔离测试间状态。返回工厂以便断言。"""
    factory = FakeClientSessionFactory()
    # 真实 aiohttp session 在 proxy_manager._get_session 里用 aiohttp.ClientSession 构造。
    monkeypatch.setattr(pm.aiohttp, "ClientSession", factory)
    # connector 不需要真的建（工厂忽略 connector kwarg，但仍会调用它）。
    monkeypatch.setattr(pm, "make_insecure_connector", lambda *a, **k: None)
    # 用全新单例，避免此前测试在共享池里留下的实例/session 干扰。
    monkeypatch.setattr(pm, "_shared_manager", None)
    return factory


async def _collect_events(provider: BaseProvider):
    events = []
    async for event in provider.send_sse_request(
        "POST",
        "https://example.com/stream",
        {"accept": "text/event-stream"},
        data="{}",
    ):
        events.append(event)
    return events


def test_channel_request_timeout_defaults_to_120_seconds():
    channel = Channel("custom", {})
    provider = CustomProvider("u", "p", provider_name="custom")
    provider.attach_channel(channel)

    assert channel.timeout_seconds == 120
    assert provider.timeout_seconds == 120
    timeout = provider._stream_request_timeout()
    assert timeout is not None
    assert timeout.total is None
    assert timeout.sock_read == 120


def test_send_sse_request_applies_stream_timeout(monkeypatch):
    """provider 配了 timeout=45：该值必须作为 per-request timeout 传到真实
    session.request（aiohttp 以 per-request timeout 优先，故实际读超时=45s）。"""
    provider = DummyProvider("u", "p", timeout=45)
    factory = _install_fake_pm(monkeypatch)

    asyncio.run(_collect_events(provider))

    assert factory.sessions, "expected a real client session to be created in proxy_manager"
    session = factory.sessions[0]
    assert session.request_calls, "expected session.request to be called"
    method, url, request_kwargs = session.request_calls[0]
    assert method == "POST"
    assert url == "https://example.com/stream"
    # per-request timeout 承载 provider 的 45s 读超时，且不设 total 上限。
    req_timeout = request_kwargs["timeout"]
    assert req_timeout is not None
    assert req_timeout.total is None
    assert req_timeout.sock_read == 45


def test_send_sse_request_without_timeout(monkeypatch):
    """provider 未配 timeout：per-request timeout 由 facade 兜底成 ClientTimeout(total=None)
    —— 无 total 上限、无 sock_read，与旧行为（None）语义等价（都不限时）。"""
    provider = DummyProvider("u", "p")
    factory = _install_fake_pm(monkeypatch)

    asyncio.run(_collect_events(provider))

    assert factory.sessions, "expected a real client session to be created in proxy_manager"
    session = factory.sessions[0]
    _, _, request_kwargs = session.request_calls[0]
    req_timeout = request_kwargs["timeout"]
    # 收口后 facade 把 None 兜底成 ClientTimeout(total=None)：无 total、无 sock_read。
    assert req_timeout is not None
    assert req_timeout.total is None
    assert req_timeout.sock_read is None


# ---------------------------------------------------------------------------
# 停滞看门狗：sock_read 只约束「两次字节」的间隔，上游持续发 SSE 注释就能无限刷新它。
# 看门狗约束的是「两次有效事件」的间隔，keep-alive 刷不掉。
# ---------------------------------------------------------------------------


def _install_stalling_pm(monkeypatch, chunks, delay_after=None, delay=0.0):
    """让 send_sse_request 收到给定分片；delay_after 指定在第 N 片后挂起（模拟上游停发）。"""

    class StallingResponse(FakeResponse):
        async def _iter_any(self):
            for idx, chunk in enumerate(chunks):
                yield chunk
                if delay_after is not None and idx == delay_after:
                    await asyncio.sleep(delay)

    class StallingSession(FakeSession):
        def request(self, method, url, **kwargs):
            self.request_calls.append((method, url, kwargs))
            return FakeReqCtx(StallingResponse())

    class Factory:
        def __init__(self):
            self.sessions = []

        def __call__(self, *args, **kwargs):
            s = StallingSession(**kwargs)
            self.sessions.append(s)
            return s

    factory = Factory()
    monkeypatch.setattr(pm.aiohttp, "ClientSession", factory)
    monkeypatch.setattr(pm, "make_insecure_connector", lambda *a, **k: None)
    monkeypatch.setattr(pm, "_shared_manager", None)
    return factory


def test_stream_stall_timeout_defaults(monkeypatch):
    """停滞上限：配了 timeout 取 max(2×, 120)；未配兜底 300；env 可覆盖/关闭。"""
    monkeypatch.delenv("STREAM_STALL_TIMEOUT", raising=False)
    assert DummyProvider("u", "p", timeout=45)._stream_stall_timeout() == 120.0
    assert DummyProvider("u", "p", timeout=600)._stream_stall_timeout() == 1200.0
    assert DummyProvider("u", "p")._stream_stall_timeout() == 300.0
    monkeypatch.setenv("STREAM_STALL_TIMEOUT", "0")
    assert DummyProvider("u", "p", timeout=45)._stream_stall_timeout() == 0.0
    monkeypatch.setenv("STREAM_STALL_TIMEOUT", "7")
    assert DummyProvider("u", "p", timeout=45)._stream_stall_timeout() == 7.0
    # 非法值回落默认，不因环境变量写错而关掉保护
    monkeypatch.setenv("STREAM_STALL_TIMEOUT", "abc")
    assert DummyProvider("u", "p", timeout=45)._stream_stall_timeout() == 120.0


def test_stall_watchdog_raises_when_upstream_goes_silent(monkeypatch):
    """上游发完一片就停发：看门狗必须在 stall 上限内抛 504，而不是无限干等。"""
    monkeypatch.setenv("STREAM_STALL_TIMEOUT", "0.05")
    provider = DummyProvider("u", "p", timeout=45)
    _install_stalling_pm(
        monkeypatch,
        [b'data: {"choices":[{"delta":{"content":"hi"}}]}\n\n'],
        delay_after=0,
        delay=5.0,
    )

    with pytest.raises(HTTPException) as excinfo:
        asyncio.run(_collect_events(provider))

    assert excinfo.value.status_code == 504
    assert "stalled" in str(excinfo.value.detail)


def test_stall_watchdog_ignores_keepalive_comments(monkeypatch):
    """上游只发 SSE 注释（keep-alive）也必须判停滞：注释不算「有进展」。

    这正是 sock_read 挡不住的场景——注释持续到达会一直刷新 sock_read。
    """
    monkeypatch.setenv("STREAM_STALL_TIMEOUT", "0.05")
    provider = DummyProvider("u", "p", timeout=45)
    _install_stalling_pm(
        monkeypatch,
        [b": ping\n\n", b": ping\n\n"],
        delay_after=1,
        delay=5.0,
    )

    with pytest.raises(HTTPException) as excinfo:
        asyncio.run(_collect_events(provider))

    assert excinfo.value.status_code == 504


def test_stall_watchdog_allows_slow_but_progressing_stream(monkeypatch):
    """正常慢流（事件持续到达、每片都远短于上限）不能被误杀。"""
    monkeypatch.setenv("STREAM_STALL_TIMEOUT", "1.0")
    provider = DummyProvider("u", "p", timeout=45)
    _install_stalling_pm(
        monkeypatch,
        [
            b'data: {"choices":[{"delta":{"content":"a"}}]}\n\n',
            b'data: {"choices":[{"delta":{"content":"b"}}]}\n\n',
        ],
        delay_after=None,
    )

    events = asyncio.run(_collect_events(provider))

    assert len(events) == 2


def test_stall_watchdog_disabled_by_zero(monkeypatch):
    """STREAM_STALL_TIMEOUT=0 关闭看门狗：与旧行为一致，静默流不再被中止。"""
    monkeypatch.setenv("STREAM_STALL_TIMEOUT", "0")
    provider = DummyProvider("u", "p", timeout=45)
    _install_stalling_pm(
        monkeypatch,
        [b'data: {"choices":[{"delta":{"content":"hi"}}]}\n\n'],
        delay_after=0,
        delay=0.2,
    )

    events = asyncio.run(_collect_events(provider))

    assert len(events) == 1
