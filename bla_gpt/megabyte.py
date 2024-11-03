from __future__ import annotations

import functools
import math
from dataclasses import dataclass
from functools import wraps
from itertools import zip_longest

import torch
import torch.nn.functional as F
from beartype import beartype
from coqpit import Coqpit
from einops import pack, rearrange, repeat, unpack
from einops.layers.torch import Rearrange
from packaging import version
from torch import einsum, nn
from torch.amp import autocast
from torch.nn import Module, ModuleList
from torch.nn.attention import SDPBackend
from tqdm import tqdm
from utils import register_model

# helpers


def exists(val):
    return val is not None


def once(fn):
    called = False

    @wraps(fn)
    def inner(x):
        nonlocal called
        if called:
            return
        called = True
        return fn(x)

    return inner


print_once = once(print)

# main class


class Attend(nn.Module):
    def __init__(self, causal=False, dropout=0.0, flash=False):
        super().__init__()
        self.dropout = dropout
        self.attn_dropout = nn.Dropout(dropout)

        self.causal = causal
        self.flash = flash
        assert not (
            flash and version.parse(torch.__version__) < version.parse("2.0.0")
        ), "in order to use flash attention, you must be using pytorch 2.0 or above"

        # default cpu attention configs
        self.attn_cfg = [
            SDPBackend.FLASH_ATTENTION,
            SDPBackend.MATH,
            SDPBackend.EFFICIENT_ATTENTION,
        ]

        if not torch.cuda.is_available() or not flash:
            return

        device_properties = torch.cuda.get_device_properties(torch.device("cuda"))

        if device_properties.major == 8 and device_properties.minor == 0:
            print_once(
                "A100 GPU detected, using flash attention if input tensor is on cuda"
            )
            self.attn_cfg = [SDPBackend.FLASH_ATTENTION]
        else:
            print_once(
                "Non-A100 GPU detected, using math or mem efficient attention if input tensor is on cuda"
            )
            self.attn_cfg = [SDPBackend.MATH, SDPBackend.EFFICIENT_ATTENTION]

    def get_mask(self, i, j, device):
        return torch.ones((i, j), device=device, dtype=torch.bool).triu(j - i + 1)

    def flash_attn(self, q, k, v, mask=None, attn_bias=None):
        _, heads, q_len, _, k_len, is_cuda, device = (
            *q.shape,
            k.shape[-2],
            q.is_cuda,
            q.device,
        )

        # single headed key / values

        if k.ndim == 3:
            k = rearrange(k, "b n d -> b 1 n d")

        if v.ndim == 3:
            v = rearrange(v, "b n d -> b 1 n d")

        # Check if mask exists and expand to compatible shape
        # The mask is B L, so it would have to be expanded to B H N L

        if exists(mask) and mask.ndim != 4:
            mask = rearrange(mask, "b j -> b 1 1 j")
            mask = mask.expand(-1, heads, q_len, -1)

        # pytorch 2.0 flash attn: q, k, v, mask, dropout, causal, softmax_scale

        with torch.nn.attention.sdpa_kernel(self.attn_cfg):
            out = F.scaled_dot_product_attention(
                q,
                k,
                v,
                attn_mask=mask,
                dropout_p=self.dropout if self.training else 0.0,
                is_causal=self.causal,
            )
        return out

    def forward(self, q, k, v, mask=None):
        """
        einstein notation
        b - batch
        h - heads
        n, i, j - sequence length (base sequence length, source, target)
        d - feature dimension
        """

        q_len, k_len, device = q.shape[-2], k.shape[-2], q.device

        scale = q.shape[-1] ** -0.5

        kv_einsum_eq = "b j d" if k.ndim == 3 else "b h j d"

        if self.flash:
            return self.flash_attn(q, k, v, mask=mask)

        # similarity

        sim = einsum(f"b h i d, {kv_einsum_eq} -> b h i j", q, k) * scale

        # causal mask

        if self.causal:
            causal_mask = self.get_mask(q_len, k_len, device)
            sim = sim.masked_fill(causal_mask, -torch.finfo(sim.dtype).max)

        # attention

        attn = sim.softmax(dim=-1)
        attn = self.attn_dropout(attn)

        # aggregate values

        out = einsum(f"b h i j, {kv_einsum_eq} -> b h i d", attn, v)

        return out


# helpers


def exists(val):
    return val is not None


def default(val, d):
    return val if exists(val) else d


def pack_one(t, pattern):
    return pack([t], pattern)


def unpack_one(t, ps, pattern):
    return unpack(t, ps, pattern)[0]


def remainder_to_mult(num, mult):
    return (mult - num % mult) % mult


def cast_tuple(t, length=1):
    return t if isinstance(t, tuple) else ((t,) * length)


def reduce_mult(nums):
    return functools.reduce(lambda x, y: x * y, nums, 1)


# tensor helpers


def log(t, eps=1e-20):
    return torch.log(t.clamp(min=eps))


def gumbel_noise(t):
    noise = torch.zeros_like(t).uniform_(0, 1)
    return -log(-log(noise))


def gumbel_sample(t, temperature=1.0, dim=-1):
    return ((t / temperature) + gumbel_noise(t)).argmax(dim=dim)


def top_k(logits, thres=0.5):
    num_logits = logits.shape[-1]
    k = max(int((1 - thres) * num_logits), 1)
    val, ind = torch.topk(logits, k)
    probs = torch.full_like(logits, float("-inf"))
    probs.scatter_(1, ind, val)
    return probs


# token shift, from Peng et al of RWKV


def token_shift(t):
    t, t_shift = t.chunk(2, dim=-1)
    t_shift = F.pad(t_shift, (0, 0, 1, -1))
    return torch.cat((t, t_shift), dim=-1)


# rotary positional embedding


class RotaryEmbedding(Module):
    def __init__(self, dim, theta=10000):
        super().__init__()
        inv_freq = 1.0 / (theta ** (torch.arange(0, dim, 2).float() / dim))
        self.register_buffer("inv_freq", inv_freq)

    @property
    def device(self):
        return next(self.buffers()).device

    @autocast("cuda", enabled=False)
    def forward(self, seq_len):
        t = torch.arange(seq_len, device=self.device).type_as(self.inv_freq)
        freqs = torch.einsum("i , j -> i j", t, self.inv_freq)
        freqs = torch.cat((freqs, freqs), dim=-1)
        return freqs


def rotate_half(x):
    x1, x2 = x.chunk(2, dim=-1)
    return torch.cat((-x2, x1), dim=-1)


@autocast("cuda", enabled=False)
def apply_rotary_pos_emb(pos, t):
    return t * pos.cos() + rotate_half(t) * pos.sin()


# norm


class RMSNorm(Module):
    def __init__(self, dim, eps=1e-8):
        super().__init__()
        self.scale = dim**-0.5
        self.eps = eps
        self.g = nn.Parameter(torch.ones(dim))

    def forward(self, x):
        norm = torch.norm(x, dim=-1, keepdim=True) * self.scale
        return x / norm.clamp(min=self.eps) * self.g


# helper classes


def FeedForward(*, dim, mult=4, dropout=0.0):
    return nn.Sequential(
        RMSNorm(dim),
        nn.Linear(dim, dim * mult),
        nn.GELU(),
        nn.Dropout(dropout),
        nn.Linear(dim * mult, dim),
    )


class Attention(Module):
    def __init__(self, *, dim, dim_head=64, heads=8, dropout=0.0, flash=False):
        super().__init__()
        self.scale = dim_head**-0.5
        self.heads = heads
        inner_dim = dim_head * heads

        self.attend = Attend(causal=True, flash=flash, dropout=dropout)

        self.dropout = nn.Dropout(dropout)
        self.norm = RMSNorm(dim)
        self.to_q = nn.Linear(dim, inner_dim, bias=False)
        self.to_kv = nn.Linear(dim, inner_dim * 2, bias=False)
        self.to_out = nn.Linear(inner_dim, dim, bias=False)

    def forward(self, x, rotary_emb=None):
        h, device = self.heads, x.device

        x = self.norm(x)
        q, k, v = (self.to_q(x), *self.to_kv(x).chunk(2, dim=-1))
        q, k, v = map(lambda t: rearrange(t, "b n (h d) -> b h n d", h=h), (q, k, v))

        if exists(rotary_emb):
            q, k = map(lambda t: apply_rotary_pos_emb(rotary_emb, t), (q, k))

        out = self.attend(q, k, v)

        out = rearrange(out, "b h n d -> b n (h d)")
        return self.to_out(out)


class Transformer(Module):
    def __init__(
        self,
        *,
        dim,
        layers,
        dim_head=64,
        heads=8,
        attn_dropout=0.0,
        ff_dropout=0.0,
        ff_mult=4,
        rel_pos=True,
        flash_attn=False,
    ):
        super().__init__()
        self.rotary_emb = RotaryEmbedding(dim_head) if rel_pos else None
        self.layers = ModuleList([])

        for _ in range(layers):
            self.layers.append(
                ModuleList(
                    [
                        Attention(
                            dim=dim,
                            dim_head=dim_head,
                            heads=heads,
                            dropout=attn_dropout,
                            flash=flash_attn,
                        ),
                        FeedForward(dim=dim, mult=ff_mult, dropout=ff_dropout),
                    ]
                )
            )

        self.norm = RMSNorm(dim)

    def forward(self, x):
        n = x.shape[-2]
        rotary_emb = self.rotary_emb(n) if exists(self.rotary_emb) else None

        for attn, ff in self.layers:
            x = attn(token_shift(x), rotary_emb=rotary_emb) + x
            x = ff(token_shift(x)) + x

        return self.norm(x)


# main class


@dataclass
class MegaByteConfig(Coqpit):
    num_tokens: int = 50305
    dim: tuple | int = (768, 256)
    depth: tuple = (10, 2)
    max_seq_len: tuple = (256, 4)
    dim_head: int = 64
    heads: int = 12
    attn_dropout: float = 0.0
    ff_mult: int = 4
    ff_dropout: float = 0.0
    pad_id: int = 50304
    rel_pos: bool = False
    pos_emb: bool = False
    flash_attn: bool = True


class MegaByte(Module):
    @beartype
    def __init__(self, config: MegaByteConfig):
        super().__init__()

        # simplified configuration for each stage of the hierarchy
        # depth = (2, 2, 4) would translate to depth 2 at first stage, depth 2 second stage, depth 4 third
        # max_seq_len = (16, 8, 4) would translate to max sequence length of 16 at first stage, length of 8 at second stage, length of 4 for last

        self.config = config

        # pass all config values to the class as attributes
        num_tokens = config.num_tokens
        dim = config.dim
        depth = config.depth
        max_seq_len = config.max_seq_len
        dim_head = config.dim_head
        heads = config.heads
        attn_dropout = config.attn_dropout
        ff_mult = config.ff_mult
        ff_dropout = config.ff_dropout
        pad_id = config.pad_id
        rel_pos = config.rel_pos
        pos_emb = config.pos_emb
        flash_attn = config.flash_attn

        assert isinstance(depth, tuple) and isinstance(max_seq_len, tuple)
        assert len(depth) == len(max_seq_len)

        self.stages = len(depth)
        dim = cast_tuple(dim, self.stages)

        assert len(dim) == self.stages

        *_, fine_dim = dim

        self.max_seq_len = max_seq_len

        self.start_tokens = nn.ParameterList(
            [
                nn.Parameter(torch.randn(h_dim))
                for h_dim, seq_len in zip(dim, max_seq_len)
            ]
        )
        self.pos_embs = (
            ModuleList(
                [
                    nn.Embedding(seq_len, h_dim)
                    for h_dim, seq_len in zip(dim, max_seq_len)
                ]
            )
            if pos_emb
            else None
        )

        self.token_embs = ModuleList([])

        patch_size = 1
        self.token_embs.append(nn.Embedding(num_tokens, fine_dim))

        for dim_out, seq_len in zip(reversed(dim[:-1]), reversed(max_seq_len[1:])):
            patch_size *= seq_len

            self.token_embs.append(
                nn.Sequential(
                    nn.Embedding(num_tokens, fine_dim),
                    Rearrange("... r d -> ... (r d)"),
                    nn.LayerNorm(patch_size * fine_dim),
                    nn.Linear(patch_size * fine_dim, dim_out),
                    nn.LayerNorm(dim_out),
                )
            )

        self.transformers = ModuleList([])
        self.to_next_transformer_projections = ModuleList([])

        for h_dim, next_h_dim, stage_depth, next_seq_len in zip_longest(
            dim, dim[1:], depth, max_seq_len[1:]
        ):
            self.transformers.append(
                Transformer(
                    dim=h_dim,
                    layers=stage_depth,
                    dim_head=dim_head,
                    heads=heads,
                    attn_dropout=attn_dropout,
                    ff_dropout=ff_dropout,
                    ff_mult=ff_mult,
                    rel_pos=rel_pos,
                    flash_attn=flash_attn,
                )
            )

            proj = nn.Identity()

            if exists(next_h_dim):
                proj = nn.Sequential(
                    Rearrange("b ... d -> b (...) d"),
                    nn.Linear(h_dim, next_h_dim * next_seq_len),
                    Rearrange("b m (n d) -> (b m) n d", n=next_seq_len),
                )

            self.to_next_transformer_projections.append(proj)

        self.to_logits = nn.Linear(fine_dim, num_tokens)
        self.pad_id = pad_id

    def generate(
        self, prime=None, filter_thres=0.9, temperature=1.0, default_batch_size=1
    ):
        total_seq_len = reduce_mult(self.max_seq_len)
        device = next(self.parameters()).device

        if not exists(prime):
            prime = torch.empty(
                (default_batch_size, 0), dtype=torch.long, device=device
            )

        seq = prime
        batch = seq.shape[0]

        for _ in tqdm(range(total_seq_len - seq.shape[-1])):
            logits = self.forward(seq)[:, -1]
            logits = top_k(logits, thres=filter_thres)
            sampled = gumbel_sample(logits, dim=-1, temperature=temperature)
            seq = torch.cat((seq, rearrange(sampled, "b -> b 1")), dim=-1)

        return seq.reshape(batch, *self.max_seq_len)

    def forward_empty(self, batch_size):
        # take care of special case
        # where you sample from input of 0 (start token only)

        prev_stage_tokens_repr = None

        for stage_start_tokens, transformer, proj in zip(
            self.start_tokens, self.transformers, self.to_next_transformer_projections
        ):
            tokens = repeat(stage_start_tokens, "d -> b 1 d", b=batch_size)

            if exists(prev_stage_tokens_repr):
                tokens = tokens + prev_stage_tokens_repr[..., : tokens.shape[-2], :]

            tokens = transformer(tokens)
            prev_stage_tokens_repr = proj(tokens)

        return self.to_logits(tokens)

    def forward(self, ids, targets, return_loss=True):
        # we need to remove the sos token and pull the last token from the target
        # megabyte handles the sos token internally

        ids = ids[:, :-1]
        ids[:, :-1] = ids[:, 1:]
        ids = torch.cat((ids, targets[:, -1:]), dim=-1)  # [b, t_main]

        batch = ids.shape[0]

        assert ids.ndim in {2, self.stages + 1}
        flattened_dims = ids.ndim == 2

        if ids.numel() == 0:
            return self.forward_empty(ids.shape[0])

        if flattened_dims:
            # allow for ids to be given in the shape of (batch, seq)
            # in which case it will be auto-padded to the next nearest multiple of depth seq len
            seq_len = ids.shape[-1]
            multiple_of = reduce_mult(self.max_seq_len[1:])
            padding = remainder_to_mult(seq_len, multiple_of)
            ids = F.pad(ids, (0, padding), value=self.pad_id)
            ids = ids.reshape(
                batch, -1, *self.max_seq_len[1:]
            )  #  [b, t1, t2] t1*t2 = t_main

        b, *prec_dims, device = *ids.shape, ids.device

        # check some dimensions

        assert (
            prec_dims[0] <= self.max_seq_len[0]
        ), f"the first dimension of your axial autoregressive transformer must be less than the first tuple element of max_seq_len (like any autoregressive transformer) {prec_dims[0]}"
        assert tuple(prec_dims[1:]) == tuple(
            self.max_seq_len[1:]
        ), "all subsequent dimensions must match exactly"

        # get tokens for all hierarchical stages, reducing by appropriate dimensions
        # and adding the absolute positional embeddings

        num_stages = len(prec_dims)

        tokens_at_stages = []
        pos_embs = default(self.pos_embs, (None,) * num_stages)

        for ind, pos_emb, token_emb in zip_longest(
            range(num_stages), reversed(pos_embs), self.token_embs
        ):
            is_first = ind == 0

            tokens = token_emb(ids)

            if exists(pos_emb):
                positions = pos_emb(torch.arange(tokens.shape[-2], device=device))
                tokens = tokens + positions

            tokens_at_stages.insert(0, tokens)

            if is_first:
                continue

            ids = rearrange(ids, "... m n -> ... (m n)")

        # the un-pixelshuffled representations of the previous hierarchy, starts with None

        prev_stage_tokens_repr = None

        # spatial tokens is tokens with depth pos reduced along depth dimension + spatial positions

        for stage_start_tokens, stage_tokens, transformer, proj in zip(
            self.start_tokens,
            tokens_at_stages,
            self.transformers,
            self.to_next_transformer_projections,
        ):
            stage_tokens, ps = pack_one(stage_tokens, "* n d")
            stage_start_tokens = repeat(
                stage_start_tokens, "f -> b 1 f", b=stage_tokens.shape[0]
            )

            # concat start token

            stage_tokens = torch.cat(
                (
                    stage_start_tokens,
                    stage_tokens,
                ),
                dim=-2,
            )

            # sum the previous hierarchy's representation

            if exists(prev_stage_tokens_repr):
                prev_stage_tokens_repr = F.pad(
                    prev_stage_tokens_repr, (0, 0, 1, 0), value=0.0
                )
                stage_tokens = stage_tokens + prev_stage_tokens_repr

            attended = transformer(stage_tokens)

            attended = unpack_one(attended, ps, "* n d")

            # project for next stage in the hierarchy

            prev_stage_tokens_repr = proj(attended[..., :-1, :])

        # project to logits
        logits = self.to_logits(attended)  # [b, t/p, n, num_tokens]

        start_tokens = logits[(slice(None), *((0,) * (logits.ndim - 2)), slice(None))]
        start_tokens = rearrange(start_tokens, "b d -> b 1 d")

        logits = logits[..., 1:, :]

        if not return_loss:
            if flattened_dims:
                logits = rearrange(logits, "b ... c -> b (...) c")
                logits = logits[:, :seq_len]

            return logits

        logits = rearrange(logits, "b ... c -> b (...) c")
        logits = torch.cat((start_tokens, logits), dim=-2)

        preds = rearrange(logits, "b n c -> b c n")
        labels = rearrange(ids, "b ... -> b (...)")
        loss = F.cross_entropy(preds[..., :-1], labels, ignore_index=self.pad_id)

        return logits, loss


@register_model
def register_megabyte():
    return MegaByteConfig, MegaByte


if __name__ == "__main__":
    config = MegaByteConfig()
    model = MegaByte(config)
    print(model)
    print("done")

    ids = torch.randint(0, 50305, (1, 257))
    x = ids[:, :-1]
    y = ids[:, 1:]
    logits, loss = model(x, y)
    print(logits.shape, loss)