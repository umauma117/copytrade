# -*- coding: utf-8 -*-
"""跟单脚本配置：从环境变量加载。"""
import os
from dotenv import load_dotenv

load_dotenv()

# WebSocket RPC（监控 pending 交易必须用 WS）
RPC_WS_URL = os.getenv("RPC_WS_URL", "").strip()
# HTTP RPC（区块扫描 & 发交易用；建议单独设置）
RPC_HTTP_URL = os.getenv("RPC_HTTP_URL", "").strip()
if not RPC_HTTP_URL:
    RPC_HTTP_URL = "https://bsc-dataseed1.binance.org"

# 备用 HTTP RPC 列表（超时时自动轮换）
RPC_HTTP_FALLBACKS = [
    u.strip() for u in os.getenv("RPC_HTTP_FALLBACKS", "").split(",") if u.strip()
] or [
    "https://rpc.ankr.com/bsc/b08f7067651e48d73413e2ac221622354ad8267190cff32e3163b56369d17536",
    "https://bsc-dataseed1.binance.org",
    "https://bsc-dataseed2.binance.org",
    "https://bsc-dataseed3.binance.org",
    "https://bsc-dataseed4.binance.org",
    "https://bsc-dataseed1.defibit.io",
    "https://bsc-dataseed2.defibit.io",
]

# 领袖钱包地址列表（逗号分隔）
_leaders = os.getenv("LEADER_ADDRESSES", "").strip()
LEADER_ADDRESSES = [a.strip().lower() for a in _leaders.split(",") if a.strip()]

# 跟单私钥
FOLLOWER_PRIVATE_KEY = os.getenv("FOLLOWER_PRIVATE_KEY", "").strip()
if FOLLOWER_PRIVATE_KEY and not FOLLOWER_PRIVATE_KEY.startswith("0x"):
    FOLLOWER_PRIVATE_KEY = "0x" + FOLLOWER_PRIVATE_KEY

# 是否执行跟单（False 则仅监控打印）
EXECUTE_COPY = os.getenv("EXECUTE_COPY", "false").strip().lower() in ("true", "1", "yes")
# 是否跟随领袖卖出动作（false=只跟买）
COPY_SELL_ACTIONS = os.getenv("COPY_SELL_ACTIONS", "true").strip().lower() in ("true", "1", "yes")

# 跟单金额比例
try:
    COPY_AMOUNT_RATIO = float(os.getenv("COPY_AMOUNT_RATIO", "1.0"))
except ValueError:
    COPY_AMOUNT_RATIO = 1.0

# 滑点（基点，50 = 0.5%）
try:
    SLIPPAGE_BPS = int(os.getenv("SLIPPAGE_BPS", "50"))
except ValueError:
    SLIPPAGE_BPS = 50

# 止盈百分比（30 = 盈利 30% 时自动卖出）
try:
    TAKE_PROFIT_PCT = float(os.getenv("TAKE_PROFIT_PCT", "30"))
except ValueError:
    TAKE_PROFIT_PCT = 30.0

# 止盈检查间隔（秒）
try:
    TAKE_PROFIT_CHECK_INTERVAL = float(os.getenv("TAKE_PROFIT_CHECK_INTERVAL", "10"))
except ValueError:
    TAKE_PROFIT_CHECK_INTERVAL = 10.0


def validate_config():
    if not LEADER_ADDRESSES:
        raise ValueError("请设置 LEADER_ADDRESSES（至少一个领袖地址）")
    if EXECUTE_COPY and not FOLLOWER_PRIVATE_KEY:
        raise ValueError("执行跟单时请设置 FOLLOWER_PRIVATE_KEY")
