# -*- coding: utf-8 -*-
# Copyright (c) 2024, Songlin Yang, Yu Zhang

from typing import Tuple

import torch
import triton
import triton.language as tl


@triton.autotune(
    configs=[
        triton.Config({}, num_warps=1),
        triton.Config({}, num_warps=2),
        triton.Config({}, num_warps=4),
        triton.Config({}, num_warps=8),
    ],
    key=["BT", "BK", "BV", "USE_G", 'USE_GK', 'USE_GV'],
)
@triton.heuristics({
    'USE_INITIAL_STATE': lambda args: args['h0'] is not None,
    'STORE_FINAL_STATE': lambda args: args['ht'] is not None
})
@triton.jit
def chunk_fwd_kernel_h(
    k,
    v,
    h,
    g,
    gk,
    gv,
    h0,
    ht,
    T: tl.constexpr,
    H: tl.constexpr,
    K: tl.constexpr,
    V: tl.constexpr,
    BT: tl.constexpr,
    BK: tl.constexpr,
    BV: tl.constexpr,
    NT: tl.constexpr,
    USE_G: tl.constexpr,
    USE_GK: tl.constexpr,
    USE_GV: tl.constexpr,
    USE_INITIAL_STATE: tl.constexpr,
    STORE_FINAL_STATE: tl.constexpr,
    HEAD_FIRST: tl.constexpr
):
    i_k, i_v, i_bh = tl.program_id(0), tl.program_id(1), tl.program_id(2)
    i_b, i_h = i_bh // H, i_bh % H

    # [BK, BV]
    b_h = tl.zeros([BK, BV], dtype=tl.float32)
    if USE_INITIAL_STATE:
        p_h0 = tl.make_block_ptr(h0 + i_bh * K*V, (K, V), (V, 1), (i_k * BK, i_v * BV), (BK, BV), (1, 0))
        b_h = tl.load(p_h0, boundary_check=(0, 1)).to(tl.float32)

    for i_t in range(NT):
        if HEAD_FIRST:
            p_k = tl.make_block_ptr(k + i_bh * T*K, (K, T), (1, K), (i_k * BK, i_t * BT), (BK, BT), (0, 1))
            p_v = tl.make_block_ptr(v + i_bh * T*V, (T, V), (V, 1), (i_t * BT, i_v * BV), (BT, BV), (1, 0))
        else:
            p_k = tl.make_block_ptr(k + i_b * T*H*K + i_h * K, (K, T), (1, H*K), (i_k * BK, i_t * BT), (BK, BT), (0, 1))
            p_v = tl.make_block_ptr(v + i_b * T*H*V + i_h * V, (T, V), (H*V, 1), (i_t * BT, i_v * BV), (BT, BV), (1, 0))
        p_h = tl.make_block_ptr(h + i_bh * NT*K*V + i_t * K * V, (K, V), (V, 1), (i_k * BK, i_v * BV), (BK, BV), (1, 0))

        tl.store(p_h, b_h.to(p_h.dtype.element_ty), boundary_check=(0, 1))
        # [BK, BT]
        b_k = tl.load(p_k, boundary_check=(0, 1))
        # [BT, BV]
        b_v = tl.load(p_v, boundary_check=(0, 1))
        last_idx = min((i_t + 1) * BT, T) - 1

        # scalar decay
        if USE_G:
            if HEAD_FIRST:
                b_g_last = tl.load(g + i_bh * T + last_idx)
                p_g = g + i_bh * T + i_t * BT + tl.arange(0, BT)
                p_g = tl.max_contiguous(tl.multiple_of(p_g, BT), BT)
            else:
                b_g_last = tl.load(g + i_b * T * H + last_idx * H + i_h)
                p_g = g + i_b * T*H + (i_t * BT + tl.arange(0, BT)) * H + i_h
            b_h *= tl.exp(b_g_last)
            b_g = tl.load(p_g, mask=(i_t * BT + tl.arange(0, BT) < T), other=0.)
            b_v = (b_v * tl.exp(b_g_last - b_g)[:, None]).to(b_v.dtype)

        # vector decay, h = Diag(gk) @ h
        if USE_GK:
            if HEAD_FIRST:
                p_gk = tl.make_block_ptr(gk + i_bh * T*K, (K, T), (1, K), (i_k * BK, i_t * BT), (BK, BT), (0, 1))
                p_gk_last = gk + i_bh * T*K + last_idx * K + i_k * BK + tl.arange(0, BK)
            else:
                p_gk = tl.make_block_ptr(gk + i_b * T*H*K + i_h * K, (K, T), (1, H*K), (i_k * BK, i_t * BT), (BK, BT), (0, 1))
                p_gk_last = gk + i_b * T*H*K + last_idx * H*K + i_h * K + i_k * BK + tl.arange(0, BK)
            p_gk_last = tl.max_contiguous(tl.multiple_of(p_gk_last, BK), BK)
            b_gk_last = tl.load(p_gk_last, mask=(i_k * BK + tl.arange(0, BK) < K), other=0.)
            b_h *= tl.exp(b_gk_last)[:, None]

            b_gk = tl.load(p_gk, boundary_check=(0, 1))
            b_k = (b_k * tl.exp(b_gk_last[:, None] - b_gk)).to(b_k.dtype)

        # vector decay, h = h @ Diag(gv)
        if USE_GV:
            if HEAD_FIRST:
                p_gv = tl.make_block_ptr(gv + i_bh * T*V, (T, V), (V, 1), (i_t * BT, i_v * BV), (BT, BV), (1, 0))
                p_gv_last = gv + i_bh * T*V + last_idx * V + i_v * BV + tl.arange(0, BV)
            else:
                p_gv = tl.make_block_ptr(gv + i_b * T*H*V + i_h * V, (T, V), (H*V, 1), (i_t * BT, i_v * BV), (BT, BV), (1, 0))
                p_gv_last = gv + i_b * T*H*V + last_idx * H*V + i_h * V + i_v * BV + tl.arange(0, BV)
            p_gv_last = tl.max_contiguous(tl.multiple_of(p_gv_last, BV), BV)
            b_gv_last = tl.load(p_gv_last, mask=(i_v * BV + tl.arange(0, BV) < V), other=0.)
            b_h *= tl.exp(b_gv_last)[None, :]

            b_gv = tl.load(p_gv, boundary_check=(0, 1))
            b_v = (b_v * tl.exp(b_gv_last[None, :] - b_gv)).to(b_v.dtype)

        b_h += tl.dot(b_k, b_v)

    if STORE_FINAL_STATE:
        p_ht = tl.make_block_ptr(ht + i_bh * K*V, (K, V), (V, 1), (i_k * BK, i_v * BV), (BK, BV), (1, 0))
        tl.store(p_ht, b_h.to(p_ht.dtype.element_ty), boundary_check=(0, 1))


@triton.autotune(
    configs=[
        triton.Config({}, num_warps=1),
        triton.Config({}, num_warps=2),
        triton.Config({}, num_warps=4),
        triton.Config({}, num_warps=8),
    ],
    key=["BT", "BK", "BV",  "USE_G", 'USE_GK', 'USE_GV'],
)
@triton.heuristics({
    'STORE_INITIAL_STATE_GRADIENT': lambda args: args['dh0'] is not None,
    'USE_FINAL_STATE_GRADIENT': lambda args: args['dht'] is not None
})
@triton.jit
def chunk_bwd_kernel_dh(
    q,
    g,
    gk,
    gv,
    do,
    dh,
    dht,
    dh0,
    scale,
    T: tl.constexpr,
    HQ: tl.constexpr,
    H: tl.constexpr,
    K: tl.constexpr,
    V: tl.constexpr,
    BT: tl.constexpr,
    BK: tl.constexpr,
    BV: tl.constexpr,
    NT: tl.constexpr,
    NG: tl.constexpr,
    USE_G: tl.constexpr,
    USE_GK: tl.constexpr,
    USE_GV: tl.constexpr,
    STORE_INITIAL_STATE_GRADIENT: tl.constexpr,
    USE_FINAL_STATE_GRADIENT: tl.constexpr,
    HEAD_FIRST: tl.constexpr
):
    i_k, i_v, i_bh = tl.program_id(0), tl.program_id(1), tl.program_id(2)
    i_bg = i_bh // NG
    i_b, i_h = i_bh // H, i_bh % H
    i_hg = i_h // NG
    # [BK, BV]
    b_dh = tl.zeros([BK, BV], dtype=tl.float32)
    if USE_FINAL_STATE_GRADIENT:
        p_dht = tl.make_block_ptr(dht + i_bh * K*V, (K, V), (V, 1), (i_k * BK, i_v * BV), (BK, BV), (1, 0))
        b_dh += tl.load(p_dht, boundary_check=(0, 1)).to(tl.float32)

    for i_t in range(NT - 1, -1, -1):
        p_dh = tl.make_block_ptr(dh + i_bh * NT*K*V + i_t * K*V, (K, V), (V, 1), (i_k * BK, i_v * BV), (BK, BV), (1, 0))
        tl.store(p_dh, b_dh.to(p_dh.dtype.element_ty), boundary_check=(0, 1))
        last_idx = min(i_t * BT + BT, T) - 1
        # [BK, BT]
        if HEAD_FIRST:
            p_q = tl.make_block_ptr(q + i_bh * T*K, (K, T), (1, K), (i_k * BK, i_t * BT), (BK, BT), (0, 1))
            p_do = tl.make_block_ptr(do + i_bh * T*V, (T, V), (V, 1), (i_t * BT, i_v * BV), (BT, BV), (1, 0))
        else:
            p_q = tl.make_block_ptr(q + i_b * T*HQ*K + i_h * K, (K, T), (1, HQ*K), (i_k * BK, i_t * BT), (BK, BT), (0, 1))
            p_do = tl.make_block_ptr(do + i_b * T*HQ*V + i_h * V, (T, V), (HQ*V, 1), (i_t * BT, i_v * BV), (BT, BV), (1, 0))
        b_q = tl.load(p_q, boundary_check=(0, 1))
        b_q = (b_q * scale).to(b_q.dtype)
        # [BT, BV]
        b_do = tl.load(p_do, boundary_check=(0, 1))

        if USE_G:
            if HEAD_FIRST:
                p_g = g + i_bg * T + i_t * BT + tl.arange(0, BT)
                p_g = tl.max_contiguous(tl.multiple_of(p_g, BT), BT)
                b_g_last = tl.load(g + i_bg * T + last_idx)
            else:
                p_g = g + i_b * T*H + (i_t * BT + tl.arange(0, BT)) * H + i_hg
                b_g_last = tl.load(g + i_b * T * H + last_idx * H + i_hg)
            b_g = tl.load(p_g, mask=(i_t * BT + tl.arange(0, BT) < T), other=0.)
            b_q = (b_q * tl.exp(b_g)[None, :]).to(b_q.dtype)

            b_dh *= tl.exp(b_g_last)

        if USE_GK:
            if HEAD_FIRST:
                p_gk = tl.make_block_ptr(gk + i_bg * T*K, (K, T), (1, K), (i_k * BK, i_t * BT), (BK, BT), (0, 1))
                p_gk_last = gk + i_bg * T*K + last_idx * K + i_k * BK + tl.arange(0, BK)

            else:
                p_gk = tl.make_block_ptr(gk + i_b * T*H*K + i_hg * K, (K, T), (1, H*K), (i_k * BK, i_t * BT), (BK, BT), (0, 1))
                p_gk_last = gk + i_b * T*H*K + last_idx * H*K + i_hg * K + i_k * BK + tl.arange(0, BK)
            p_gk_last = tl.max_contiguous(tl.multiple_of(p_gk_last, BK), BK)

            b_gk = tl.load(p_gk, boundary_check=(0, 1))
            b_q = (b_q * tl.exp(b_gk)).to(b_q.dtype)
            b_gk_last = tl.load(p_gk_last, mask=(i_k * BK + tl.arange(0, BK) < K), other=0.)
            b_dh *= tl.exp(b_gk_last)[:, None]

        if USE_GV:
            if HEAD_FIRST:
                p_gv = tl.make_block_ptr(gv + i_bg * T*V, (T, V), (V, 1), (i_t * BT, i_v * BV), (BT, BV), (1, 0))
                p_gv_last = gv + i_bg * T*V + last_idx * V + i_v * BV + tl.arange(0, BV)
            else:
                p_gv = tl.make_block_ptr(gv + i_b * T*H*V + i_hg * V, (T, V), (H*V, 1), (i_t * BT, i_v * BV), (BT, BV), (1, 0))
                p_gv_last = gv + i_b * T*H*V + last_idx * H*V + i_hg * V + i_v * BV + tl.arange(0, BV)
            p_gv_last = tl.max_contiguous(tl.multiple_of(p_gv_last, BV), BV)

            b_gv = tl.load(p_gv, boundary_check=(0, 1))
            b_do = (b_do * tl.exp(b_gv)).to(b_do.dtype)

            b_gv_last = tl.load(p_gv_last, mask=(i_v * BV + tl.arange(0, BV) < V), other=0.)
            b_dh *= tl.exp(b_gv_last)[None, :]

        b_dh += tl.dot(b_q, b_do)

    if STORE_INITIAL_STATE_GRADIENT:
        p_dh0 = tl.make_block_ptr(dh0 + i_bh * K*V, (K, V), (V, 1), (i_k * BK, i_v * BV), (BK, BV), (1, 0))
        tl.store(p_dh0, b_dh.to(p_dh0.dtype.element_ty), boundary_check=(0, 1))


def chunk_fwd_h(
    k: torch.Tensor,
    v: torch.Tensor,
    g: torch.Tensor,
    gk: torch.Tensor,
    gv: torch.Tensor,
    h0: torch.Tensor,
    output_final_state: bool,
    states_in_fp32: bool = False,
    head_first: bool = True,
    chunk_size: int = 64
) -> Tuple[torch.Tensor, torch.Tensor]:
    if head_first:
        B, H, T, K, V = *k.shape, v.shape[-1]
    else:
        B, T, H, K, V = *k.shape, v.shape[-1]
    BT = chunk_size
    BK, BV = min(64, triton.next_power_of_2(K)), min(64, triton.next_power_of_2(V))
    NT, NK, NV = triton.cdiv(T, BT), triton.cdiv(K, BK), triton.cdiv(V, BV)

    h = k.new_empty(B, H, NT * K, V, dtype=k.dtype if not states_in_fp32 else torch.float32)
    ht = k.new_empty(B, H, K, V, dtype=torch.float32) if output_final_state else None

    chunk_fwd_kernel_h[(NK, NV, B * H)](
        k=k,
        v=v,
        h=h,
        g=g,
        gk=gk,
        gv=gv,
        h0=h0,
        ht=ht,
        T=T,
        H=H,
        K=K,
        V=V,
        BT=BT,
        BK=BK,
        BV=BV,
        NT=NT,
        USE_G=g is not None,
        USE_GK=gk is not None,
        USE_GV=gv is not None,
        HEAD_FIRST=head_first
    )
    return h, ht


def chunk_bwd_dh(
    q: torch.Tensor,
    k: torch.Tensor,
    v: torch.Tensor,
    g: torch.Tensor,
    gk: torch.Tensor,
    gv: torch.Tensor,
    do: torch.Tensor,
    h0: torch.Tensor,
    dht: torch.Tensor,
    scale: float,
    states_in_fp32: bool = False,
    head_first: bool = True,
    chunk_size: int = 64
) -> Tuple[torch.Tensor, torch.Tensor]:
    if head_first:
        B, H, T, K, V = *k.shape, v.shape[-1]
        HQ = q.shape[1]
    else:
        B, T, H, K, V = *k.shape, v.shape[-1]
        HQ = q.shape[2]
    BT = chunk_size
    BK = min(triton.next_power_of_2(K), 64)
    BV = min(triton.next_power_of_2(V), 64)
    NT, NK, NV = triton.cdiv(T, BT), triton.cdiv(K, BK), triton.cdiv(V, BV)
    # number of groups in GQA
    NG = HQ // H

    dh = k.new_empty(B, HQ, NT * K, V, dtype=k.dtype if not states_in_fp32 else torch.float32)
    dh0 = torch.empty_like(h0, dtype=torch.float32) if h0 is not None else None
    chunk_bwd_kernel_dh[(NK, NV, B * HQ)](
        q=q,
        g=g,
        gk=gk,
        gv=gv,
        do=do,
        dh=dh,
        dht=dht,
        dh0=dh0,
        scale=scale,
        T=T,
        HQ=HQ,
        H=H,
        K=K,
        V=V,
        BT=BT,
        BK=BK,
        BV=BV,
        NT=NT,
        NG=NG,
        USE_G=g is not None,
        USE_GK=gk is not None,
        USE_GV=gv is not None,
        HEAD_FIRST=head_first,
    )
    return dh, dh0
