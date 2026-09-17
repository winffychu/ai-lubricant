"""前缀代理（url_prefix 模式）与 network 模式解析、出站 URL 改写的单元测试。"""
import aiohttp
import pytest

import proxy_utils
from proxy_utils import (
    canonical_url_prefix,
    proxy_mode,
    proxy_prefix_base,
    proxy_effective_url,
    resolve_account_proxy,
    resolve_account_url_prefix,
    runtime_accounts,
    split_proxy_credentials,
)
from providers.base import apply_url_prefix
from providers.custom import CustomProvider


# ──────────────── canonical_url_prefix ────────────────

def test_canonical_url_prefix_strips_and_trims_trailing_slash():
    assert canonical_url_prefix("  https://relay.example/p/  ") == "https://relay.example/p"
    assert canonical_url_prefix("https://relay.example/") == "https://relay.example"
    assert canonical_url_prefix("") is None
    assert canonical_url_prefix(None) is None


# ──────────────── proxy_mode / proxy_prefix_base ────────────────

def test_proxy_mode_defaults_to_network_for_missing_or_invalid():
    assert proxy_mode(None) == "network"
    assert proxy_mode({}) == "network"
    assert proxy_mode({"mode": "bogus"}) == "network"
    assert proxy_mode({"mode": "url_prefix"}) == "url_prefix"
    assert proxy_mode({"mode": "direct"}) == "direct"
    assert proxy_mode({"mode": "direct"}) == "direct"


def test_proxy_prefix_base_only_for_url_prefix_mode():
    network = {"mode": "network", "url": "http://127.0.0.1:7890"}
    prefix = {"mode": "url_prefix", "url": "https://relay.example/p/"}
    assert proxy_prefix_base(network) is None
    assert proxy_prefix_base(prefix) == "https://relay.example/p"


def test_proxy_effective_url_is_none_for_url_prefix_mode():
    prefix = {"mode": "url_prefix", "url": "https://relay.example/p"}
    # 前缀模式不注入 userinfo、不走 aiohttp proxy=
    assert proxy_effective_url(prefix) is None


def test_proxy_effective_url_is_none_for_direct_mode():
    # direct 模式强制直连：即便条目残留 url/认证，也不产生 aiohttp proxy=
    direct = {"mode": "direct", "url": "http://127.0.0.1:7890", "username": "u", "password": "p"}
    assert proxy_effective_url(direct) is None
    assert proxy_prefix_base(direct) is None


def test_proxy_effective_url_network_injects_credentials():
    network = {"mode": "network", "url": "http://127.0.0.1:7890", "username": "u s", "password": "p@ss"}
    assert proxy_effective_url(network) == "http://u%20s:p%40ss@127.0.0.1:7890"


# ──────────────── split_proxy_credentials ────────────────
#
# 与 proxy_effective_url 互为逆操作：那边把 username/password 拼进 URL，这边把用户
# 粘进来的整行 scheme://user:pass@host:port 拆回结构化字段。前端两份实现
# （user-frontend/src/utils/proxy-url.ts、mobile/src/utils/proxyUrl.ts）覆盖同一批边界。


def test_split_proxy_credentials_extracts_userinfo():
    assert split_proxy_credentials("http://u:p@127.0.0.1:7890") == ("http://127.0.0.1:7890", "u", "p")


def test_split_proxy_credentials_decodes_percent_escapes():
    assert split_proxy_credentials("http://u:p%40ss@h:7890") == ("http://h:7890", "u", "p@ss")


def test_split_proxy_credentials_takes_last_at_so_password_may_contain_at():
    # 密码里的 @ 不切错：userinfo 按最后一个 @ 切分。
    assert split_proxy_credentials("http://u:p@ss@h:7890") == ("http://h:7890", "u", "p@ss")


def test_split_proxy_credentials_username_only():
    assert split_proxy_credentials("http://user@h:7890") == ("http://h:7890", "user", "")


def test_split_proxy_credentials_empty_password_still_counts():
    assert split_proxy_credentials("http://u:@h:7890") == ("http://h:7890", "u", "")


def test_split_proxy_credentials_ipv6_host():
    assert split_proxy_credentials("http://u:p@[::1]:1080") == ("http://[::1]:1080", "u", "p")


def test_split_proxy_credentials_socks5_scheme_preserved():
    # 不改 scheme 白名单：socks5 条目对节点下载链路有效，拆分只动 userinfo。
    assert split_proxy_credentials("socks5://u:p@1.2.3.4:1080") == ("socks5://1.2.3.4:1080", "u", "p")


def test_split_proxy_credentials_malformed_escape_does_not_raise():
    # unquote 对畸形 % 宽容，原样返回而不抛。
    assert split_proxy_credentials("http://u:p%zz@h:7890") == ("http://h:7890", "u", "p%zz")


def test_split_proxy_credentials_keeps_path_query_fragment():
    assert split_proxy_credentials("http://u:p@h:7890/path?x=1#f") == (
        "http://h:7890/path?x=1#f", "u", "p",
    )


def test_split_proxy_credentials_no_userinfo_returns_unchanged():
    for text in ("http://h:7890", "http://h:7890/", "127.0.0.1:7890", ""):
        assert split_proxy_credentials(text) == (text, "", "")


def test_split_proxy_credentials_userinfo_without_host_not_split():
    # 只有 userinfo 没有主机：不是可用地址，原样返回让上层校验拦下。
    assert split_proxy_credentials("http://u:p@") == ("http://u:p@", "", "")


def test_split_proxy_credentials_empty_userinfo_yields_empty_credentials():
    assert split_proxy_credentials("http://:@h:7890") == ("http://h:7890", "", "")


# ──────────────── resolve_account_* ────────────────

_PROXIES = [
    {"id": "net1", "name": "N1", "mode": "network", "url": "http://127.0.0.1:7890", "username": "", "password": ""},
    {"id": "pre1", "name": "P1", "mode": "url_prefix", "url": "https://relay.example/p", "username": "", "password": ""},
    {"id": "dir1", "name": "D1", "mode": "direct", "url": "", "username": "", "password": ""},
]


def test_resolve_account_network_mode():
    acc = {"username": "a", "proxy_id": "net1"}
    assert resolve_account_proxy(acc, _PROXIES) == "http://127.0.0.1:7890"
    assert resolve_account_url_prefix(acc, _PROXIES) is None


def test_resolve_account_url_prefix_mode():
    acc = {"username": "b", "proxy_id": "pre1"}
    # 前缀模式：proxy 置空（直连前缀服务），url_prefix 返回前缀基址
    assert resolve_account_proxy(acc, _PROXIES) is None
    assert resolve_account_url_prefix(acc, _PROXIES) == "https://relay.example/p"


def test_resolve_account_legacy_literal_proxy_stays_network():
    acc = {"username": "c", "proxy": "http://legacy.example:1234"}
    assert resolve_account_proxy(acc, _PROXIES) == "http://legacy.example:1234"
    assert resolve_account_url_prefix(acc, _PROXIES) is None


def test_resolve_account_direct_mode_forces_no_proxy():
    acc = {"username": "e", "proxy_id": "dir1"}
    # direct 模式：显式强制直连，proxy 与 url_prefix 均为 None
    assert resolve_account_proxy(acc, _PROXIES) is None
    assert resolve_account_url_prefix(acc, _PROXIES) is None


def test_resolve_account_missing_ref_is_no_proxy():
    acc = {"username": "d", "proxy_id": "missing"}
    assert resolve_account_proxy(acc, _PROXIES) is None
    assert resolve_account_url_prefix(acc, _PROXIES) is None


def test_runtime_accounts_injects_both_fields():
    accounts = [
        {"username": "a", "proxy_id": "net1"},
        {"username": "b", "proxy_id": "pre1"},
    ]
    out = runtime_accounts(accounts, _PROXIES)
    assert out[0]["proxy"] == "http://127.0.0.1:7890" and out[0]["url_prefix"] is None
    assert out[1]["proxy"] is None and out[1]["url_prefix"] == "https://relay.example/p"


# ──────────────── apply_url_prefix ────────────────

def test_apply_url_prefix_empty_prefix_is_identity():
    url = "https://api.example.com/v1/chat/completions?a=1"
    assert apply_url_prefix(url, None) == url
    assert apply_url_prefix(url, "") == url


def test_apply_url_prefix_prepends_full_absolute_url():
    url = "https://api.example.com/v1/chat?a=1&b=2"
    assert apply_url_prefix(url, "https://relay.example/p") == "https://relay.example/p/https://api.example.com/v1/chat?a=1&b=2"


def test_apply_url_prefix_trailing_slash_no_double_slash():
    url = "https://api.example.com/v1"
    assert apply_url_prefix(url, "https://relay.example/p/") == "https://relay.example/p/https://api.example.com/v1"


def test_apply_url_prefix_non_http_unchanged():
    assert apply_url_prefix("/v1/models", "https://relay.example/p") == "/v1/models"
    assert apply_url_prefix("ws://x/y", "https://relay.example/p") == "ws://x/y"


def test_apply_url_prefix_not_double_prefixed():
    already = "https://relay.example/p/https://api.example.com/v1"
    assert apply_url_prefix(already, "https://relay.example/p") == already


# ──────────────── provider._outbound_url ────────────────

def test_provider_outbound_url_respects_url_prefix():
    provider = CustomProvider("u", "k", base_url="https://api.example.com", url_prefix="https://relay.example/p")
    assert provider._outbound_url("https://api.example.com/v1/chat") == "https://relay.example/p/https://api.example.com/v1/chat"


def test_provider_outbound_url_identity_without_prefix():
    provider = CustomProvider("u", "k", base_url="https://api.example.com")
    assert provider.url_prefix is None
    assert provider._outbound_url("https://api.example.com/v1/chat") == "https://api.example.com/v1/chat"


# ──────────────── install_url_prefix_interceptor 包裹 _request ────────────────


class _FakeSession:
    """带 _request 属性的假 session：get/post/request 在 aiohttp 内部都归到 _request。"""

    def __init__(self):
        self.calls = []

    def _request(self, method, str_or_url, **kwargs):
        self.calls.append((method, str_or_url, kwargs))
        return "ctx"


def test_interceptor_rewrites_request_url_in_url_prefix_mode():
    """url_prefix 模式：_request 收到的 URL 被改写为前缀+原始URL。"""
    from providers.base import install_url_prefix_interceptor

    provider = CustomProvider("u", "k", base_url="https://api.example.com", url_prefix="https://relay.example/p")
    session = _FakeSession()
    install_url_prefix_interceptor(session, provider)

    session._request("POST", "https://api.example.com/v1/chat")
    assert session.calls[0][0] == "POST"
    assert session.calls[0][1] == "https://relay.example/p/https://api.example.com/v1/chat"


def test_interceptor_identity_when_no_prefix():
    """network / 无前缀：_request 收到的 URL 不改写。"""
    from providers.base import install_url_prefix_interceptor

    provider = CustomProvider("u", "k", base_url="https://api.example.com")
    session = _FakeSession()
    install_url_prefix_interceptor(session, provider)

    session._request("GET", "https://api.example.com/v1/models")
    assert session.calls[0][1] == "https://api.example.com/v1/models"


def test_interceptor_hot_update_of_prefix_takes_effect_on_existing_session():
    """代理池热更新改 provider.url_prefix 后，已建 session 的下一次请求即用新前缀。"""
    from providers.base import install_url_prefix_interceptor

    provider = CustomProvider("u", "k", base_url="https://api.example.com")
    session = _FakeSession()
    install_url_prefix_interceptor(session, provider)

    session._request("POST", "https://api.example.com/v1/chat")
    assert session.calls[0][1] == "https://api.example.com/v1/chat"  # 初始无前缀

    provider.url_prefix = "https://relay.example/p"  # 模拟热更新
    session._request("POST", "https://api.example.com/v1/chat")
    assert session.calls[1][1] == "https://relay.example/p/https://api.example.com/v1/chat"
