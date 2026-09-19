"""Explicit-KV SDPA backend for the local Qwen3 relevance pilot.

PyTorch 2.6 GQA only supports Flash/math, while this Windows build has no
Flash kernel. Repeating KV heads before SDPA permits the efficient kernel.
CUDA must use that kernel or raise; no silent math fallback is allowed.
CPU execution is retained for small equivalence tests, not model inference.

The original grouped cache and model attributes remain unchanged. Matching
Transformers ``sdpa_mask`` registration preserves causal and padding masks.
This is an execution change, not an input truncation or new scoring policy.

References:
https://docs.pytorch.org/docs/2.6/generated/torch.nn.functional.scaled_dot_product_attention.html
https://huggingface.co/docs/transformers/main/en/attention_interface
"""
from __future__ import annotations

from contextlib import nullcontext

import torch
from torch.nn import functional as F
from torch.nn.attention import SDPBackend, sdpa_kernel


BACKEND_NAME = "sdpa_explicit_kv"


def _kernel_context(device):
    return sdpa_kernel(SDPBackend.EFFICIENT_ATTENTION) if device.type == "cuda" else nullcontext()


def _expand_kv(query, key, value):
    if any(tensor.ndim != 4 for tensor in (query, key, value)):
        raise ValueError("Expected [batch, heads, sequence, head_dim] tensors")
    query_heads, key_heads, value_heads = query.shape[1], key.shape[1], value.shape[1]
    if key_heads != value_heads or key_heads <= 0 or query_heads % key_heads:
        raise ValueError("Query heads must be a multiple of equal key/value heads")
    repeats = query_heads // key_heads
    if repeats != 1:
        key = key.repeat_interleave(repeats, dim=1)
        value = value.repeat_interleave(repeats, dim=1)
    return key, value


def explicit_kv_sdpa_attention_forward(
    module,
    query,
    key,
    value,
    attention_mask,
    dropout=0.0,
    scaling=None,
    is_causal=None,
    position_bias=None,
    **kwargs,
):
    """Transformers attention signature, returning [batch, sequence, heads, dim]."""
    if kwargs.get("output_attentions", False):
        raise ValueError("This SDPA backend does not return attention weights")
    # Determine expansion from tensor shapes, never mutate the module's GQA
    # setting. Already expanded KV tensors therefore cannot be repeated twice.
    key, value = _expand_kv(query, key, value)
    q_length, kv_length = query.shape[2], key.shape[2]
    causal_setting = getattr(module, "is_causal", True) if is_causal is None else is_causal
    causal = bool(q_length > 1 and attention_mask is None and causal_setting)

    # Same empty-StaticCache prefill handling as Transformers' SDPA adapter.
    if causal and kv_length > q_length:
        key, value = key[:, :, :q_length, :], value[:, :, :q_length, :]
        if position_bias is not None:
            position_bias = position_bias[:, :, :, :q_length]
    if position_bias is not None:
        from transformers.integrations.sdpa_attention import create_position_bias_mask

        attention_mask = create_position_bias_mask(position_bias, attention_mask, causal, query, key)
        causal = False

    with _kernel_context(query.device):
        output = F.scaled_dot_product_attention(
            query, key, value, attn_mask=attention_mask, dropout_p=dropout,
            scale=scaling, is_causal=causal, enable_gqa=False,
        )
    return output.transpose(1, 2).contiguous(), None


def register_attention_backend():
    """Register both attention and the unchanged SDPA mask; return its name."""
    from transformers import AttentionInterface, AttentionMaskInterface
    from transformers.masking_utils import sdpa_mask

    AttentionInterface.register(BACKEND_NAME, explicit_kv_sdpa_attention_forward)
    AttentionMaskInterface.register(BACKEND_NAME, sdpa_mask)
    return BACKEND_NAME
