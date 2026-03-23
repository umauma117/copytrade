# -*- coding: utf-8 -*-
"""解析 DEX swap 交易：从 tx input 解码出 swap 参数。"""
import logging
import time
from typing import Any, Dict, Optional, Set, Tuple

from eth_abi import decode
from web3 import Web3

from abi import UNISWAP_V2_ROUTER_ADDRESSES

logger = logging.getLogger(__name__)

# BSC 链 WBNB 地址，用于判断 买入/卖出
WBNB_BSC = "0xbb4CdB9CBd36B01bD1cBaEBF2De08d9173bc095c".lower()
USDT_BSC = "0x55d398326f99059ff775485246999027b3197955".lower()
BUSD_BSC = "0xe9e7cea3dedca5984780bafc599bd69add087d56".lower()
USDC_BSC = "0x8ac76a51cc950d9822d68b83fe1ad97b32cd580d".lower()
QUOTE_TOKENS_BSC: Set[str] = {WBNB_BSC, USDT_BSC, BUSD_BSC, USDC_BSC}
BNB_DECIMALS = 18

# 领袖地址最近一次 approve 的 token（用于自定义合约卖出/买入兜底识别）
_LAST_APPROVED_TOKEN_BY_LEADER: Dict[str, str] = {}
# 领袖最近一次 approve 的 (token, spender)；卖出前领袖常先授权中间合约（非聚合器/pool）
_LAST_APPROVE_PAIR_BY_LEADER: Dict[str, Tuple[str, str]] = {}

# 函数选择器 => (函数名, 类型列表)
# 前 4 字节 keccak256("函数签名")
SELECTORS = {
    "0x7ff36ab5": ("swapExactETHForTokens", ["uint256", "address[]", "address", "uint256"]),
    "0x18cbafe5": ("swapExactTokensForETH", ["uint256", "uint256", "address[]", "address", "uint256"]),
    "0x38ed1739": ("swapExactTokensForTokens", ["uint256", "uint256", "address[]", "address", "uint256"]),
    "0xb6f9de95": ("swapExactETHForTokensSupportingFeeOnTransferTokens", ["uint256", "address[]", "address", "uint256"]),
    "0x791ac947": ("swapExactTokensForETHSupportingFeeOnTransferTokens", ["uint256", "uint256", "address[]", "address", "uint256"]),
    "0x5c11d795": ("swapExactTokensForTokensSupportingFeeOnTransferTokens", ["uint256", "uint256", "address[]", "address", "uint256"]),
}

# 常见非 swap 选择器
APPROVE_SELECTOR = "0x095ea7b3"

# 领袖常用自定义合约方法（当前观测）
# 0x0b3f5cf9 已单独在 _decode_aggregator_0b3f5cf9 里解码，不再走兜底启发式
CUSTOM_EXEC_SELECTORS = {
    "0x3e0f9c3c",
}

# 聚合器合约选择器（直接 calldata 解码，不依赖收据）
AGGREGATOR_SELECTORS = {
    "0x0b3f5cf9",   # swap(tuple[] descs, address feeToken, uint256 amountIn, uint256 minReturn)
}

# 已知的聚合路由合约地址（只有发往这些地址的交易才走聚合器解码）
KNOWN_AGGREGATOR_ADDRS = {
    "0x1de460f363af910f51726def188f9004276bf4bc",
}

# 单笔跟单买入金额上限（超过则认为解码错误，丢弃），单位 wei
_MAX_SANE_AMOUNT_IN_WEI = 200 * 10 ** 18  # 200 BNB


def _normalize_input_hex(input_data: Any) -> str:
    """兼容 str / bytes / HexBytes，统一转成 0x 开头的十六进制字符串。"""
    if input_data is None:
        return ""
    if isinstance(input_data, (bytes, bytearray)):
        return "0x" + bytes(input_data).hex()
    # HexBytes 继承 bytes，已在上面处理；其余走字符串分支
    s = str(input_data)
    return s if s.startswith("0x") else "0x" + s


def decode_swap_tx(
    w3: Web3,
    to_address: Optional[str],
    input_hex: Any,
    value_wei: int = 0,
) -> Optional[Dict[str, Any]]:
    """
    若为已知 Uniswap V2 风格 swap，返回解码后的参数字典；
    否则返回 None。
    """
    raw = _normalize_input_hex(input_hex)
    if not raw or len(raw) < 10:
        return None
    selector = raw[:10].lower()
    to_address = (to_address or "").lower()
    if to_address and to_address not in UNISWAP_V2_ROUTER_ADDRESSES:
        # 仍尝试解码，可能是其他链上相同接口的 Router
        pass
    if selector not in SELECTORS:
        # 噪音抑制：approve / 已知自定义执行函数不打印“未识别”
        if selector in {APPROVE_SELECTOR, *CUSTOM_EXEC_SELECTORS, *AGGREGATOR_SELECTORS}:
            return None
        # 打印未识别的选择器，方便定位领袖使用的 DEX/合约
        if raw and len(raw) >= 10:
            logger.info(
                "[未识别selector] to=%s selector=%s (请将此地址和选择器告知开发者以添加支持)",
                (to_address or "")[:20], selector,
            )
        return None
    name, types = SELECTORS[selector]
    try:
        payload = bytes.fromhex(raw[10:])
        decoded = decode(types, payload)
    except Exception as e:
        logger.debug("decode swap failed %s: %s", selector, e)
        return None
    # 统一格式: amountIn, amountOutMin, path, to, deadline
    # swapExactETHForTokens: amountOutMin, path, to, deadline -> amountIn = value_wei
    if "ETHForTokens" in name:
        amount_out_min = decoded[0]
        path = list(decoded[1])
        to_addr = decoded[2]
        deadline = decoded[3]
        amount_in = value_wei
    else:
        amount_in = decoded[0]
        amount_out_min = decoded[1]
        path = list(decoded[2])
        to_addr = decoded[3]
        deadline = decoded[4]
    return {
        "method": name,
        "amount_in": amount_in,
        "amount_out_min": amount_out_min,
        "path": [w3.to_checksum_address(a) for a in path],
        "to": w3.to_checksum_address(to_addr) if to_addr else None,
        "deadline": deadline,
        "router": to_address or None,
    }


def _decode_aggregator_swap(raw: str, value_wei: int, w3: Web3, to_addr: str = "") -> Optional[Dict[str, Any]]:
    """
    解码 0x0b3f5cf9 聚合器格式：
      swap(tuple[] descs, address feeToken, uint256 amountIn, uint256 minReturn)

    正确处理多跳路由（descs 数组可能有 1~N 个元素）：
      - 整笔交易的 tokenIn = desc[0].tokenIn
      - 整笔交易的 tokenOut = desc[-1].tokenOut
    """
    import time as _time
    try:
        payload = bytes.fromhex(raw[10:])
        if len(payload) < 5 * 32:
            return None

        def word_int(n: int) -> int:
            return int.from_bytes(payload[n * 32:(n + 1) * 32], "big")

        def addr_at_byte(offset: int) -> str:
            """从 payload 绝对字节偏移读取 address（32字节 slot 的后20字节）。"""
            return "0x" + payload[offset + 12:offset + 32].hex()

        amount_in_calldata = word_int(2)
        min_return = word_int(3)

        # ── 解析 descs 动态数组 ──
        n_descs = word_int(4)
        if n_descs <= 0 or n_descs > 10:
            logger.debug("聚合器解码：descs 数量异常 (%d)，跳过", n_descs)
            return None

        # offsets 区起始字节位置（紧接 array length 之后）
        offsets_start_byte = 5 * 32  # word[5] 开始

        # desc[0] 的起始字节 = offsets_start_byte + offset[0]
        offset_first = word_int(5)
        desc0_byte = offsets_start_byte + offset_first
        # 每个 desc tuple: field[0]=swapType(32B), field[1]=tokenIn(32B), field[2]=tokenOut(32B), ...
        if desc0_byte + 96 > len(payload):
            return None
        token_in = addr_at_byte(desc0_byte + 32).lower()   # field[1]

        # desc[-1] 的起始字节
        offset_last = word_int(5 + n_descs - 1)
        desc_last_byte = offsets_start_byte + offset_last
        if desc_last_byte + 96 > len(payload):
            return None
        token_out = addr_at_byte(desc_last_byte + 64).lower()  # field[2]

        _zero = "0x" + "0" * 40

        # 判断买入 / 卖出方向
        if token_in in (WBNB_BSC, _zero):
            amount_in = value_wei if value_wei > 0 else amount_in_calldata
            method = "swapExactETHForTokensSupportingFeeOnTransferTokens"
            token_in = WBNB_BSC
        elif token_out in (WBNB_BSC, _zero):
            amount_in = amount_in_calldata
            method = "swapExactTokensForETHSupportingFeeOnTransferTokens"
            token_out = WBNB_BSC
        else:
            amount_in = amount_in_calldata
            method = "swapExactTokensForTokensSupportingFeeOnTransferTokens"

        # 金额合理性校验
        if "ETHForTokens" in method and amount_in > _MAX_SANE_AMOUNT_IN_WEI:
            logger.warning(
                "聚合器解码：amount_in=%.2f BNB 超过上限，疑似解码错误，跳过",
                amount_in / 1e18,
            )
            return None

        path = [w3.to_checksum_address(token_in), w3.to_checksum_address(token_out)]
        is_buy = "ETHForTokens" in method
        if is_buy:
            logger.info(
                "[聚合器解码-买入] 花 %.6f BNB 买 %s (%d跳) minReturn=%d",
                amount_in / 1e18, token_out[:16], n_descs, min_return,
            )
        else:
            logger.info(
                "[聚合器解码-卖出] 卖 %s → %.6f BNB (%d跳) amount=%d(wei)",
                token_in[:16], min_return / 1e18, n_descs, amount_in,
            )
        return {
            "method": method,
            "amount_in": amount_in,
            "amount_out_min": min_return,
            "path": path,
            "to": None,
            "deadline": int(_time.time()) + 300,
            "router": PANCAKE_V2_ROUTER.lower(),
            "_from_aggregator": True,
            "_aggregator_payload": payload,
            "_aggregator_addr": to_addr or "",
            "_use_native_bnb": token_in == WBNB_BSC and value_wei > 0,
        }
    except Exception as e:
        logger.debug("aggregator decode failed: %s", e)
        return None


# ERC20 Transfer 事件 topic
_TRANSFER_TOPIC = "0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef"

# PancakeSwap V2 Router（用于跟单执行）
PANCAKE_V2_ROUTER = "0x10ED43C718714eb63d5aA57B78B54704E256024E"


def _inject_extra_spenders_from_leader_approve(
    swap_info: Dict[str, Any], leader_addr: str
) -> None:
    """
    若领袖在卖出前对「卖出的 token」做了 approve，把 spender 记入 swap_info，
    跟卖时对同一 token 也 approve 该地址（解决 transferFrom 非聚合器/pool 的情况）。
    """
    if not leader_addr or not swap_info:
        return
    # get_trade_action 在文件后部定义；此处仅在运行期调用，无循环引用问题
    if get_trade_action(swap_info) != "sell":
        return
    path = swap_info.get("path") or []
    if len(path) < 2:
        return
    token_in = (path[0] or "").lower()
    pair = _LAST_APPROVE_PAIR_BY_LEADER.get(leader_addr)
    if not pair:
        return
    token_l, spender_l = pair[0].lower(), pair[1].lower()
    if token_l != token_in or not spender_l:
        return
    prev = list(swap_info.get("_extra_approve_spenders") or [])
    lower_prev = {p.lower() if isinstance(p, str) else "" for p in prev}
    if spender_l not in lower_prev:
        prev.append(spender_l)
        swap_info["_extra_approve_spenders"] = prev


def parse_leader_tx(tx: dict, w3: Web3) -> Optional[Dict[str, Any]]:
    """
    解析领袖交易：
    1. 先尝试 calldata 解码（标准 V2 Router）
    2. 若不认识选择器，改用收据 Transfer 事件反推买卖（支持自定义合约）
    """
    to_addr = tx.get("to")
    data = tx.get("input") or tx.get("data") or ""
    raw = _normalize_input_hex(data)
    selector = raw[:10].lower() if len(raw) >= 10 else ""
    value = int(tx.get("value", 0) or 0)
    leader_addr = (tx.get("from") or "").lower()

    def _finish(out: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
        if out:
            _inject_extra_spenders_from_leader_approve(out, leader_addr)
        return out

    # approve 不作为买卖动作，但记录 token + spender，供后续卖出跟单授权
    if selector == APPROVE_SELECTOR and to_addr and leader_addr:
        token_l = (to_addr or "").lower()
        _LAST_APPROVED_TOKEN_BY_LEADER[leader_addr] = token_l
        try:
            payload = bytes.fromhex(raw[10:])
            if len(payload) >= 64:
                spender_addr, _ = decode(["address", "uint256"], payload)
                _LAST_APPROVE_PAIR_BY_LEADER[leader_addr] = (
                    token_l,
                    spender_addr.lower(),
                )
        except Exception:
            pass
        return None

    # ── 方法一：calldata 解码（标准 V2 Router）──
    result = decode_swap_tx(w3, to_addr, data, value)
    if result:
        return _finish(result)

    # ── 方法一-B：聚合器 calldata 直接解码（仅限已知聚合路由合约）──
    to_lower = (to_addr or "").lower()
    if selector in AGGREGATOR_SELECTORS and to_lower in KNOWN_AGGREGATOR_ADDRS:
        agg = _decode_aggregator_swap(raw, value, w3, to_addr=to_lower)
        if agg:
            return _finish(agg)

    # ── 方法二：收据 Transfer 事件反推 ──
    tx_hash = tx.get("hash")
    if not tx_hash:
        return _finish(_parse_custom_heuristic(w3, leader_addr, selector, to_addr, value))
    tx_hash_hex = tx_hash.hex() if hasattr(tx_hash, "hex") else str(tx_hash)
    from_addr = (tx.get("from") or "").lower()
    out = _parse_from_receipt(w3, tx_hash_hex, from_addr, value, receipt=tx.get("_receipt"))
    if out:
        return _finish(out)
    return _finish(_parse_custom_heuristic(w3, leader_addr, selector, to_addr, value))


def _parse_custom_heuristic(
    w3: Web3,
    leader_addr: str,
    selector: str,
    to_addr: Optional[str],
    value_wei: int,
) -> Optional[Dict[str, Any]]:
    """
    自定义合约兜底识别：
    - approve 后紧接自定义执行函数
    - value>0 视为买入
    - value==0 视为卖出
    标的 token 优先使用最近一次 approve 的 token。
    """
    if selector not in CUSTOM_EXEC_SELECTORS:
        return None
    approved_token = _LAST_APPROVED_TOKEN_BY_LEADER.get(leader_addr, "")
    # 没有最近 approve，就无法安全判断标的
    if not approved_token:
        return None

    import time as _time

    if value_wei > 0:
        # 买入：WBNB -> approved_token
        return {
            "method": "customBuyHeuristic",
            "amount_in": value_wei,
            "amount_out_min": 0,
            "path": [w3.to_checksum_address(WBNB_BSC), w3.to_checksum_address(approved_token)],
            "to": None,
            "deadline": int(_time.time()) + 300,
            "router": PANCAKE_V2_ROUTER.lower(),
            "_from_custom_heuristic": True,
        }

    # 卖出：approved_token -> WBNB
    return {
        "method": "customSellHeuristic",
        "amount_in": 0,
        "amount_out_min": 0,
        "path": [w3.to_checksum_address(approved_token), w3.to_checksum_address(WBNB_BSC)],
        "to": None,
        "deadline": int(_time.time()) + 300,
        "router": PANCAKE_V2_ROUTER.lower(),
        "_from_custom_heuristic": True,
    }


def _addr_from_topic(t) -> str:
    h = t.hex() if hasattr(t, "hex") else str(t)
    return ("0x" + h[-40:]).lower()


def _parse_from_receipt(
    w3: Web3,
    tx_hash: str,
    leader_addr: str,
    value_wei: int,
    receipt: Optional[dict] = None,
) -> Optional[Dict[str, Any]]:
    """
    通过收据里的 ERC20 Transfer 事件判断买卖。

    买入优先级：
      1. 转入领袖 EOA 的非计价 token
      2. 转入领袖自定义合约（receipt.to）的非计价 token
      3. 收据中所有非计价 token 转账量最大的（兜底）
    卖出优先级：
      1. 从领袖 EOA 发出的非计价 token
      2. 从领袖自定义合约发出的非计价 token
    """
    import time as _time

    if receipt is None:
        last_err = None
        for _ in range(4):
            try:
                receipt = w3.eth.get_transaction_receipt(tx_hash)
                break
            except Exception as e:
                last_err = e
                time.sleep(0.15)
        if receipt is None:
            logger.debug("获取收据失败 %s: %s", tx_hash[:12], last_err)
            return None
    if not receipt:
        return None

    logs = receipt.get("logs") or []
    leader_lower = leader_addr.lower()
    # 领袖调用的自定义合约地址（receipt.to）
    contract_lower = (receipt.get("to") or "").lower()
    quote_set = QUOTE_TOKENS_BSC

    # token → 该 token 在收据中所有 Transfer 的累计量（买入兜底用）
    non_quote_totals: Dict[str, int] = {}
    # 转入领袖 EOA 的 token
    tokens_to_leader: Dict[str, int] = {}
    # 转入领袖自定义合约的 token
    tokens_to_contract: Dict[str, int] = {}
    # 从领袖 EOA 发出的 token（卖出信号）
    tokens_from_leader: Dict[str, int] = {}
    # 从领袖自定义合约发出的 token（卖出信号）
    tokens_from_contract: Dict[str, int] = {}

    for log in logs:
        topics = log.get("topics") or []
        if not topics:
            continue
        t0 = topics[0]
        t0_hex = t0.hex() if hasattr(t0, "hex") else str(t0)
        if t0_hex.lower() != _TRANSFER_TOPIC.lower():
            continue
        if len(topics) < 3:
            continue

        from_t = _addr_from_topic(topics[1])
        to_t   = _addr_from_topic(topics[2])
        token  = (log.get("address") or "").lower()

        raw_data = log.get("data") or b""
        try:
            data_hex = raw_data.hex() if hasattr(raw_data, "hex") else str(raw_data)
            amount = int(data_hex, 16) if data_hex else 0
        except Exception:
            amount = 0

        if token not in quote_set and amount > 0:
            non_quote_totals[token] = non_quote_totals.get(token, 0) + amount

        if token not in quote_set:
            if to_t == leader_lower:
                tokens_to_leader[token] = tokens_to_leader.get(token, 0) + amount
            if to_t == contract_lower and contract_lower:
                tokens_to_contract[token] = tokens_to_contract.get(token, 0) + amount
            if from_t == leader_lower:
                tokens_from_leader[token] = tokens_from_leader.get(token, 0) + amount
            if from_t == contract_lower and contract_lower:
                tokens_from_contract[token] = tokens_from_contract.get(token, 0) + amount

    deadline = int(_time.time()) + 300

    # ── 买入：value_wei > 0 ──
    if value_wei > 0:
        # 优先级：直接给领袖 > 给领袖合约 > 收据里最大量
        if tokens_to_leader:
            token_out = max(tokens_to_leader, key=tokens_to_leader.get)
            amount_out = tokens_to_leader[token_out]
            src = "→EOA"
        elif tokens_to_contract:
            token_out = max(tokens_to_contract, key=tokens_to_contract.get)
            amount_out = tokens_to_contract[token_out]
            src = "→合约"
        elif non_quote_totals:
            token_out = max(non_quote_totals, key=non_quote_totals.get)
            amount_out = non_quote_totals[token_out]
            src = "→最大量(兜底)"
        else:
            return None
        path = [w3.to_checksum_address(WBNB_BSC), w3.to_checksum_address(token_out)]
        logger.info(
            "[收据解析-买入%s] token=%s amount_in=%.6f BNB amount_out=%d",
            src, token_out[:16], value_wei / 1e18, amount_out,
        )
        return {
            "method": "swapExactETHForTokensSupportingFeeOnTransferTokens",
            "amount_in": value_wei,
            "amount_out_min": int(amount_out * 0.85),
            "path": path,
            "to": None,
            "deadline": deadline,
            "router": PANCAKE_V2_ROUTER.lower(),
            "_from_receipt": True,
        }

    # ── 卖出：优先从领袖 EOA，其次从领袖合约 ──
    sell_candidates = tokens_from_leader or tokens_from_contract
    if sell_candidates:
        token_in = max(sell_candidates, key=sell_candidates.get)
        amount_in = sell_candidates[token_in]
        path = [w3.to_checksum_address(token_in), w3.to_checksum_address(WBNB_BSC)]
        src = "EOA" if tokens_from_leader else "合约"
        logger.info(
            "[收据解析-卖出(%s)] token=%s amount_in=%d",
            src, token_in[:16], amount_in,
        )
        return {
            "method": "swapExactTokensForETHSupportingFeeOnTransferTokens",
            "amount_in": amount_in,
            "amount_out_min": 0,
            "path": path,
            "to": None,
            "deadline": deadline,
            "router": PANCAKE_V2_ROUTER.lower(),
            "_from_receipt": True,
        }

    return None


def get_trade_action(
    swap_info: Dict[str, Any],
    quote_tokens: Optional[Set[str]] = None,
) -> str:
    """
    返回动作类型:
      - buy:  用主流计价币(如 BNB/USDT/USDC/BUSD)买入其他代币
      - sell: 卖出代币换回主流计价币
      - swap: 其他代币互换
      - unknown: 无法判断
    """
    path = swap_info.get("path") or []
    if len(path) < 2:
        return "unknown"
    quotes = quote_tokens or QUOTE_TOKENS_BSC
    token_in = (path[0] or "").lower()
    token_out = (path[-1] or "").lower()
    in_quote = token_in in quotes
    out_quote = token_out in quotes

    if in_quote and not out_quote:
        return "buy"
    if not in_quote and out_quote:
        return "sell"
    return "swap"


def get_trade_type_and_summary(
    swap_info: Dict[str, Any],
    tx_hash: Optional[str] = None,
) -> Tuple[str, str]:
    """
    根据 path 判断交易类型并生成可读摘要。
    返回 (类型, 摘要)，类型为 "买入" | "卖出" | "兑换"。
    """
    path = swap_info.get("path") or []
    if not path:
        return "兑换", "path 为空"
    amount_in = swap_info.get("amount_in") or 0
    amount_out_min = swap_info.get("amount_out_min") or 0
    action = get_trade_action(swap_info)

    def bnb_fmt(wei_val: int) -> str:
        if wei_val <= 0:
            return "0"
        return f"{wei_val / (10 ** BNB_DECIMALS):.6f} BNB"

    tx_line = f" tx={str(tx_hash)[:12]}...{str(tx_hash)[-8:]}" if tx_hash else ""

    if action == "buy":
        return "买入", f"用 {bnb_fmt(amount_in)} 买入 {path[-1][:12]}... 最少收到={amount_out_min}{tx_line}"
    if action == "sell":
        return "卖出", f"卖出 {path[0][:12]}... 数量={amount_in} 最少换回 {bnb_fmt(amount_out_min)}{tx_line}"
    return "兑换", f"{path[0][:10]}... -> {path[-1][:10]}... amount_in={amount_in}{tx_line}"
