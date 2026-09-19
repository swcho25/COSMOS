"""CPU-only mathematical/adapter checks; no GPU or model is loaded."""
from pathlib import Path
from types import SimpleNamespace
import sys
import unittest
from unittest.mock import patch

import torch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import local_relevance_attention as attention


class ExplicitKVAttentionTest(unittest.TestCase):
    def setUp(self):
        self.generator = torch.Generator(device="cpu").manual_seed(20260916)
        self.module = SimpleNamespace(is_causal=True, num_key_value_groups=4)

    def tensors(self, q_length, kv_length=None):
        kv_length = q_length if kv_length is None else kv_length
        def rand(*shape):
            return torch.randn(*shape, generator=self.generator, device="cpu", dtype=torch.float64)
        return rand(2, 8, q_length, 16), rand(2, 2, kv_length, 16), rand(2, 2, kv_length, 16)

    def eager_reference(self, q, k, v, mask=None, causal=False, scale=None):
        # Compute each query head against its original grouped KV head; this
        # independent reference never repeats/reshapes KV heads like the adapter.
        scale = q.shape[-1] ** -0.5 if scale is None else scale
        outputs = []
        for head in range(q.shape[1]):
            kv_head = head // (q.shape[1] // k.shape[1])
            score = q[:, head] @ k[:, kv_head].transpose(-2, -1) * scale
            if causal:
                allowed = torch.ones(q.shape[-2], k.shape[-2], dtype=torch.bool, device="cpu").tril()
                score = score.masked_fill(~allowed, float("-inf"))
            if mask is not None:
                per_head = mask[:, 0 if mask.shape[1] == 1 else head]
                score = score.masked_fill(~per_head, float("-inf")) if mask.dtype == torch.bool else score + per_head
            probability = torch.nan_to_num(torch.softmax(score, dim=-1))
            outputs.append(probability @ v[:, kv_head])
        return torch.stack(outputs, dim=2)

    def assert_output(self, tensors, mask=None, causal=False, **kwargs):
        actual, weights = attention.explicit_kv_sdpa_attention_forward(self.module, *tensors, mask, **kwargs)
        expected = self.eager_reference(*tensors, mask=mask, causal=causal, scale=kwargs.get("scaling"))
        torch.testing.assert_close(actual, expected, rtol=1e-11, atol=1e-11)
        self.assertIsNone(weights)
        self.assertTrue(actual.is_contiguous())
        self.assertEqual(self.module.num_key_value_groups, 4)
        return actual

    def test_causal_prefill_and_custom_scale(self):
        self.assert_output(self.tensors(9), causal=True, scaling=0.3)

    def test_single_token_cache_decode_attends_full_cache(self):
        tensors = self.tensors(1, 11)
        self.assert_output(tensors)

    def test_padding_and_causal_boolean_mask(self):
        tensors = self.tensors(7)
        mask = torch.ones(2, 1, 7, 7, dtype=torch.bool, device="cpu").tril()
        mask[0, :, :, :2] = False
        mask[1, :, :, -2:] = False
        self.assert_output(tensors, mask=mask)

    def test_additive_mask_for_cached_multi_token_chunk(self):
        tensors = self.tensors(3, 8)
        q_pos = torch.arange(5, 8, device="cpu")[:, None]
        kv_pos = torch.arange(8, device="cpu")[None, :]
        allowed = (kv_pos <= q_pos)[None, None].expand(2, 1, 3, 8).clone()
        allowed[0, :, :, 0] = False
        mask = torch.zeros(2, 1, 3, 8, dtype=torch.float64, device="cpu").masked_fill(~allowed, float("-inf"))
        self.assert_output(tensors, mask=mask)

    def test_expanded_kv_not_repeated_twice_or_module_mutated(self):
        q, k, v = self.tensors(5)
        k, v = k.repeat_interleave(4, dim=1), v.repeat_interleave(4, dim=1)
        expanded_k, expanded_v = attention._expand_kv(q, k, v)
        self.assertIs(expanded_k, k)
        self.assertIs(expanded_v, v)
        self.assert_output((q, k, v), causal=True)

    def test_explicit_noncausal_override(self):
        self.assert_output(self.tensors(6), is_causal=False)

    def test_static_prefill_ignores_unused_cache_capacity(self):
        q, k, v = self.tensors(4, 10)
        actual, _ = attention.explicit_kv_sdpa_attention_forward(self.module, q, k, v, None)
        expected = self.eager_reference(q, k[:, :, :4], v[:, :, :4], causal=True)
        torch.testing.assert_close(actual, expected, rtol=1e-11, atol=1e-11)

    def test_sdpa_has_matching_heads_and_gqa_disabled(self):
        q, k, v = self.tensors(5)
        real_sdpa = attention.F.scaled_dot_product_attention
        with patch.object(attention.F, "scaled_dot_product_attention", wraps=real_sdpa) as observed:
            attention.explicit_kv_sdpa_attention_forward(self.module, q, k, v, None)
        args, kwargs = observed.call_args
        self.assertEqual([tensor.shape[1] for tensor in args], [8, 8, 8])
        self.assertFalse(kwargs["enable_gqa"])
        self.assertEqual(k.shape[1], 2)

    def test_cuda_policy_selects_only_efficient_without_allocating_cuda(self):
        with patch.object(attention, "sdpa_kernel") as context:
            attention._kernel_context(SimpleNamespace(type="cuda"))
        context.assert_called_once_with(attention.SDPBackend.EFFICIENT_ATTENTION)

    def test_registration_preserves_builtin_sdpa_and_uses_same_mask(self):
        from transformers.modeling_utils import ALL_ATTENTION_FUNCTIONS
        from transformers.masking_utils import ALL_MASK_ATTENTION_FUNCTIONS, sdpa_mask
        previous_attention = ALL_ATTENTION_FUNCTIONS["sdpa"]
        previous_mask = ALL_MASK_ATTENTION_FUNCTIONS["sdpa"]
        name = attention.register_attention_backend()
        self.assertEqual(name, "sdpa_explicit_kv")
        self.assertIs(ALL_ATTENTION_FUNCTIONS[name], attention.explicit_kv_sdpa_attention_forward)
        self.assertIs(ALL_MASK_ATTENTION_FUNCTIONS[name], sdpa_mask)
        self.assertIs(ALL_ATTENTION_FUNCTIONS["sdpa"], previous_attention)
        self.assertIs(ALL_MASK_ATTENTION_FUNCTIONS["sdpa"], previous_mask)
        self.assertEqual(attention.register_attention_backend(), name)

    def test_bad_head_mapping_and_attention_weights_fail_explicitly(self):
        q, k, v = self.tensors(4)
        with self.assertRaises(ValueError):
            attention.explicit_kv_sdpa_attention_forward(self.module, q[:, :7], k, v, None)
        with self.assertRaises(ValueError):
            attention.explicit_kv_sdpa_attention_forward(self.module, q, k, v[:, :1], None)
        with self.assertRaises(ValueError):
            attention.explicit_kv_sdpa_attention_forward(self.module, q, k, v, None, output_attentions=True)


if __name__ == "__main__":
    unittest.main()
