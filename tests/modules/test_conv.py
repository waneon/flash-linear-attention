# -*- coding: utf-8 -*-

import pytest
import torch

from fla.modules.convolution import ShortConvolution


def get_abs_err(x, y):
    return (x-y).flatten().abs().max().item()


def get_err_ratio(x, y):
    err = (x-y).flatten().square().mean().sqrt().item()
    base = (x).flatten().square().mean().sqrt().item()
    return err / (base + 1e-10)


def assert_close(prefix, ref, tri, ratio):
    msg = f"{prefix} diff: {get_abs_err(ref, tri):.6f} ratio: {get_err_ratio(ref, tri):.6f}"
    print(msg)
    assert get_err_ratio(ref, tri) < ratio, msg


@pytest.mark.parametrize("B", [4])
@pytest.mark.parametrize("T", [100, 500, 1])
@pytest.mark.parametrize("H", [128])
@pytest.mark.parametrize("C", [4])
def test_shortconv(B: int, T: int, H: int, C: int):
    torch.manual_seed(42)
    conv_slow = ShortConvolution(H, C, activation='silu', use_fast_conv1d=False).cuda()
    conv_fast = ShortConvolution(H, C, activation='silu', use_fast_conv1d=True).cuda()
    conv_fast.weight.data.copy_(conv_slow.weight.data)
    if conv_fast.bias is not None:
        conv_fast.bias.data.copy_(conv_slow.bias.data)

    x = torch.randn(B, T, H).cuda()
    y_slow, _ = conv_slow(x)
    y_fast, _ = conv_fast(x)
    assert y_slow.shape == x.shape
    assert y_fast.shape == x.shape
    assert torch.allclose(y_slow, y_fast), f"{y_slow}\n{y_fast}"


@pytest.mark.parametrize("B", [4])
@pytest.mark.parametrize("T", [100])
@pytest.mark.parametrize("H", [16])
@pytest.mark.parametrize("C", [4])
def test_shortconv_cache(B: int, T: int, H: int, C: int):
    torch.manual_seed(42)
    conv_slow = ShortConvolution(H, C, use_fast_conv1d=False).cuda()
    conv_fast = ShortConvolution(H, C, use_fast_conv1d=True).cuda()
    conv_fast.weight.data.copy_(conv_slow.weight.data)
    if conv_fast.bias is not None:
        conv_fast.bias.data.copy_(conv_slow.bias.data)

    x = torch.randn(B, T, H).cuda()
    mask = torch.randint(T//3, T, (B,)).unsqueeze(-1).gt(torch.arange(T).flip(-1)).bool().cuda()
    y, _ = conv_slow(x, mask=mask)
    cache_slow, cache_fast = None, None
    for i in range(T):
        y_slow, cache_slow = conv_slow(x[:, i:i+1], mask=mask[:, i:i+1], cache=cache_slow, output_final_state=True)
        y_fast, cache_fast = conv_fast(x[:, i:i+1], mask=mask[:, i:i+1], cache=cache_fast, output_final_state=True)

        assert_close(f" slow {i:2}", y_slow, y[:, i:i+1], 1e-5)
        assert_close(f" fast {i:2}", y_fast, y[:, i:i+1], 1e-5)
        assert_close(f"cache {i:2}", cache_slow, cache_fast, 1e-5)
