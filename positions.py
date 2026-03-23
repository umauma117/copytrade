# -*- coding: utf-8 -*-
"""
仓位追踪 & 止盈卖出：
  - 跟单买入时记录持仓（代币、买入价、数量、Router、path）
  - 后台线程周期性通过 Router.getAmountsOut 查询当前价格
  - 盈利 ≥ TAKE_PROFIT_PCT 时自动卖出
  - 领袖卖出时也自动卖出（由 main.py 调用 trigger_leader_sell）
"""
import logging
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from eth_account import Account
from web3 import Web3

from abi import UNISWAP_V2_ROUTER_ABI
from config import FOLLOWER_PRIVATE_KEY, SLIPPAGE_BPS

logger = logging.getLogger(__name__)

# 聚合器 swapType=5（pool=token）模式下，链上观测到的“实际调用 transferFrom 的中间合约/助手”。
# 由于 pending 交易到达顺序不可控，可能出现跟卖时未先捕获领袖 approve，导致缺少该 spender 授权。
# 给它做兜底 approve 可以显著降低 ERC20: insufficient allowance 的概率。
_KNOWN_TRANSFERFROM_SPENDER_FALLBACK_BSC: List[str] = [
    "0x5c952063c7fc8610ffdb798152d69f0b9550762b",
]


@dataclass
class Position:
    token_address: str          # 买到的代币合约地址（checksum）
    amount: int                 # 买入时收到的代币数量（wei）
    cost_bnb: int               # 花了多少 BNB（wei）
    router_address: str         # 买入时用的 Router
    buy_path: List[str]         # 买入 path，如 [WBNB, Token]
    buy_tx_hash: str = ""       # 买入交易 hash
    sold: bool = False          # 是否已卖出
    sell_tx_hash: str = ""
    # 聚合器卖出支持：若买入走聚合器，存储聚合合约地址和最新一次领袖卖出的 payload
    aggregator_addr: str = ""
    aggregator_sell_payload: bytes = b""
    # 领袖 approve 过的额外 spender（如中间路由），卖出时需一并授权；止盈跟卖会复用
    extra_approve_spenders: List[str] = field(default_factory=list)


# ERC20 balanceOf ABI（查余额）
_ERC20_ABI = [
    {
        "constant": True,
        "inputs": [{"name": "_owner", "type": "address"}],
        "name": "balanceOf",
        "outputs": [{"name": "balance", "type": "uint256"}],
        "type": "function",
    }
]


class PositionTracker:
    """线程安全的仓位管理器。"""

    def __init__(self, w3: Web3, take_profit_pct: float, check_interval: float = 10.0):
        self.w3 = w3
        self._w3_lock = threading.Lock()
        self.take_profit_pct = take_profit_pct
        self.check_interval = check_interval
        self._positions: Dict[str, Position] = {}   # token_lower -> Position
        self._lock = threading.Lock()

    def update_w3(self, w3: Web3):
        """线程安全地更新 w3 实例（节点切换时调用）。"""
        with self._w3_lock:
            self.w3 = w3

    def get_w3(self) -> Web3:
        """获取当前 w3 实例。"""
        with self._w3_lock:
            return self.w3

    # ── 记录买入 ──────────────────────────────────────────────

    def record_buy(
        self,
        token_address: str,
        cost_bnb: int,
        router_address: str,
        buy_path: List[str],
        buy_tx_hash: str = "",
        aggregator_addr: str = "",
    ):
        """跟单买入成功后调用，记录仓位。"""
        key = token_address.lower()
        # 查真实余额
        balance = self._get_token_balance(token_address)
        with self._lock:
            if key in self._positions and not self._positions[key].sold:
                old = self._positions[key]
                old.amount = balance
                old.cost_bnb += cost_bnb
                if aggregator_addr:
                    old.aggregator_addr = aggregator_addr
                logger.info("[仓位] 加仓 %s, 余额=%d, 累计成本=%d wei BNB", key[:10], balance, old.cost_bnb)
            else:
                pos = Position(
                    token_address=token_address,
                    amount=balance,
                    cost_bnb=cost_bnb,
                    router_address=router_address,
                    buy_path=buy_path,
                    buy_tx_hash=buy_tx_hash,
                    aggregator_addr=aggregator_addr,
                )
                self._positions[key] = pos
                logger.info(
                    "[仓位] 新建 %s, 余额=%d, 成本=%d wei BNB, router=%s%s",
                    key[:10], balance, cost_bnb, router_address[:10],
                    f" [聚合器]" if aggregator_addr else "",
                )

        # 聚合器买入：提前 approve，让后续 eth_call 估值和跟卖可以立即执行
        # 注意：record_buy 在 buy tx 上链前就被调用，此时余额可能为 0，
        # 但 approve 与余额无关，无论余额多少都应该提前授权
        if aggregator_addr and FOLLOWER_PRIVATE_KEY:
            def _post_buy_setup():
                """后台等买入上链后刷新余额 + 预授权聚合器（仅1笔 approve）。"""
                import time as _t
                try:
                    account = Account.from_key(FOLLOWER_PRIVATE_KEY)
                    for _ in range(15):
                        real_bal = self._get_token_balance(token_address)
                        if real_bal > 0:
                            with self._lock:
                                pos_now = self._positions.get(token_address.lower())
                                if pos_now and not pos_now.sold:
                                    pos_now.amount = real_bal
                            logger.debug("[预授权] %s 余额已到账 %d", token_address[:10], real_bal)
                            break
                        _t.sleep(2)
                    self._approve_token(account, token_address, aggregator_addr, 2**256 - 1)
                except Exception as e:
                    logger.debug("[预授权] 买入后 approve 失败: %s", e)

            threading.Thread(target=_post_buy_setup, daemon=True).start()

    # ── 领袖卖出触发 ─────────────────────────────────────────

    def trigger_leader_sell(
        self,
        token_address: str,
        sell_swap_info: Optional[Dict[str, Any]] = None,
    ) -> Optional[str]:
        """领袖卖出时调用，若持有该代币则立即卖出。返回卖出 tx hash。"""
        key = token_address.lower()
        with self._lock:
            pos = self._positions.get(key)
            if not pos or pos.sold:
                return None
        # 若领袖卖出 tx 含聚合器 payload，更新仓位存储的卖出路由信息
        if sell_swap_info:
            extras = sell_swap_info.get("_extra_approve_spenders") or []
            if sell_swap_info.get("_aggregator_payload"):
                with self._lock:
                    pos.aggregator_addr = sell_swap_info.get("_aggregator_addr", "")
                    pos.aggregator_sell_payload = sell_swap_info["_aggregator_payload"]
            if extras:
                with self._lock:
                    have = {x.lower() for x in pos.extra_approve_spenders}
                    for s in extras:
                        if not s:
                            continue
                        sl = s.lower() if isinstance(s, str) else str(s).lower()
                        if sl not in have:
                            have.add(sl)
                            pos.extra_approve_spenders.append(sl)
        logger.info("[卖出触发] 领袖卖出 %s，执行跟卖", key[:10])
        return self._execute_sell(pos, reason="领袖卖出")

    # ── 止盈检查（后台线程调用）───────────────────────────────

    def check_take_profit(self):
        """遍历持仓，检查是否达到止盈线。"""
        with self._lock:
            open_positions = [
                (k, p) for k, p in self._positions.items() if not p.sold
            ]
        for key, pos in open_positions:
            try:
                if pos.aggregator_addr:
                    # 聚合器代币：通过「当前余额 / 买入数量」比例折算当前价值
                    current_value = self._get_current_value_bnb_aggregator(pos)
                else:
                    current_value = self._get_current_value_bnb(pos)
                if current_value <= 0 or pos.cost_bnb <= 0:
                    continue
                pnl_pct = (current_value - pos.cost_bnb) / pos.cost_bnb * 100
                logger.debug(
                    "[止盈检查] %s 成本=%.6f BNB 当前=%.6f BNB 盈亏=%.1f%%",
                    key[:10], pos.cost_bnb / 1e18, current_value / 1e18, pnl_pct,
                )
                if pnl_pct >= self.take_profit_pct:
                    logger.info(
                        "[止盈触发] %s 盈利 %.1f%% >= %.1f%%，执行卖出",
                        key[:10], pnl_pct, self.take_profit_pct,
                    )
                    self._execute_sell(pos, reason=f"止盈 {pnl_pct:.1f}%")
            except Exception as e:
                logger.debug("[止盈检查] %s 异常: %s", key[:10], e)

    def run_take_profit_loop(self):
        """在子线程中循环检查止盈。"""
        while True:
            try:
                self.check_take_profit()
            except Exception as e:
                logger.warning("[止盈循环] 异常: %s", e)
            time.sleep(self.check_interval)

    # ── 内部方法 ──────────────────────────────────────────────

    def _get_token_balance(self, token_address: str) -> int:
        """查询跟单钱包持有的 token 余额。"""
        if not FOLLOWER_PRIVATE_KEY:
            return 0
        try:
            account = Account.from_key(FOLLOWER_PRIVATE_KEY)
            token = self.w3.eth.contract(
                address=self.w3.to_checksum_address(token_address), abi=_ERC20_ABI
            )
            return token.functions.balanceOf(account.address).call()
        except Exception as e:
            logger.debug("查余额失败 %s: %s", token_address[:10], e)
            return 0

    def _get_current_value_bnb_aggregator(self, pos: Position) -> int:
        """
        聚合器代币止盈估值：通过 eth_call 模拟卖出，获取真实的当前 BNB 价值。
        需要 aggregator_sell_payload（领袖至少卖过一次后才有）。
        若 eth_call 失败，返回 0（跳过本次检查）。
        """
        if not pos.aggregator_sell_payload or not pos.aggregator_addr:
            # 还没有卖出 payload（领袖尚未卖过），无法估值
            return 0

        balance = self._get_token_balance(pos.token_address)
        if balance <= 0:
            return 0

        # 构造卖出 calldata：将 amountIn=当前余额, minReturn=0
        payload = pos.aggregator_sell_payload
        new_payload = (
            payload[:64]
            + balance.to_bytes(32, "big")
            + (0).to_bytes(32, "big")
            + payload[128:]
        )
        selector = bytes.fromhex("0b3f5cf9")
        calldata = selector + new_payload

        try:
            account = Account.from_key(FOLLOWER_PRIVATE_KEY)
            raw = self.w3.eth.call({
                "from": account.address,
                "to": self.w3.to_checksum_address(pos.aggregator_addr),
                "data": calldata,
            })
            if raw and len(raw) >= 32:
                return_amount = int.from_bytes(raw[:32], "big")
                if return_amount > 0:
                    logger.debug(
                        "[止盈估值] %s eth_call 得到 %.6f BNB",
                        pos.token_address[:10], return_amount / 1e18,
                    )
                    return return_amount
        except Exception as e:
            logger.debug("[止盈估值] %s eth_call 失败（未授权或不支持）: %s", pos.token_address[:10], e)

        return 0

    def _get_current_value_bnb(self, pos: Position) -> int:
        """通过 Router.getAmountsOut 查询当前持仓价值（wei BNB）。"""
        sell_path = list(reversed(pos.buy_path))
        balance = self._get_token_balance(pos.token_address)
        if balance <= 0:
            return 0
        try:
            router = self.w3.eth.contract(
                address=self.w3.to_checksum_address(pos.router_address),
                abi=UNISWAP_V2_ROUTER_ABI + [{
                    "inputs": [
                        {"internalType": "uint256", "name": "amountIn", "type": "uint256"},
                        {"internalType": "address[]", "name": "path", "type": "address[]"},
                    ],
                    "name": "getAmountsOut",
                    "outputs": [{"internalType": "uint256[]", "name": "amounts", "type": "uint256[]"}],
                    "stateMutability": "view",
                    "type": "function",
                }]
            )
            amounts = router.functions.getAmountsOut(balance, sell_path).call()
            return amounts[-1]
        except Exception as e:
            logger.debug("getAmountsOut 失败: %s", e)
            return 0

    def _approve_token(self, account, token_addr: str, spender: str, amount: int) -> bool:
        """向 spender 发起 ERC20 approve，若余额不足则跳过。"""
        _erc20_abi = [
            {
                "constant": False,
                "inputs": [
                    {"name": "_spender", "type": "address"},
                    {"name": "_value", "type": "uint256"},
                ],
                "name": "approve",
                "outputs": [{"name": "", "type": "bool"}],
                "type": "function",
            },
            {
                "constant": True,
                "inputs": [
                    {"name": "_owner", "type": "address"},
                    {"name": "_spender", "type": "address"},
                ],
                "name": "allowance",
                "outputs": [{"name": "", "type": "uint256"}],
                "type": "function",
            },
        ]
        try:
            token = self.w3.eth.contract(
                address=self.w3.to_checksum_address(token_addr), abi=_erc20_abi
            )
            # 先检查 allowance，已足够就跳过
            allowance = token.functions.allowance(account.address, self.w3.to_checksum_address(spender)).call()
            if allowance >= amount:
                return True
            nonce = self.w3.eth.get_transaction_count(account.address, "pending")
            tx = token.functions.approve(
                self.w3.to_checksum_address(spender),
                2**256 - 1,  # 无限授权
            ).build_transaction({
                "from": account.address,
                "gas": 100000,
                "nonce": nonce,
                "chainId": self.w3.eth.chain_id,
            })
            signed = account.sign_transaction(tx)
            tx_hash = self.w3.eth.send_raw_transaction(signed.raw_transaction)
            logger.info("[Approve] %s → %s tx=%s", token_addr[:10], spender[:10], tx_hash.hex())
            receipt = self.w3.eth.wait_for_transaction_receipt(tx_hash, timeout=30)
            if receipt.get("status") == 0:
                logger.warning("[Approve失败] %s approve 链上 revert (status=0)", token_addr[:10])
                return False
            return True
        except Exception as e:
            logger.warning("[Approve失败] %s: %s", token_addr[:10], e)
            return False

    def _debug_revert(self, account, tx_hash: str, pos: Position) -> None:
        """卖出失败时尽量打印 revert 原因与关键参数（不影响主流程）。"""
        try:
            tx = self.w3.eth.get_transaction(tx_hash)
        except Exception:
            tx = None
        if not tx:
            return
        try:
            # 用同样的 call 数据做一次 eth_call，通常能拿到 revert reason
            self.w3.eth.call({
                "from": account.address,
                "to": tx.get("to"),
                "data": tx.get("input"),
                "value": tx.get("value", 0),
            })
        except Exception as e:
            msg = str(e)
            logger.warning("[卖出revert原因] %s tx=%s %s", pos.token_address[:10], tx_hash, msg[:220])
            if "allowance" in msg.lower():
                logger.warning(
                    "[卖出失败说明] ERC20 授权不足：实际执行 transferFrom 的合约必须是已 approve 的 spender；"
                    "多跳卖出需对 payload 中每一跳的 pool 都授权（已按此逻辑修复）。"
                )
        try:
            if pos.aggregator_sell_payload:
                pools = self._parse_all_pool_addrs_from_payload(pos.aggregator_sell_payload)
                logger.warning(
                    "[卖出失败参数] token=%s agg=%s pools=%s",
                    pos.token_address[:10],
                    (pos.aggregator_addr or "")[:10],
                    ",".join(p[:10] for p in pools) if pools else "(无)",
                )
        except Exception:
            pass

    _MAX_SELL_RETRIES = 3

    def _execute_sell(self, pos: Position, reason: str = "") -> Optional[str]:
        """卖出持仓：将全部 token 换回 BNB，卖出后核查余额，失败自动重试。"""
        if pos.sold:
            return None
        if not FOLLOWER_PRIVATE_KEY:
            logger.warning("[卖出] 未配置私钥，跳过")
            return None

        account = Account.from_key(FOLLOWER_PRIVATE_KEY)

        for attempt in range(1, self._MAX_SELL_RETRIES + 1):
            balance = self._get_token_balance(pos.token_address)
            if balance <= 0:
                logger.info("[卖出] %s 余额为 0，认为已卖出", pos.token_address[:10])
                with self._lock:
                    pos.sold = True
                return pos.sell_tx_hash or None

            if attempt > 1:
                logger.info("[卖出重试] %s 第 %d 次尝试，余额=%d", pos.token_address[:10], attempt, balance)

            # ── 聚合器卖出路径 ──
            if pos.aggregator_addr and pos.aggregator_sell_payload:
                hash_hex = self._execute_sell_via_aggregator(pos, account, balance, reason)
            else:
                hash_hex = self._execute_sell_v2(pos, account, balance, reason)

            if not hash_hex:
                # 发送失败（非上链失败），稍等后重试
                if attempt < self._MAX_SELL_RETRIES:
                    time.sleep(3)
                continue

            # ── 等待链上确认 ──
            try:
                receipt = self.w3.eth.wait_for_transaction_receipt(hash_hex, timeout=60)
                if receipt.get("status") == 1:
                    # 成功：再确认余额归零
                    remaining = self._get_token_balance(pos.token_address)
                    if remaining > 0:
                        logger.warning(
                            "[卖出核查] %s 仍有余额 %d，可能税费扣除不足或部分成交，重试",
                            pos.token_address[:10], remaining,
                        )
                        with self._lock:
                            pos.sold = False  # 允许重试
                        time.sleep(2)
                        continue
                    logger.info("[卖出确认] %s 余额归零，卖出完成", pos.token_address[:10])
                    return hash_hex
                else:
                    logger.warning(
                        "[卖出链上失败] %s tx=%s status=0，重试",
                        pos.token_address[:10], hash_hex,
                    )
                    # 打印一次可读的 revert reason（尽量）
                    self._debug_revert(account, hash_hex, pos)
                    with self._lock:
                        pos.sold = False
                    time.sleep(2)
            except Exception as e:
                logger.warning("[卖出] 等待 receipt 超时或异常: %s，继续重试", e)
                with self._lock:
                    pos.sold = False
                time.sleep(3)

        logger.error("[卖出失败] %s 重试 %d 次后仍失败", pos.token_address[:10], self._MAX_SELL_RETRIES)
        return None

    def _execute_sell_v2(self, pos: Position, account, balance: int, reason: str) -> Optional[str]:
        """标准 PancakeSwap V2 卖出（不等链上确认，由 _execute_sell 统一处理）。"""
        if not self._approve_token(account, pos.token_address, pos.router_address, balance):
            logger.warning("[V2卖出] approve Router 失败，中止")
            return None
        sell_path = list(reversed(pos.buy_path))
        router = self.w3.eth.contract(
            address=self.w3.to_checksum_address(pos.router_address),
            abi=UNISWAP_V2_ROUTER_ABI,
        )
        deadline = int(time.time()) + 300
        amount_out_min = 0
        try:
            current_value = self._get_current_value_bnb(pos)
            if current_value > 0:
                amount_out_min = int(current_value * (10000 - SLIPPAGE_BPS) / 10000)
        except Exception:
            pass
        try:
            tx = router.functions.swapExactTokensForETHSupportingFeeOnTransferTokens(
                balance, amount_out_min, sell_path, account.address, deadline,
            ).build_transaction({"from": account.address, "gas": 300000})
        except Exception as e:
            logger.warning("[V2卖出] 构建交易失败: %s", e)
            return None
        tx["chainId"] = self.w3.eth.chain_id
        tx["nonce"] = self.w3.eth.get_transaction_count(account.address, "pending")
        signed = account.sign_transaction(tx)
        try:
            tx_hash = self.w3.eth.send_raw_transaction(signed.raw_transaction)
            hash_hex = tx_hash.hex()
            with self._lock:
                pos.sold = True
                pos.sell_tx_hash = hash_hex
            logger.info("[V2卖出已发送] %s 数量=%d 原因=%s tx=%s",
                        pos.token_address[:10], balance, reason, hash_hex)
            return hash_hex
        except Exception as e:
            logger.warning("[V2卖出] 发送交易失败: %s", e)
            return None

    @staticmethod
    def _parse_pool_addr_from_payload(payload: bytes) -> str:
        """兼容旧逻辑：仅 desc[0] 的 pool。"""
        pools = PositionTracker._parse_all_pool_addrs_from_payload(payload)
        return pools[0] if pools else ""

    @staticmethod
    def _parse_all_pool_addrs_from_payload(payload: bytes) -> List[str]:
        """
        从每个 desc 的第 4 个 word 提取 pool（与 decoder 中 desc 布局一致）。
        多跳卖出时，每一跳的 pool 都可能调用 transferFrom，需分别 approve。
        """
        _zero = "0x" + "0" * 40
        out: List[str] = []
        try:
            if len(payload) < 6 * 32:
                return out
            n_descs = int.from_bytes(payload[4 * 32 : 5 * 32], "big")
            if n_descs <= 0 or n_descs > 10:
                return out
            offsets_start_byte = 5 * 32
            for k in range(n_descs):
                off = int.from_bytes(payload[(5 + k) * 32 : (6 + k) * 32], "big")
                desc_byte = offsets_start_byte + off
                if desc_byte + 128 > len(payload):
                    continue
                pool_addr = "0x" + payload[desc_byte + 96 + 12 : desc_byte + 128].hex()
                if pool_addr and pool_addr != _zero:
                    out.append(pool_addr.lower())
        except Exception:
            pass
        # 去重保序
        seen = set()
        uniq = []
        for a in out:
            if a not in seen:
                seen.add(a)
                uniq.append(a)
        return uniq

    _SELL_TEMPLATE_WORDS = [
        0x80, 0, 0, 0,
        1, 0x20,
        5, 0, 0, 0,
        0, 1, 0, 0x140, 0, 0, 0xa0, 0, 0, 0,
        0x64, 0xb8159ba378904f803639d274cec79f788931c9c8,
    ]

    def _build_fallback_sell_calldata(self, token_addr: str, amount: int) -> bytes:
        """当领袖 payload 卖出失败时，使用模板构建 swapType=5 卖出 calldata。"""
        token_int = int(token_addr, 16)
        words = list(self._SELL_TEMPLATE_WORDS)
        words[2] = amount
        words[7] = token_int
        words[9] = token_int
        payload = b"".join(w.to_bytes(32, "big") for w in words)
        return bytes.fromhex("0b3f5cf9") + payload

    def _execute_sell_via_aggregator(
        self,
        pos: Position,
        account,
        balance: int,
        reason: str,
    ) -> Optional[str]:
        """
        通过聚合器卖出。只 approve 聚合器和 pool（最多2笔）。
        swapType=5 且 pool=token 时优先用与 force_sell 一致的模板 calldata，提高成功率。
        """
        payload = pos.aggregator_sell_payload
        pool_addrs = self._parse_all_pool_addrs_from_payload(payload) if payload else []
        pool_addr = pool_addrs[0] if pool_addrs else ""
        _zero = "0x" + "0" * 40
        token_lower = pos.token_address.lower()
        # 与 force_sell 一致：swapType=5 且 pool=token 时用固定模板，避免领袖 payload 结构导致失败
        use_template = (
            pool_addr
            and pool_addr != _zero
            and pool_addr.lower() == token_lower
        )
        if use_template:
            calldata = self._build_fallback_sell_calldata(pos.token_address, balance)
            logger.debug("[聚合器卖出] 使用 swapType=5 模板 (pool=token)")
        else:
            if not payload or len(payload) < 128:
                calldata = self._build_fallback_sell_calldata(pos.token_address, balance)
            else:
                new_payload = (
                    payload[:64]
                    + balance.to_bytes(32, "big")
                    + (0).to_bytes(32, "big")
                    + payload[128:]
                )
                calldata = bytes.fromhex("0b3f5cf9") + new_payload

        # ── approve：聚合器 + payload 里每一跳的 pool（多跳时缺一即 insufficient allowance） ──
        if not self._approve_token(account, pos.token_address, pos.aggregator_addr, balance):
            logger.warning("[聚合器卖出] approve 聚合合约失败，中止")
            return None
        to_approve = list(pool_addrs)
        if use_template and token_lower not in {p.lower() for p in to_approve}:
            to_approve.append(pos.token_address)
        for p in to_approve:
            if not p or p == _zero:
                continue
            pl = p.lower() if isinstance(p, str) else str(p).lower()
            if pl == pos.aggregator_addr.lower():
                continue
            self._approve_token(account, pos.token_address, p, balance)

        # 领袖单独 approve 的中间合约（链上常见：先 approve 再给聚合器调）
        for s in pos.extra_approve_spenders or []:
            if not s:
                continue
            sl = s.lower() if isinstance(s, str) else str(s).lower()
            if sl == pos.aggregator_addr.lower():
                continue
            if sl in {p.lower() for p in to_approve if p}:
                continue
            self._approve_token(account, pos.token_address, s, balance)

        # 兜底：swapType=5(pool=token) 可能由特定 helper/spender 执行 transferFrom
        if use_template:
            for fs in _KNOWN_TRANSFERFROM_SPENDER_FALLBACK_BSC:
                if not fs:
                    continue
                self._approve_token(account, pos.token_address, fs, balance)

        # ── 直接发送交易 ──
        try:
            nonce = self.w3.eth.get_transaction_count(account.address, "pending")
            gas_price = self.w3.eth.gas_price
            tx = {
                "to": self.w3.to_checksum_address(pos.aggregator_addr),
                "data": calldata,
                "value": 0,
                "gas": 500000,
                "gasPrice": int(gas_price * 1.1),
                "nonce": nonce,
                "chainId": self.w3.eth.chain_id,
            }
            signed = account.sign_transaction(tx)
            tx_hash = self.w3.eth.send_raw_transaction(signed.raw_transaction)
            hash_hex = tx_hash.hex()
            with self._lock:
                pos.sold = True
                pos.sell_tx_hash = hash_hex
            logger.info("[聚合器卖出已发送] %s 数量=%d 原因=%s tx=%s",
                        pos.token_address[:10], balance, reason, hash_hex)
            return hash_hex
        except Exception as e:
            logger.warning("[聚合器卖出] 发送交易失败: %s", e)
            return None

    # ── 状态查看 ──────────────────────────────────────────────

    def get_open_positions(self) -> List[Position]:
        with self._lock:
            return [p for p in self._positions.values() if not p.sold]

    def has_position(self, token_address: str) -> bool:
        key = token_address.lower()
        with self._lock:
            pos = self._positions.get(key)
            return pos is not None and not pos.sold
