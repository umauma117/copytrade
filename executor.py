# -*- coding: utf-8 -*-
"""跟单执行：根据解析出的 swap 参数，用跟单钱包发起相同方向的交易。"""
import logging
from typing import Any, Dict, Optional

from web3 import Web3
from eth_account import Account

from abi import UNISWAP_V2_ROUTER_ABI
from config import COPY_AMOUNT_RATIO, FOLLOWER_PRIVATE_KEY, SLIPPAGE_BPS

logger = logging.getLogger(__name__)

# PancakeSwap V2 工厂，用于检查交易对是否存在
_PANCAKE_FACTORY_V2 = "0xcA143Ce32Fe78f1f7019d7d551a6402fC5350c73"
_FACTORY_ABI = [
    {
        "constant": True,
        "inputs": [{"name": "tokenA", "type": "address"}, {"name": "tokenB", "type": "address"}],
        "name": "getPair",
        "outputs": [{"name": "", "type": "address"}],
        "type": "function",
    }
]
_ZERO_ADDR = "0x" + "0" * 40


def _estimate_min_bnb_for_tx(w3: Web3, value_wei: int, gas_limit: int) -> int:
    """粗略估算本笔交易至少需要的 BNB（wei）：value + gas * gasPrice * 1.2。"""
    try:
        gp = int(w3.eth.gas_price * 1.1)
    except Exception:
        gp = 3 * 10**9
    return value_wei + int(gas_limit * gp * 12 // 10)


def _log_bnb_shortfall(w3: Web3, account, need_wei: int, extra_msg: str = "") -> None:
    try:
        bal = w3.eth.get_balance(account.address)
        logger.warning(
            "[跟单失败-BNB不足] 钱包=%s 余额=%.6f BNB 估算至少需=%.6f BNB %s",
            account.address[:10],
            bal / 1e18,
            need_wei / 1e18,
            extra_msg,
        )
    except Exception as e:
        logger.warning("[跟单失败] 无法读取 BNB 余额: %s", e)


def _pair_exists(w3: Web3, token_a: str, token_b: str) -> bool:
    """检查 PancakeSwap V2 上 tokenA/tokenB 是否有流动性池。"""
    try:
        factory = w3.eth.contract(
            address=w3.to_checksum_address(_PANCAKE_FACTORY_V2), abi=_FACTORY_ABI
        )
        pair = factory.functions.getPair(
            w3.to_checksum_address(token_a),
            w3.to_checksum_address(token_b),
        ).call()
        return pair.lower() != _ZERO_ADDR
    except Exception as e:
        logger.debug("查询交易对失败（默认继续尝试）: %s", e)
        return True  # 查不到时默认尝试，避免漏单


ERC20_ABI = [
    {
        "constant": True,
        "inputs": [{"name": "_owner", "type": "address"}],
        "name": "balanceOf",
        "outputs": [{"name": "balance", "type": "uint256"}],
        "type": "function",
    }
]


_APPROVE_ABI = [
    {
        "constant": False,
        "inputs": [{"name": "_spender", "type": "address"}, {"name": "_value", "type": "uint256"}],
        "name": "approve",
        "outputs": [{"name": "", "type": "bool"}],
        "type": "function",
    },
    {
        "constant": True,
        "inputs": [{"name": "_owner", "type": "address"}, {"name": "_spender", "type": "address"}],
        "name": "allowance",
        "outputs": [{"name": "", "type": "uint256"}],
        "type": "function",
    },
]


def _ensure_approve(w3: Web3, account, token_addr: str, spender: str, amount: int) -> bool:
    """检查 allowance，不足则发 approve 并等上链。"""
    try:
        token = w3.eth.contract(address=w3.to_checksum_address(token_addr), abi=_APPROVE_ABI)
        allowance = token.functions.allowance(account.address, w3.to_checksum_address(spender)).call()
        if allowance >= amount:
            return True
        nonce = w3.eth.get_transaction_count(account.address, "pending")
        tx = token.functions.approve(
            w3.to_checksum_address(spender), 2**256 - 1,
        ).build_transaction({
            "from": account.address, "gas": 100000, "nonce": nonce, "chainId": w3.eth.chain_id,
        })
        signed = account.sign_transaction(tx)
        tx_hash = w3.eth.send_raw_transaction(signed.raw_transaction)
        logger.info("[Approve] %s → %s tx=%s", token_addr[:10], spender[:10], tx_hash.hex())
        receipt = w3.eth.wait_for_transaction_receipt(tx_hash, timeout=30)
        if receipt.get("status") == 0:
            logger.warning("[Approve失败] %s approve 链上 revert (status=0)", token_addr[:10])
            return False
        return True
    except Exception as e:
        logger.warning("[Approve失败] %s: %s", token_addr[:10], e)
        return False


def _get_token_balance(w3: Web3, token_addr: str, owner: str) -> int:
    try:
        token = w3.eth.contract(address=w3.to_checksum_address(token_addr), abi=ERC20_ABI)
        return int(token.functions.balanceOf(owner).call())
    except Exception as e:
        logger.warning("读取 token 余额失败 %s: %s", token_addr[:10], e)
        return 0


_WBNB_ADDR = "0xbb4CdB9CBd36B01bD1cBaEBF2De08d9173bc095c"
_WBNB_DEPOSIT_ABI = [
    {"constant": False, "inputs": [], "name": "deposit", "outputs": [], "type": "function", "payable": True},
]


def _wrap_bnb(w3: Web3, account, amount: int) -> bool:
    """将原生 BNB 包装为 WBNB。"""
    try:
        wbnb = w3.eth.contract(address=w3.to_checksum_address(_WBNB_ADDR), abi=_WBNB_DEPOSIT_ABI)
        nonce = w3.eth.get_transaction_count(account.address, "pending")
        tx = wbnb.functions.deposit().build_transaction({
            "from": account.address,
            "value": amount,
            "gas": 60000,
            "nonce": nonce,
            "chainId": w3.eth.chain_id,
        })
        signed = account.sign_transaction(tx)
        tx_hash = w3.eth.send_raw_transaction(signed.raw_transaction)
        logger.info("[Wrap BNB→WBNB] %.6f BNB tx=%s", amount / 1e18, tx_hash.hex())
        w3.eth.wait_for_transaction_receipt(tx_hash, timeout=30)
        return True
    except Exception as e:
        logger.warning("[Wrap BNB→WBNB 失败] %s", e)
        return False


def _execute_via_aggregator(
    w3: Web3,
    swap_info: Dict[str, Any],
    account,
    scaled_in: int,
    scaled_out_min: int,
) -> Optional[str]:
    """
    直接复用领袖的聚合器 calldata，只替换 amountIn (word[2]) 和 minReturn (word[3])，
    发到同一个聚合合约。
    当领袖用 WBNB (value=0) 买入时，自动包装 BNB→WBNB 并授权给聚合器。
    """
    payload: bytes = swap_info.get("_aggregator_payload", b"")
    aggr_addr: str = swap_info.get("_aggregator_addr", "")
    use_native: bool = swap_info.get("_use_native_bnb", False)
    method_name: str = swap_info.get("method", "")

    if not payload or not aggr_addr:
        return None

    is_buy = "ETHForTokens" in method_name

    # 领袖用 WBNB (value=0) 买入 → 跟单者需要先 Wrap BNB + Approve WBNB
    if is_buy and not use_native:
        if not _wrap_bnb(w3, account, scaled_in):
            return None
        if not _ensure_approve(w3, account, _WBNB_ADDR, aggr_addr, scaled_in):
            return None

    new_payload = (
        payload[:64]
        + scaled_in.to_bytes(32, "big")
        + scaled_out_min.to_bytes(32, "big")
        + payload[128:]
    )
    selector = bytes.fromhex("0b3f5cf9")
    calldata = selector + new_payload

    value = scaled_in if use_native else 0

    # 预检 BNB：WBNB 路径还要 wrap + approve
    need = _estimate_min_bnb_for_tx(w3, value, 500000)
    if is_buy and not use_native:
        need += scaled_in + _estimate_min_bnb_for_tx(w3, 0, 160000)
    try:
        if w3.eth.get_balance(account.address) < need:
            _log_bnb_shortfall(w3, account, need, "(聚合器直通)")
            return None
    except Exception:
        pass

    try:
        nonce = w3.eth.get_transaction_count(account.address, "pending")
        gas_price = w3.eth.gas_price
        tx = {
            "to": w3.to_checksum_address(aggr_addr),
            "data": calldata,
            "value": value,
            "gas": 500000,
            "gasPrice": int(gas_price * 1.1),
            "nonce": nonce,
            "chainId": w3.eth.chain_id,
        }
        signed = account.sign_transaction(tx)
        tx_hash = w3.eth.send_raw_transaction(signed.raw_transaction)
        logger.info("[聚合器直通] 交易已发送: %s", tx_hash.hex())
        return tx_hash.hex()
    except Exception as e:
        if "insufficient funds" in str(e).lower():
            _log_bnb_shortfall(w3, account, need, "(聚合器 send_raw)")
        logger.warning("聚合器直通交易失败: %s", e)
        return None


def execute_copy_tx(
    w3: Web3,
    swap_info: Dict[str, Any],
    router_address: str,
) -> Optional[str]:
    """
    根据 swap_info 用跟单账户发起一笔同方向 swap。
    返回交易 hash，失败返回 None。
    """
    if not FOLLOWER_PRIVATE_KEY:
        logger.warning("未配置 FOLLOWER_PRIVATE_KEY，跳过执行")
        return None
    account = Account.from_key(FOLLOWER_PRIVATE_KEY)
    amount_in = swap_info["amount_in"]
    amount_out_min = swap_info["amount_out_min"]
    path = swap_info["path"]
    to_addr = swap_info["to"] or account.address
    deadline = swap_info["deadline"]
    method_name = swap_info["method"]
    if not path or len(path) < 2:
        logger.warning("无效 path，跳过: %s", path)
        return None

    # 执行前检查 PancakeSwap V2 是否有该交易对
    if not _pair_exists(w3, path[0], path[-1]):
        # 若来自聚合器解码，直接复用其 calldata 发到聚合合约（支持 swapType≠0 的池）
        if swap_info.get("_from_aggregator") and swap_info.get("_aggregator_payload"):
            logger.info(
                "[聚合器直通] PancakeSwap V2 无 %s/%s，改走聚合合约执行",
                path[0][:10], path[-1][:10],
            )
            # 在这里计算 scaled_in/scaled_out_min（sell 场景也处理）
            sell_like_pre = (
                "TokensForETH" in method_name
                or "TokensForTokens" in method_name
                or method_name == "customSellHeuristic"
            )
            si_pre = int(amount_in * COPY_AMOUNT_RATIO)
            if sell_like_pre and si_pre <= 0:
                bal = _get_token_balance(w3, path[0], account.address)
                si_pre = int(bal * COPY_AMOUNT_RATIO)
                if si_pre <= 0 and bal > 0:
                    si_pre = bal
            if si_pre <= 0:
                logger.warning("[聚合器直通] 缩放后金额为 0，跳过")
                return None
            som_pre = int(amount_out_min * COPY_AMOUNT_RATIO * (10000 - SLIPPAGE_BPS) / 10000)
            return _execute_via_aggregator(w3, swap_info, account, si_pre, som_pre)
        logger.warning(
            "[跳过] PancakeSwap V2 无 %s/%s 交易对，该 token 可能在其他 DEX。",
            path[0][:10], path[-1][:10],
        )
        return None

    router = w3.eth.contract(
        address=w3.to_checksum_address(router_address),
        abi=UNISWAP_V2_ROUTER_ABI,
    )

    # 按比例缩放；卖出动作 amount_in 可能为 0（启发式识别），此时读取钱包余额
    scaled_in = int(amount_in * COPY_AMOUNT_RATIO)
    sell_like = (
        "TokensForETH" in method_name
        or "TokensForTokens" in method_name
        or method_name == "customSellHeuristic"
    )
    if sell_like and scaled_in <= 0:
        bal = _get_token_balance(w3, path[0], account.address)
        scaled_in = int(bal * COPY_AMOUNT_RATIO)
        if scaled_in <= 0 and bal > 0:
            scaled_in = bal  # 兜底：至少卖一笔完整余额
    if scaled_in <= 0:
        logger.warning("缩放后金额为 0，跳过")
        return None
    # 最少输出按跟单比例缩放，再扣滑点
    scaled_out_min = int(amount_out_min * COPY_AMOUNT_RATIO * (10000 - SLIPPAGE_BPS) / 10000)

    if "swapExactETHForTokens" in method_name and "SupportingFee" not in method_name:
        try:
            tx = router.functions.swapExactETHForTokens(
                scaled_out_min,
                path,
                to_addr,
                deadline,
            ).build_transaction({
                "from": account.address,
                "value": scaled_in,
                "gas": 300000,
            })
        except Exception as e:
            logger.warning("构建 swapExactETHForTokens 失败: %s", e)
            return None
    elif (method_name == "customBuyHeuristic") or ("swapExactETHForTokensSupportingFeeOnTransferTokens" in method_name):
        try:
            tx = router.functions.swapExactETHForTokensSupportingFeeOnTransferTokens(
                scaled_out_min,
                path,
                to_addr,
                deadline,
            ).build_transaction({
                "from": account.address,
                "value": scaled_in,
                "gas": 300000,
            })
        except Exception as e:
            logger.warning("构建 swapExactETHForTokensSupportingFeeOnTransferTokens 失败: %s", e)
            return None
    elif "TokensForETH" in method_name and "SupportingFee" not in method_name:
        if not _ensure_approve(w3, account, path[0], router_address, scaled_in):
            return None
        try:
            tx = router.functions.swapExactTokensForETH(
                scaled_in,
                scaled_out_min,
                path,
                to_addr,
                deadline,
            ).build_transaction({
                "from": account.address,
                "gas": 300000,
            })
        except Exception as e:
            logger.warning("构建 swapExactTokensForETH 失败: %s", e)
            return None
    elif (method_name == "customSellHeuristic") or ("swapExactTokensForETHSupportingFeeOnTransferTokens" in method_name):
        if not _ensure_approve(w3, account, path[0], router_address, scaled_in):
            return None
        try:
            tx = router.functions.swapExactTokensForETHSupportingFeeOnTransferTokens(
                scaled_in,
                scaled_out_min,
                path,
                to_addr,
                deadline,
            ).build_transaction({
                "from": account.address,
                "gas": 300000,
            })
        except Exception as e:
            logger.warning("构建 swapExactTokensForETHSupportingFeeOnTransferTokens 失败: %s", e)
            return None
    elif "swapExactTokensForTokens" in method_name and "SupportingFee" not in method_name:
        if not _ensure_approve(w3, account, path[0], router_address, scaled_in):
            return None
        try:
            tx = router.functions.swapExactTokensForTokens(
                scaled_in,
                scaled_out_min,
                path,
                to_addr,
                deadline,
            ).build_transaction({
                "from": account.address,
                "gas": 350000,
            })
        except Exception as e:
            logger.warning("构建 swapExactTokensForTokens 失败: %s", e)
            return None
    elif "swapExactTokensForTokensSupportingFeeOnTransferTokens" in method_name:
        if not _ensure_approve(w3, account, path[0], router_address, scaled_in):
            return None
        try:
            tx = router.functions.swapExactTokensForTokensSupportingFeeOnTransferTokens(
                scaled_in,
                scaled_out_min,
                path,
                to_addr,
                deadline,
            ).build_transaction({
                "from": account.address,
                "gas": 350000,
            })
        except Exception as e:
            logger.warning("构建 swapExactTokensForTokensSupportingFeeOnTransferTokens 失败: %s", e)
            return None
    else:
        logger.warning("不支持的 swap 方法: %s", method_name)
        return None

    # 填链 ID 与 nonce
    tx["chainId"] = w3.eth.chain_id
    tx["nonce"] = w3.eth.get_transaction_count(account.address, "pending")
    gas_est = int(tx.get("gas", 300000))
    val_est = int(tx.get("value", 0) or 0)
    need = _estimate_min_bnb_for_tx(w3, val_est, gas_est)
    try:
        if w3.eth.get_balance(account.address) < need:
            _log_bnb_shortfall(w3, account, need, "(Router 路径)")
            return None
    except Exception:
        pass
    signed = account.sign_transaction(tx)
    try:
        tx_hash = w3.eth.send_raw_transaction(signed.raw_transaction)
        logger.info("跟单交易已发送: %s", tx_hash.hex())
        return tx_hash.hex()
    except Exception as e:
        err = str(e)
        if "insufficient funds" in err.lower():
            _log_bnb_shortfall(w3, account, need, "(节点拒绝: insufficient funds)")
        logger.warning("发送跟单交易失败: %s（BNB 不足 / nonce / gas 或需先 approve Router）", e)
        return None
