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

# 未平仓仓位本地快照（相对运行目录，重启后恢复并做链上自检）
POSITIONS_STATE_FILE = (os.getenv("POSITIONS_STATE_FILE", ".positions_state.json") or "").strip() or ".positions_state.json"

# 网页控制台（Flask）
DASHBOARD_ENABLE = os.getenv("DASHBOARD_ENABLE", "false").strip().lower() in ("true", "1", "yes")
DASHBOARD_HOST = (os.getenv("DASHBOARD_HOST", "127.0.0.1") or "").strip() or "127.0.0.1"
try:
    DASHBOARD_PORT = int(os.getenv("DASHBOARD_PORT", "8765").strip() or "8765")
except ValueError:
    DASHBOARD_PORT = 8765
DASHBOARD_TOKEN = os.getenv("DASHBOARD_TOKEN", "").strip()

# 控制台口令最短长度（过短易被猜中）
DASHBOARD_TOKEN_MIN_LEN = 16


def dashboard_bind_is_all_interfaces() -> bool:
    """是否监听所有网卡（外网可达风险高）。"""
    h = DASHBOARD_HOST.strip().lower()
    return h in ("0.0.0.0", "::", "[::]")


def dashboard_config_error() -> str:
    """开启控制台时若配置不合格，返回中文错误说明，否则返回空串。"""
    if not DASHBOARD_ENABLE:
        return ""
    if not DASHBOARD_TOKEN:
        return "未设置 DASHBOARD_TOKEN（.env 里加一行，至少 16 位乱码）"
    if len(DASHBOARD_TOKEN) < DASHBOARD_TOKEN_MIN_LEN:
        return f"DASHBOARD_TOKEN 太短，至少 {DASHBOARD_TOKEN_MIN_LEN} 个字符"
    return ""


def validate_config():
    if not LEADER_ADDRESSES:
        raise ValueError("请设置 LEADER_ADDRESSES（至少一个领袖地址）")
    if EXECUTE_COPY and not FOLLOWER_PRIVATE_KEY:
        raise ValueError("执行跟单时请设置 FOLLOWER_PRIVATE_KEY")
