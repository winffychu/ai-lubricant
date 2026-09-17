"""代理池条目归一化：把粘进 url 的 userinfo 拆到 username/password。

对应用户侧的「一行添加」体验：表单只留一个代理地址框，用户把
``http://user:pass@host:port`` 整行粘进去，后端拆开存结构化的三个字段，
运行时再由 ``proxy_effective_url`` 拼回去。
"""
import hashlib

import pytest


@pytest.fixture(scope="module")
def admin():
    import admin as _admin

    return _admin


# ──────────────── url 里的凭据被拆到 username/password ────────────────

def test_credentials_in_url_are_split_out(admin):
    item = admin._normalize_proxy_item(
        {"name": "P", "mode": "network", "url": "http://user:pass@127.0.0.1:7890"}
    )
    assert item["url"] == "http://127.0.0.1:7890"
    assert item["username"] == "user"
    assert item["password"] == "pass"


def test_split_credentials_round_trip_through_effective_url(admin):
    """拆开存 → 运行时拼回，与用户粘进来的那一行等价。"""
    item = admin._normalize_proxy_item(
        {"name": "P", "mode": "network", "url": "http://user:pass@127.0.0.1:7890"}
    )
    assert admin._proxy_effective_url(item) == "http://user:pass@127.0.0.1:7890"


def test_explicit_username_wins_and_url_userinfo_is_still_stripped(admin):
    """显式传 username 时以它为准，但 url 里的 userinfo 仍要摘掉——两处认证并存会让
    运行时拼出的地址含义含糊。"""
    item = admin._normalize_proxy_item(
        {
            "name": "P",
            "mode": "network",
            "url": "http://urluser:urlpass@127.0.0.1:7890",
            "username": "explicit",
            "password": "explicitpass",
        }
    )
    assert item["url"] == "http://127.0.0.1:7890"
    assert item["username"] == "explicit"
    assert item["password"] == "explicitpass"


def test_legacy_entry_with_credentials_in_url_is_normalized_in_place(admin):
    """历史遗留的「凭据写在 url 里」条目在读取/保存时就地归一化，id 不变
    （账号里存的 proxy_id 引用不受影响）。"""
    item = admin._normalize_proxy_item(
        {"id": "proxy_legacy", "name": "P", "mode": "network", "url": "http://u:p@h:7890"}
    )
    assert item["id"] == "proxy_legacy"
    assert item["url"] == "http://h:7890"
    assert item["username"] == "u"
    assert item["password"] == "p"


def test_entry_without_credentials_is_untouched(admin):
    item = admin._normalize_proxy_item(
        {"name": "P", "mode": "network", "url": "http://127.0.0.1:7890"}
    )
    assert item["url"] == "http://127.0.0.1:7890"
    assert item["username"] == ""
    assert item["password"] == ""


def test_url_prefix_base_with_at_in_path_is_not_split(admin):
    """url_prefix 的基址不走认证，其 path 里若有 @ 不应被当凭据摘掉。"""
    item = admin._normalize_proxy_item(
        {"name": "P", "mode": "url_prefix", "url": "https://relay.example/p@th"}
    )
    assert item["url"] == "https://relay.example/p@th"
    assert item["username"] == ""
    assert item["password"] == ""


# ──────────────── _proxy_id：凭据参与判别，且不扰动既有口径 ────────────────

def test_same_host_different_credentials_get_distinct_ids(admin):
    """摘掉 url 里的 userinfo 后，同名 + 同 host:port + 不同凭据的多条代理 url 相同，
    不带 username 入 seed 会撞 id，_normalize_proxies 会以「代理 ID 重复」400 拒绝整批。"""
    a = admin._proxy_id({"name": "P", "mode": "network", "url": "http://h:7890", "username": "u1"})
    b = admin._proxy_id({"name": "P", "mode": "network", "url": "http://h:7890", "username": "u2"})
    assert a != b


def test_idless_entry_without_username_keeps_legacy_id(admin):
    """无认证条目的 seed 与旧口径逐字节一致，避免任何 id-less 历史条目被改号。"""
    legacy = "proxy_" + hashlib.sha1("P|network|http://h:7890".encode("utf-8")).hexdigest()[:12]
    assert admin._proxy_id({"name": "P", "mode": "network", "url": "http://h:7890"}) == legacy


def test_existing_id_is_returned_verbatim(admin):
    assert admin._proxy_id({"id": "proxy_keep", "name": "P", "url": "http://h:7890"}) == "proxy_keep"


# ──────────────── _normalize_proxies：整批不再因凭据撞 id 而 400 ────────────────

def test_batch_with_same_host_different_credentials_is_accepted(admin):
    items = [
        {"name": "P", "mode": "network", "url": "http://u1:p1@h:7890"},
        {"name": "P", "mode": "network", "url": "http://u2:p2@h:7890"},
    ]
    normalized = admin._normalize_proxies(items)
    assert len(normalized) == 2
    assert {p["username"] for p in normalized} == {"u1", "u2"}
    assert all(p["url"] == "http://h:7890" for p in normalized)
