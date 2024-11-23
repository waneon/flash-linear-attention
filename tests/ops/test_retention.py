# -*- coding: utf-8 -*-

import pytest
import torch

from fla.ops.retention import (chunk_retention, fused_recurrent_retention,
                               parallel_retention)
from fla.ops.retention.naive import naive_retention


def get_abs_err(x, y):
    return (x-y).flatten().abs().max().item()


def get_err_ratio(x, y):
    err = (x-y).flatten().square().mean().sqrt().item()
    base = (x).flatten().square().mean().sqrt().item()
    return err / base


def assert_close(prefix, ref, tri, ratio):
    msg = f"{prefix} diff: {get_abs_err(ref, tri):.6f} ratio: {get_err_ratio(ref, tri):.6f}"
    print(msg)
    assert get_err_ratio(ref, tri) < ratio, msg


@pytest.mark.parametrize("B", [4])
@pytest.mark.parametrize("H", [4])
@pytest.mark.parametrize("T", [300, 512])
@pytest.mark.parametrize("K", [32, 64, 100])
@pytest.mark.parametrize("expand_ratio", [1, 2])
@pytest.mark.parametrize("head_first", [True, False])
@pytest.mark.parametrize("dtype", [torch.float16, torch.float32])
def test_chunk(
    B: int,
    H: int,
    T: int,
    K: int,
    expand_ratio: int,
    head_first: bool,
    dtype: torch.dtype
):
    torch.manual_seed(42)
    V = K * expand_ratio

    if head_first:
        q = torch.randn((B, H, T, K), dtype=dtype, device='cuda').requires_grad_()
        k = torch.randn((B, H, T, K), dtype=dtype, device='cuda').requires_grad_()
        v = torch.randn((B, H, T, V), dtype=dtype, device='cuda').requires_grad_()
    else:
        q = torch.randn((B, T, H, K), dtype=dtype, device='cuda').requires_grad_()
        k = torch.randn((B, T, H, K), dtype=dtype, device='cuda').requires_grad_()
        v = torch.randn((B, T, H, V), dtype=dtype, device='cuda').requires_grad_()
    h0 = torch.randn((B, H, K, V), dtype=dtype, device='cuda').requires_grad_()

    do = torch.randn_like(v)
    dht = torch.randn_like(h0)
    ref, ref_ht = fused_recurrent_retention(q, k, v, initial_state=h0, output_final_state=True, head_first=head_first)
    ((ref * do).sum() + (ref_ht * dht).sum()).backward()
    ref_dq, q.grad = q.grad.clone(), None
    ref_dk, k.grad = k.grad.clone(), None
    ref_dv, v.grad = v.grad.clone(), None

    tri, tri_ht = chunk_retention(q, k, v, initial_state=h0, output_final_state=True, head_first=head_first)
    ((tri * do).sum() + (tri_ht * dht).sum()).backward()
    tri_dq, q.grad = q.grad.clone(), None
    tri_dk, k.grad = k.grad.clone(), None
    tri_dv, v.grad = v.grad.clone(), None

    assert_close(" o", ref, tri, 0.005)
    assert_close("ht", ref_ht, tri_ht, 0.005)
    assert_close("dq", ref_dq, tri_dq, 0.005)
    assert_close("dk", ref_dk, tri_dk, 0.005)
    assert_close("dv", ref_dv, tri_dv, 0.005)


@pytest.mark.parametrize("B", [4])
@pytest.mark.parametrize("H", [4])
@pytest.mark.parametrize("T", [300, 512])
@pytest.mark.parametrize("K", [32, 64, 100])
@pytest.mark.parametrize("expand_ratio", [1, 2])
@pytest.mark.parametrize("dtype", [torch.float16, torch.float32])
def test_parallel(
    B: int,
    H: int,
    T: int,
    K: int,
    expand_ratio: int,
    dtype: torch.dtype
):
    torch.manual_seed(42)
    V = K * expand_ratio

    q = torch.randn((B, H, T, K), dtype=dtype, device='cuda').requires_grad_()
    k = torch.randn((B, H, T, K), dtype=dtype, device='cuda').requires_grad_()
    v = torch.randn((B, H, T, V), dtype=dtype, device='cuda').requires_grad_()
    do = torch.randn_like(v)

    ref = naive_retention(q, k, v)
    ref.backward(do)
    ref_dq, q.grad = q.grad.clone(), None
    ref_dk, k.grad = k.grad.clone(), None
    ref_dv, v.grad = v.grad.clone(), None

    tri, _ = parallel_retention(q, k, v)
    tri.backward(do)
    tri_dq, q.grad = q.grad.clone(), None
    tri_dk, k.grad = k.grad.clone(), None
    tri_dv, v.grad = v.grad.clone(), None

    assert_close(" o", ref, tri, 0.005)
    assert_close("dq", ref_dq, tri_dq, 0.005)
    assert_close("dk", ref_dk, tri_dk, 0.005)
    assert_close("dv", ref_dv, tri_dv, 0.005)
