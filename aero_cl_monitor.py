#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Aerodrome Slipstream(CL) 仓位监控脚本（多池版本）

用途:
1) 根据 tokenId(也就是仓位 NFT ID) 读取仓位的当前 token0 / token1 数量
2) 若某一边数量为 0，则认为该仓位已经到边界/出区间，触发提醒
3) 支持同时监控多个 pool，每个 pool 对应自己的 gauge / tokenIds / owner
4) 支持通过 gauge + owner 自动发现已质押在 gauge 里的 tokenId
5) 支持 Telegram / Webhook 报警

依赖:
    pip install web3 requests

使用方法:
    直接修改脚本顶部“用户配置”里的变量，然后运行：
    python aero_cl_monitor_multi.py

说明:
- 这个脚本算的是“当前流动性本金在两边 token 中的分布”，对应 UI 里的 Staked 数量。
- 不包含未领取的 Trading Fees / Emissions。
- Aerodrome Slipstream 的 CL 仓位是 NFT；要定位具体仓位，最关键的是 tokenId。
- 只有 pool address / gauge address 还不够，因为同一个池子里可以有很多不同区间的仓位。
"""

from __future__ import annotations

import json
import sys
import time
from dataclasses import dataclass
from decimal import Decimal, getcontext
from typing import Dict, List, Optional, Sequence, Tuple

import requests
from fun.base_init import w3
from web3.contract import Contract

# 显示数字用，高一点避免精度问题
from fun.beep import beep_notice
from fun.tlg_bot import send_message, CHAT_ID_SELF
from fun.overwritePrint import print

getcontext().prec = 80

# -----------------------------
# Minimal ABIs
# -----------------------------
ERC20_ABI = json.loads(
    """[
      {"inputs":[],"name":"symbol","outputs":[{"internalType":"string","name":"","type":"string"}],"stateMutability":"view","type":"function"},
      {"inputs":[],"name":"decimals","outputs":[{"internalType":"uint8","name":"","type":"uint8"}],"stateMutability":"view","type":"function"}
    ]"""
)

POOL_ABI = json.loads(
    """[
      {"inputs":[],"name":"token0","outputs":[{"internalType":"address","name":"","type":"address"}],"stateMutability":"view","type":"function"},
      {"inputs":[],"name":"token1","outputs":[{"internalType":"address","name":"","type":"address"}],"stateMutability":"view","type":"function"},
      {"inputs":[],"name":"tickSpacing","outputs":[{"internalType":"int24","name":"","type":"int24"}],"stateMutability":"view","type":"function"},
      {"inputs":[],"name":"slot0","outputs":[
        {"internalType":"uint160","name":"sqrtPriceX96","type":"uint160"},
        {"internalType":"int24","name":"tick","type":"int24"},
        {"internalType":"uint16","name":"observationIndex","type":"uint16"},
        {"internalType":"uint16","name":"observationCardinality","type":"uint16"},
        {"internalType":"uint16","name":"observationCardinalityNext","type":"uint16"},
        {"internalType":"bool","name":"unlocked","type":"bool"}
      ],"stateMutability":"view","type":"function"}
    ]"""
)

NPM_ABI = json.loads(
    """[
      {"inputs":[{"internalType":"uint256","name":"tokenId","type":"uint256"}],"name":"positions","outputs":[
        {"internalType":"uint96","name":"nonce","type":"uint96"},
        {"internalType":"address","name":"operator","type":"address"},
        {"internalType":"address","name":"token0","type":"address"},
        {"internalType":"address","name":"token1","type":"address"},
        {"internalType":"int24","name":"tickSpacing","type":"int24"},
        {"internalType":"int24","name":"tickLower","type":"int24"},
        {"internalType":"int24","name":"tickUpper","type":"int24"},
        {"internalType":"uint128","name":"liquidity","type":"uint128"},
        {"internalType":"uint256","name":"feeGrowthInside0LastX128","type":"uint256"},
        {"internalType":"uint256","name":"feeGrowthInside1LastX128","type":"uint256"},
        {"internalType":"uint128","name":"tokensOwed0","type":"uint128"},
        {"internalType":"uint128","name":"tokensOwed1","type":"uint128"}
      ],"stateMutability":"view","type":"function"}
    ]"""
)

GAUGE_ABI = json.loads(
    """[
      {"inputs":[],"name":"pool","outputs":[{"internalType":"address","name":"","type":"address"}],"stateMutability":"view","type":"function"},
      {"inputs":[],"name":"nft","outputs":[{"internalType":"address","name":"","type":"address"}],"stateMutability":"view","type":"function"},
      {"inputs":[{"internalType":"address","name":"depositor","type":"address"}],"name":"stakedValues","outputs":[{"internalType":"uint256[]","name":"staked","type":"uint256[]"}],"stateMutability":"view","type":"function"},
      {"inputs":[{"internalType":"address","name":"depositor","type":"address"},{"internalType":"uint256","name":"tokenId","type":"uint256"}],"name":"stakedContains","outputs":[{"internalType":"bool","name":"","type":"bool"}],"stateMutability":"view","type":"function"}
    ]"""
)

# 官方安全页公布的 Aerodrome Base Slipstream NPM 地址（可作为 gauge 未提供时的兜底）
DEFAULT_AERODROME_NPM = w3.to_checksum_address("0x827922686190790b37229fd06084350E74485b72")

# =============================
# 用户配置：直接修改下面这些变量即可
# =============================

# 方式一：最推荐，按“对应下标”配置多个池子
# 第 0 个 pool 对应第 0 个 gauge / 第 0 组 tokenIds / 第 0 个 owner
POOL_ADDRESSES = [
    # "0x70acdf2ad0bf2402c957154f944c19ef4e1cbae1",
    "0x42d4a22cad0f5a49681a5715ce994af73a43b76b",
    "0x160d7e9d948b16c163332a277b393c288408eb12",
    "0x3fe04a59ebd38cf06080a6f60a98d124eb59392a",
]

GAUGE_ADDRESSES = [
    # "0x41b2126661C673C2beDd208cC72E85DC51a5320a",
    "0x61E0B10423a0009C3f83ab4313813d29437d0817",
    "0x7B0f1103746648FBfce9222f1266f79B934E16b2",
    "0xA0B61fdB9f1FB9b917Fe38b49427Fd4D87472D28",
]

# 每个 pool 对应一组 tokenIds
# 支持写成 [[8850, 9009], [12345, 12346]]
# 也支持 [["8850,9009"], ["12345,12346"]]
TOKEN_IDS_LIST = [
    # [65225012, 67359397],
    [87546],
    [185344],
    [191145],
]

# 可选：如果某个池子的仓位都 stake 在对应 gauge 里，也可以填 owner 自动发现
# 会与 TOKEN_IDS_LIST 对应位置的 tokenIds 合并去重
OWNER_ADDRESSES = [
    None,
    None,
    None,
]

# 轮询配置
INTERVAL_SECONDS = 60
RUN_ONCE = False

# 报警阈值：
# 0 = 严格等于 0 才报警
# >0 = 把极小 dust 也视为 0
ZERO_THRESHOLD_RAW = 0

# 通知配置：不填则只在控制台打印
TELEGRAM_BOT_TOKEN = ""
TELEGRAM_CHAT_ID = ""
WEBHOOK_URL = ""

Q96 = 1 << 96
Q128 = 1 << 128
UINT256_MAX = (1 << 256) - 1


@dataclass
class TokenMeta:
    address: str
    symbol: str
    decimals: int


@dataclass
class PositionReport:
    token_id: int
    token0: TokenMeta
    token1: TokenMeta
    tick_lower: int
    tick_upper: int
    current_tick: int
    liquidity: int
    sqrt_price_x96: int
    amount0_raw: int
    amount1_raw: int
    amount0: Decimal
    amount1: Decimal
    state: str
    zero_side: Optional[str]


@dataclass
class MonitorConfig:
    index: int
    pool_address: str
    gauge_address: Optional[str]
    owner_address: Optional[str]
    explicit_token_ids: List[int]


@dataclass
class PoolContext:
    index: int
    pool_address: str
    gauge_address: Optional[str]
    owner_address: Optional[str]
    position_manager_address: str
    token0_meta: TokenMeta
    token1_meta: TokenMeta
    explicit_token_ids: List[int]


# -----------------------------
# TickMath: Python 版，等价于官方 Solidity 逻辑
# 来源于 Uniswap V3 / Aerodrome Slipstream TickMath
# -----------------------------
def get_sqrt_ratio_at_tick(tick: int) -> int:
    if tick < -887272 or tick > 887272:
        raise ValueError(f"tick out of range: {tick}")

    abs_tick = -tick if tick < 0 else tick

    ratio = 0xFFFcb933BD6FAD37AA2D162D1A594001 if (abs_tick & 0x1) else 0x100000000000000000000000000000000
    if abs_tick & 0x2:
        ratio = (ratio * 0xFFF97272373D413259A46990580E213A) >> 128
    if abs_tick & 0x4:
        ratio = (ratio * 0xFFF2E50F5F656932EF12357CF3C7FDCC) >> 128
    if abs_tick & 0x8:
        ratio = (ratio * 0xFFE5CACA7E10E4E61C3624EAA0941CD0) >> 128
    if abs_tick & 0x10:
        ratio = (ratio * 0xFFCB9843D60F6159C9DB58835C926644) >> 128
    if abs_tick & 0x20:
        ratio = (ratio * 0xFF973B41FA98C081472E6896DFB254C0) >> 128
    if abs_tick & 0x40:
        ratio = (ratio * 0xFF2EA16466C96A3843EC78B326B52861) >> 128
    if abs_tick & 0x80:
        ratio = (ratio * 0xFE5DEE046A99A2A811C461F1969C3053) >> 128
    if abs_tick & 0x100:
        ratio = (ratio * 0xFCBE86C7900A88AEDCFFC83B479AA3A4) >> 128
    if abs_tick & 0x200:
        ratio = (ratio * 0xF987A7253AC413176F2B074CF7815E54) >> 128
    if abs_tick & 0x400:
        ratio = (ratio * 0xF3392B0822B70005940C7A398E4B70F3) >> 128
    if abs_tick & 0x800:
        ratio = (ratio * 0xE7159475A2C29B7443B29C7FA6E889D9) >> 128
    if abs_tick & 0x1000:
        ratio = (ratio * 0xD097F3BDFD2022B8845AD8F792AA5825) >> 128
    if abs_tick & 0x2000:
        ratio = (ratio * 0xA9F746462D870FDF8A65DC1F90E061E5) >> 128
    if abs_tick & 0x4000:
        ratio = (ratio * 0x70D869A156D2A1B890BB3DF62BAF32F7) >> 128
    if abs_tick & 0x8000:
        ratio = (ratio * 0x31BE135F97D08FD981231505542FCFA6) >> 128
    if abs_tick & 0x10000:
        ratio = (ratio * 0x09AA508B5B7A84E1C677DE54F3E99BC9) >> 128
    if abs_tick & 0x20000:
        ratio = (ratio * 0x005D6AF8DEDB81196699C329225EE604) >> 128
    if abs_tick & 0x40000:
        ratio = (ratio * 0x0002216E584F5FA1EA926041BEDFE98) >> 128
    if abs_tick & 0x80000:
        ratio = (ratio * 0x000048A170391F7DC42444E8FA2) >> 128

    if tick > 0:
        ratio = UINT256_MAX // ratio

    return (ratio >> 32) + (0 if (ratio & ((1 << 32) - 1)) == 0 else 1)


# -----------------------------
# LiquidityAmounts: Python 版，等价于官方 Solidity 逻辑
# -----------------------------
def get_amount0_for_liquidity(sqrt_ratio_a_x96: int, sqrt_ratio_b_x96: int, liquidity: int) -> int:
    if sqrt_ratio_a_x96 > sqrt_ratio_b_x96:
        sqrt_ratio_a_x96, sqrt_ratio_b_x96 = sqrt_ratio_b_x96, sqrt_ratio_a_x96
    return ((liquidity << 96) * (sqrt_ratio_b_x96 - sqrt_ratio_a_x96) // sqrt_ratio_b_x96) // sqrt_ratio_a_x96


def get_amount1_for_liquidity(sqrt_ratio_a_x96: int, sqrt_ratio_b_x96: int, liquidity: int) -> int:
    if sqrt_ratio_a_x96 > sqrt_ratio_b_x96:
        sqrt_ratio_a_x96, sqrt_ratio_b_x96 = sqrt_ratio_b_x96, sqrt_ratio_a_x96
    return liquidity * (sqrt_ratio_b_x96 - sqrt_ratio_a_x96) // Q96


def get_amounts_for_liquidity(
    sqrt_ratio_x96: int,
    sqrt_ratio_a_x96: int,
    sqrt_ratio_b_x96: int,
    liquidity: int,
) -> Tuple[int, int]:
    if sqrt_ratio_a_x96 > sqrt_ratio_b_x96:
        sqrt_ratio_a_x96, sqrt_ratio_b_x96 = sqrt_ratio_b_x96, sqrt_ratio_a_x96

    amount0 = 0
    amount1 = 0
    if sqrt_ratio_x96 <= sqrt_ratio_a_x96:
        amount0 = get_amount0_for_liquidity(sqrt_ratio_a_x96, sqrt_ratio_b_x96, liquidity)
    elif sqrt_ratio_x96 < sqrt_ratio_b_x96:
        amount0 = get_amount0_for_liquidity(sqrt_ratio_x96, sqrt_ratio_b_x96, liquidity)
        amount1 = get_amount1_for_liquidity(sqrt_ratio_a_x96, sqrt_ratio_x96, liquidity)
    else:
        amount1 = get_amount1_for_liquidity(sqrt_ratio_a_x96, sqrt_ratio_b_x96, liquidity)
    return amount0, amount1


# -----------------------------
# Helpers
# -----------------------------
def to_checksum(address: str) -> str:
    return w3.to_checksum_address(address)


def from_units(value: int, decimals: int) -> Decimal:
    return Decimal(value) / (Decimal(10) ** Decimal(decimals))


def format_decimal(value: Decimal, places: int = 8) -> str:
    q = Decimal(10) ** -places
    return f"{value.quantize(q):f}".rstrip("0").rstrip(".") or "0"


def safe_symbol(token: Contract) -> str:
    try:
        sym = token.functions.symbol().call()
        if isinstance(sym, bytes):
            return sym.rstrip(b"\x00").decode("utf-8", errors="ignore") or "UNKNOWN"
        return str(sym)
    except Exception:
        return "UNKNOWN"


def safe_decimals(token: Contract) -> int:
    try:
        return int(token.functions.decimals().call())
    except Exception as e:
        raise RuntimeError(f"读取 decimals 失败: {e}")


def load_token_meta(w3: w3, address: str) -> TokenMeta:
    c = w3.eth.contract(address=to_checksum(address), abi=ERC20_ABI)
    return TokenMeta(address=to_checksum(address), symbol=safe_symbol(c), decimals=safe_decimals(c))


def send_telegram(bot_token: str, chat_id: str, text: str) -> None:
    url = f"https://api.telegram.org/bot{bot_token}/sendMessage"
    resp = requests.post(url, json={"chat_id": chat_id, "text": text}, timeout=10)
    resp.raise_for_status()


def send_webhook(webhook_url: str, text: str) -> None:
    resp = requests.post(webhook_url, json={"text": text, "message": text}, timeout=10)
    resp.raise_for_status()


def notify(text: str) -> None:
    print(text)
    send_message(text, "", CHAT_ID_SELF)
    beep_notice()


def resolve_position_manager(w3: w3, gauge_address: Optional[str]) -> str:
    if gauge_address:
        gauge = w3.eth.contract(address=to_checksum(gauge_address), abi=GAUGE_ABI)
        return to_checksum(gauge.functions.nft().call())
    return DEFAULT_AERODROME_NPM


def fetch_staked_token_ids(w3: w3, gauge_address: str, owner_address: str) -> List[int]:
    gauge = w3.eth.contract(address=to_checksum(gauge_address), abi=GAUGE_ABI)
    ids = gauge.functions.stakedValues(to_checksum(owner_address)).call()
    return [int(x) for x in ids]


def validate_pool_vs_gauge(w3: w3, pool_address: str, gauge_address: Optional[str]) -> None:
    if not gauge_address:
        return
    gauge = w3.eth.contract(address=to_checksum(gauge_address), abi=GAUGE_ABI)
    gauge_pool = to_checksum(gauge.functions.pool().call())
    if gauge_pool != to_checksum(pool_address):
        raise ValueError(f"gauge.pool() = {gauge_pool}，与传入 pool = {to_checksum(pool_address)} 不一致")


def classify_position_state(sqrt_price_x96: int, tick_lower: int, tick_upper: int) -> str:
    sqrt_lower = get_sqrt_ratio_at_tick(tick_lower)
    sqrt_upper = get_sqrt_ratio_at_tick(tick_upper)
    if sqrt_price_x96 <= sqrt_lower:
        return "below_or_at_lower"
    if sqrt_price_x96 < sqrt_upper:
        return "in_range"
    return "above_or_at_upper"


def inspect_position(
    w3: w3,
    pool_address: str,
    position_manager_address: str,
    token_id: int,
    token0_meta: Optional[TokenMeta] = None,
    token1_meta: Optional[TokenMeta] = None,
) -> PositionReport:
    pool = w3.eth.contract(address=to_checksum(pool_address), abi=POOL_ABI)
    npm = w3.eth.contract(address=to_checksum(position_manager_address), abi=NPM_ABI)

    pos = npm.functions.positions(int(token_id)).call()
    pos_token0 = to_checksum(pos[2])
    pos_token1 = to_checksum(pos[3])
    pos_tick_spacing = int(pos[4])
    tick_lower = int(pos[5])
    tick_upper = int(pos[6])
    liquidity = int(pos[7])

    pool_token0 = to_checksum(pool.functions.token0().call())
    pool_token1 = to_checksum(pool.functions.token1().call())
    pool_tick_spacing = int(pool.functions.tickSpacing().call())

    if pos_token0 != pool_token0 or pos_token1 != pool_token1 or pos_tick_spacing != pool_tick_spacing:
        raise ValueError(
            f"tokenId={token_id} 不属于这个 pool。"
            f" position(token0={pos_token0}, token1={pos_token1}, tickSpacing={pos_tick_spacing})"
            f" vs pool(token0={pool_token0}, token1={pool_token1}, tickSpacing={pool_tick_spacing})"
        )

    slot0 = pool.functions.slot0().call()
    sqrt_price_x96 = int(slot0[0])
    current_tick = int(slot0[1])

    sqrt_lower = get_sqrt_ratio_at_tick(tick_lower)
    sqrt_upper = get_sqrt_ratio_at_tick(tick_upper)
    amount0_raw, amount1_raw = get_amounts_for_liquidity(sqrt_price_x96, sqrt_lower, sqrt_upper, liquidity)

    t0 = token0_meta or load_token_meta(w3, pos_token0)
    t1 = token1_meta or load_token_meta(w3, pos_token1)
    amount0 = from_units(amount0_raw, t0.decimals)
    amount1 = from_units(amount1_raw, t1.decimals)

    state = classify_position_state(sqrt_price_x96, tick_lower, tick_upper)
    zero_side = None
    if amount0_raw == 0:
        zero_side = t0.symbol
    elif amount1_raw == 0:
        zero_side = t1.symbol

    return PositionReport(
        token_id=int(token_id),
        token0=t0,
        token1=t1,
        tick_lower=tick_lower,
        tick_upper=tick_upper,
        current_tick=current_tick,
        liquidity=liquidity,
        sqrt_price_x96=sqrt_price_x96,
        amount0_raw=amount0_raw,
        amount1_raw=amount1_raw,
        amount0=amount0,
        amount1=amount1,
        state=state,
        zero_side=zero_side,
    )


def print_report(rep: PositionReport, pool_address: Optional[str] = None, monitor_index: Optional[int] = None) -> None:
    prefix_parts = []
    if monitor_index is not None:
        prefix_parts.append(f"monitor#{monitor_index}")
    if pool_address:
        prefix_parts.append(f"pool={pool_address}")
    prefix = f"[{' | '.join(prefix_parts)}] " if prefix_parts else ""

    print(
        f"{prefix}[tokenId={rep.token_id}] "
        f"{rep.token0.symbol}={format_decimal(rep.amount0, 8)} | "
        f"{rep.token1.symbol}={format_decimal(rep.amount1, 8)} | "
        f"tick={rep.current_tick} | range=[{rep.tick_lower}, {rep.tick_upper}] | state={rep.state}"
    )


def parse_token_ids(values: Optional[Sequence[object]]) -> List[int]:
    if not values:
        return []
    out: List[int] = []
    for v in values:
        if isinstance(v, int):
            out.append(v)
            continue
        s = str(v).strip()
        if not s:
            continue
        if "," in s:
            out.extend(int(x.strip()) for x in s.split(",") if x.strip())
        else:
            out.append(int(s))
    seen = set()
    uniq: List[int] = []
    for x in out:
        if x not in seen:
            uniq.append(x)
            seen.add(x)
    return uniq


def parse_optional_address_list(values: Optional[Sequence[object]], expect_len: int) -> List[Optional[str]]:
    vals = list(values or [])
    if not vals:
        vals = [None] * expect_len
    if len(vals) != expect_len:
        raise ValueError(f"配置长度不一致：期望 {expect_len} 项，实际 {len(vals)} 项")
    out: List[Optional[str]] = []
    for v in vals:
        if v is None:
            out.append(None)
            continue
        s = str(v).strip()
        out.append(s or None)
    return out


def build_monitor_configs() -> List[MonitorConfig]:
    if not POOL_ADDRESSES:
        raise ValueError("请先在代码顶部填写 POOL_ADDRESSES")

    pool_list = [str(x).strip() for x in POOL_ADDRESSES if str(x).strip()]
    if not pool_list:
        raise ValueError("POOL_ADDRESSES 不能为空")

    gauge_list = parse_optional_address_list(GAUGE_ADDRESSES, len(pool_list))
    owner_list = parse_optional_address_list(OWNER_ADDRESSES, len(pool_list))

    token_ids_groups = list(TOKEN_IDS_LIST or [])
    if not token_ids_groups:
        token_ids_groups = [[] for _ in pool_list]
    if len(token_ids_groups) != len(pool_list):
        raise ValueError(
            f"TOKEN_IDS_LIST 长度必须与 POOL_ADDRESSES 一致："
            f" pools={len(pool_list)}, token_groups={len(token_ids_groups)}"
        )

    configs: List[MonitorConfig] = []
    for idx, pool in enumerate(pool_list):
        explicit_ids = parse_token_ids(token_ids_groups[idx])
        gauge = gauge_list[idx]
        owner = owner_list[idx]
        if not explicit_ids and not (gauge and owner):
            raise ValueError(
                f"第 {idx} 组配置缺少仓位定位方式："
                f" 要么填写 TOKEN_IDS_LIST[{idx}]，"
                f" 要么填写 GAUGE_ADDRESSES[{idx}] + OWNER_ADDRESSES[{idx}]"
            )

        configs.append(
            MonitorConfig(
                index=idx,
                pool_address=to_checksum(pool),
                gauge_address=to_checksum(gauge) if gauge else None,
                owner_address=to_checksum(owner) if owner else None,
                explicit_token_ids=explicit_ids,
            )
        )
    return configs


def build_alert_message(rep: PositionReport, pool_address: str, monitor_index: int) -> str:
    if rep.state == "below_or_at_lower":
        state_text = "价格在下边界或更低，仓位已变成单边 token0"
    elif rep.state == "above_or_at_upper":
        state_text = "价格在上边界或更高，仓位已变成单边 token1"
    else:
        state_text = "仓位在区间内"

    return (
        f"[Aero CL Alert] monitor#{monitor_index} tokenId={rep.token_id}\n"
        f"pool={pool_address}\n"
        f"Pool side: {rep.token0.symbol} / {rep.token1.symbol}\n"
        f"Current: {rep.token0.symbol}={format_decimal(rep.amount0, 8)}, {rep.token1.symbol}={format_decimal(rep.amount1, 8)}\n"
        f"tick={rep.current_tick}, range=[{rep.tick_lower}, {rep.tick_upper}]\n"
        f"state={rep.state} ({state_text})\n"
        f"zero_side={rep.zero_side or 'None'}"
    )


def build_recovery_message(rep: PositionReport, pool_address: str, monitor_index: int) -> str:
    return (
        f"[Aero CL Recovery] monitor#{monitor_index} tokenId={rep.token_id}\n"
        f"pool={pool_address}\n"
        f"Current: {rep.token0.symbol}={format_decimal(rep.amount0, 8)}, {rep.token1.symbol}={format_decimal(rep.amount1, 8)}\n"
        f"tick={rep.current_tick}, range=[{rep.tick_lower}, {rep.tick_upper}]\n"
        f"state={rep.state}"
    )


def should_alert(rep: PositionReport, zero_threshold_raw: int) -> bool:
    return rep.amount0_raw <= zero_threshold_raw or rep.amount1_raw <= zero_threshold_raw


def init_pool_contexts() -> List[PoolContext]:
    configs = build_monitor_configs()
    contexts: List[PoolContext] = []

    for cfg in configs:
        validate_pool_vs_gauge(w3, cfg.pool_address, cfg.gauge_address)
        position_manager_address = resolve_position_manager(w3, cfg.gauge_address)

        pool = w3.eth.contract(address=cfg.pool_address, abi=POOL_ABI)
        token0_meta = load_token_meta(w3, pool.functions.token0().call())
        token1_meta = load_token_meta(w3, pool.functions.token1().call())

        ctx = PoolContext(
            index=cfg.index,
            pool_address=cfg.pool_address,
            gauge_address=cfg.gauge_address,
            owner_address=cfg.owner_address,
            position_manager_address=position_manager_address,
            token0_meta=token0_meta,
            token1_meta=token1_meta,
            explicit_token_ids=cfg.explicit_token_ids,
        )
        contexts.append(ctx)

    return contexts


def main() -> int:
    try:
        contexts = init_pool_contexts()

        print(f"[INIT] RPC connected: chain_id={w3.eth.chain_id}")
        print(f"[INIT] total_monitors={len(contexts)}")
        for ctx in contexts:
            print(f"[INIT][monitor#{ctx.index}] pool={ctx.pool_address}")
            print(f"[INIT][monitor#{ctx.index}] gauge={ctx.gauge_address or 'N/A'}")
            print(f"[INIT][monitor#{ctx.index}] npm={ctx.position_manager_address}")
            print(
                f"[INIT][monitor#{ctx.index}] token0={ctx.token0_meta.symbol}({ctx.token0_meta.address}) "
                f"decimals={ctx.token0_meta.decimals}"
            )
            print(
                f"[INIT][monitor#{ctx.index}] token1={ctx.token1_meta.symbol}({ctx.token1_meta.address}) "
                f"decimals={ctx.token1_meta.decimals}"
            )
            print(f"[INIT][monitor#{ctx.index}] explicit_token_ids={ctx.explicit_token_ids}")
            print(f"[INIT][monitor#{ctx.index}] owner={ctx.owner_address or 'N/A'}")

        alerted: Dict[Tuple[str, int], bool] = {}

        while True:
            active_keys: set[Tuple[str, int]] = set()

            for ctx in contexts:
                token_ids = list(ctx.explicit_token_ids)
                if ctx.gauge_address and ctx.owner_address:
                    try:
                        discovered = fetch_staked_token_ids(w3, ctx.gauge_address, ctx.owner_address)
                        token_ids = list(dict.fromkeys(token_ids + discovered))
                    except Exception as e:
                        print(f"[WARN][monitor#{ctx.index}] 读取 staked tokenIds 失败: {e}", file=sys.stderr)

                if not token_ids:
                    print(f"[WARN][monitor#{ctx.index}] 当前没有可监控的 tokenId")
                    continue

                print(f"[LOOP][monitor#{ctx.index}] pool={ctx.pool_address} tokenIds={token_ids}")

                for token_id in token_ids:
                    active_keys.add((ctx.pool_address, token_id))
                    try:
                        rep = inspect_position(
                            w3=w3,
                            pool_address=ctx.pool_address,
                            position_manager_address=ctx.position_manager_address,
                            token_id=token_id,
                            token0_meta=ctx.token0_meta,
                            token1_meta=ctx.token1_meta,
                        )
                        print_report(rep, pool_address=ctx.pool_address, monitor_index=ctx.index)

                        is_alert = should_alert(rep, ZERO_THRESHOLD_RAW)
                        key = (ctx.pool_address, token_id)
                        was_alert = alerted.get(key, False)

                        if is_alert and not was_alert:
                            msg = build_alert_message(rep, ctx.pool_address, ctx.index)
                            notify(msg)
                            alerted[key] = True
                        elif (not is_alert) and was_alert:
                            msg = build_recovery_message(rep, ctx.pool_address, ctx.index)
                            print(msg)
                            beep_notice()
                            alerted[key] = False
                        else:
                            alerted[key] = is_alert

                    except Exception as e:
                        print(
                            f"[ERROR][monitor#{ctx.index}] pool={ctx.pool_address} tokenId={token_id} 检查失败: {e}",
                            file=sys.stderr,
                        )

            for old_key in list(alerted.keys()):
                if old_key not in active_keys:
                    alerted.pop(old_key, None)

            if RUN_ONCE:
                break
            time.sleep(INTERVAL_SECONDS)

    except KeyboardInterrupt:
        print("\n用户中断")
        return 130
    except Exception as e:
        print(f"启动失败: {e}", file=sys.stderr)
        return 1

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
