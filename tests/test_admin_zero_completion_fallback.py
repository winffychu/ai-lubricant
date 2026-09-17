"""管理端测试面板的零 completion 兜底回归测试。

历史 bug：管理端 /admin 渠道测试面板只用 OpenAI `delta.content`（final_text）判定
"是否有输出"，且非流式校验只看当前 chunk，导致 Anthropic / Responses / Gemini /
reasoning / tool_calls 等协议在上游 usage.completion_tokens=0 时被误判为空响应，
抛 502 `upstream response usage reports zero completion tokens`。

修复后：判定统一走协议无关的 `_has_first_token_content`，有真实输出就只把 completion
分量归零、保留上游给的有效 prompt/cached，交给后续统计估算补 completion，不判失败；
只有真正无内容 + 零 completion 才抛 502。

（早期版本是整包 `del payload["usage"]`，但那样会把上游给出的有效 prompt_tokens
一起丢掉，下游只能靠估算，口径一旦读不到输入键就把有效 prompt 记成 0——见
usage-prompt-tokens-doubled.md 的姐妹问题「输入 token 为 0」。）
"""
import pytest
from fastapi import HTTPException

from admin import _admin_validate_upstream_usage_payload


# ---------------------------------------------------------------------------
# 有真实内容 + usage.completion_tokens=0 → 只归零 completion、不抛（各协议）
# 上游给的有效 prompt 分量必须保留，不能整包删 usage。
# ---------------------------------------------------------------------------

def test_openai_content_with_zero_completion_keeps_prompt():
    payload = {
        "choices": [{"message": {"content": "hi"}}],
        "usage": {"prompt_tokens": 5, "completion_tokens": 0, "total_tokens": 5},
    }
    _admin_validate_upstream_usage_payload(payload)
    assert payload["usage"]["prompt_tokens"] == 5
    assert payload["usage"]["completion_tokens"] == 0


def test_openai_reasoning_only_with_zero_completion_keeps_prompt():
    # 仅 reasoning_content（无 content）也算真实输出。
    payload = {
        "choices": [{"delta": {"reasoning_content": "思考中"}}],
        "usage": {"prompt_tokens": 5, "completion_tokens": 0, "total_tokens": 5},
    }
    _admin_validate_upstream_usage_payload(payload)
    assert payload["usage"]["prompt_tokens"] == 5
    assert payload["usage"]["completion_tokens"] == 0


def test_openai_tool_calls_only_with_zero_completion_keeps_prompt():
    payload = {
        "choices": [{"delta": {"tool_calls": [{"id": "1", "function": {"name": "f"}}]}}],
        "usage": {"prompt_tokens": 5, "completion_tokens": 0, "total_tokens": 5},
    }
    _admin_validate_upstream_usage_payload(payload)
    assert payload["usage"]["prompt_tokens"] == 5
    assert payload["usage"]["completion_tokens"] == 0


def test_anthropic_content_block_with_zero_output_tokens_keeps_prompt():
    # Anthropic 直通：content 走 content_block_delta.text，usage 用 output_tokens。
    # 零的只是 output_tokens，input_tokens（= prompt 分量）原样保留。
    payload = {
        "type": "content_block_delta",
        "delta": {"type": "text_delta", "text": "hello"},
        "usage": {"input_tokens": 5, "output_tokens": 0},
    }
    _admin_validate_upstream_usage_payload(payload)
    assert payload["usage"]["input_tokens"] == 5


def test_responses_output_item_with_zero_completion_keeps_prompt():
    # Responses API：内容走 response.output_item.added。
    payload = {
        "type": "response.output_item.added",
        "item": {"type": "message"},
        "usage": {"prompt_tokens": 5, "completion_tokens": 0, "total_tokens": 5},
    }
    _admin_validate_upstream_usage_payload(payload)
    assert payload["usage"]["prompt_tokens"] == 5
    assert payload["usage"]["completion_tokens"] == 0


def test_content_and_usage_in_separate_chunks_uses_prior_had_content():
    # usage 帧与 content 帧分离：本 attempt 之前已见过内容 → had_content=True → 不抛。
    _admin_validate_upstream_usage_payload(
        {"usage": {"prompt_tokens": 5, "completion_tokens": 0, "total_tokens": 5}},
        had_content=True,
    )


# ---------------------------------------------------------------------------
# 真正无内容 + 零 completion → 仍抛 502
# ---------------------------------------------------------------------------

def test_empty_response_with_zero_completion_raises():
    with pytest.raises(HTTPException) as exc:
        _admin_validate_upstream_usage_payload(
            {"choices": [{"message": {}}],
             "usage": {"prompt_tokens": 5, "completion_tokens": 0, "total_tokens": 5}},
        )
    assert exc.value.status_code == 502


def test_no_usage_payload_never_raises():
    # 没有 usage 字段时不判定（交给流末尾/估算兜底）。
    _admin_validate_upstream_usage_payload({"choices": [{"message": {}}]})


def test_nonzero_completion_never_raises():
    _admin_validate_upstream_usage_payload(
        {"choices": [{"message": {}}],
         "usage": {"prompt_tokens": 5, "completion_tokens": 3, "total_tokens": 8}},
    )
