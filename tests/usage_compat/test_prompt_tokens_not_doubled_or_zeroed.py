"""prompt_tokens 口径回归：既不翻倍，也不被抹成 0。

两个独立缺陷，同属一条 usage 链：

1. 翻倍（见 specs/usage-prompt-tokens-doubled.md）：
   `normalize_usage` 的加回判据 `input_val <= cache_total` 把「恰好相等」当成
   「缓存未计入 input」的证据。DeepSeek 风格 `prompt_cache_hit_tokens +
   prompt_cache_miss_tokens` 恒等于 `prompt_tokens`，于是每次请求都走进加回分支，
   `prompt_tokens` 精确翻倍（60254 -> 120508）。

2. 抹成 0（姐妹问题）：
   上游给有效 prompt、只把 completion 报 0 时，`_validate_upstream_usage_payload`
   整包 `del usage`，把有效 prompt 一起丢掉；下游只能靠估算，而 `estimate_usage`
   写死读 system/messages，在 Responses 体（input/instructions）上恒估出 0。

修复后：分量对存在时直接采信 prompt_tokens；相等不再触发加回；零 completion
不再整包删 usage；估算口径改为读 body 里实际存在的输入键（不分协议）。
"""

import pytest

from usage_utils import estimate_usage, fill_usage_with_estimate, normalize_usage


# ── 1. 翻倍 ────────────────────────────────────────────────

def test_deepseek_cache_split_does_not_double_prompt_tokens():
    """spec 中的真实 payload：上游 60254，平台曾回传 120508。"""
    usage = normalize_usage({
        "prompt_tokens": 60254,
        "completion_tokens": 638,
        "total_tokens": 60892,
        "prompt_cache_hit_tokens": 57728,
        "prompt_cache_miss_tokens": 2526,
        "prompt_tokens_details": {"cached_tokens": 57728},
    })
    assert usage["prompt_tokens"] == 60254
    assert usage["total_tokens"] == 60892
    assert usage["cached_tokens"] == 57728


def test_deepseek_split_without_details_does_not_double():
    """分量对存在但无 prompt_tokens_details 时同样不加回。"""
    usage = normalize_usage({
        "prompt_tokens": 100704,
        "completion_tokens": 500,
        "prompt_cache_hit_tokens": 96000,
        "prompt_cache_miss_tokens": 4704,
    })
    assert usage["prompt_tokens"] == 100704


def test_cold_cache_all_miss_does_not_double():
    """冷缓存全 miss：hit=0 也构成分量对，prompt_tokens 仍是总输入。"""
    usage = normalize_usage({
        "prompt_tokens": 60254,
        "completion_tokens": 10,
        "prompt_cache_hit_tokens": 0,
        "prompt_cache_miss_tokens": 60254,
    })
    assert usage["prompt_tokens"] == 60254


def test_fully_cached_without_split_does_not_double():
    """无分量对、但缓存恰好等于全部输入（全命中）时也不能加回。

    这是 spec 未覆盖的同类路径：`input <= cache_total` 的等号在多义场景下命中。
    """
    usage = normalize_usage({
        "prompt_tokens": 1000,
        "completion_tokens": 5,
        "prompt_tokens_details": {"cached_tokens": 1000},
    })
    assert usage["prompt_tokens"] == 1000


def test_fill_usage_with_estimate_is_idempotent_under_split():
    """兜底函数内部会再 normalize 一次，不能让缓存被二次加回。"""
    filled = fill_usage_with_estimate(
        {"prompt_tokens": 60254, "completion_tokens": 0,
         "cached_tokens": 57728, "cache_creation_tokens": 2526},
        {"prompt_tokens": 0, "completion_tokens": 638},
    )
    assert filled["prompt_tokens"] == 60254
    assert filled["completion_tokens"] == 638


# ── 2. 不被抹成 0 ───────────────────────────────────────────

@pytest.mark.parametrize("body", [
    {"messages": [{"role": "user", "content": "x" * 4000}]},                      # chat
    {"input": [{"role": "user", "content": "x" * 4000}], "instructions": "y" * 500},  # responses
    {"messages": [{"role": "user", "content": "x" * 4000}], "system": "y" * 500},     # anthropic
])
def test_estimate_usage_reads_input_regardless_of_protocol(body):
    """同一份内容落在 messages / input 上必须估出同一个非零 prompt。

    修复前只读 system/messages，Responses 体（input/instructions）恒估出 0。
    """
    estimated = estimate_usage(body, {"choices": []}, "m")
    assert estimated["prompt_tokens"] > 0


def test_zero_completion_usage_keeps_valid_prompt():
    """上游给有效 prompt、completion=0 时，prompt 必须保住（不被整包删）。"""
    usage = normalize_usage({"prompt_tokens": 60254, "completion_tokens": 0})
    assert usage["prompt_tokens"] == 60254


def test_per_component_fill_backfills_completion_only():
    """逐分量兜底：completion 为 0 补估算，有效 prompt 不被估算顶替。"""
    filled = fill_usage_with_estimate(
        {"prompt_tokens": 60254, "completion_tokens": 0},
        {"prompt_tokens": 1, "completion_tokens": 638},
    )
    assert filled["prompt_tokens"] == 60254
    assert filled["completion_tokens"] == 638
