# -*- coding: utf-8 -*-
"""
跟单脚本入口：
  - WebSocket pending 订阅（快速捕获，依赖节点）
  - HTTP 区块轮询（兜底，每 3s 扫一次新区块，一条不漏）
  - 止盈检查线程（每 N 秒查持仓盈亏，达标自动卖出）
卖出条件（满足任一即卖）：
  1. 盈利 ≥ TAKE_PROFIT_PCT（如 30%）
  2. 领袖卖出同一代币
"""
import asyncio
import logging
import sys
import time
from threading import Thread
from typing import Optional

from web3 import Web3
from web3.middleware import ExtraDataToPOAMiddleware

import config
from config import (
    COPY_AMOUNT_RATIO,
    COPY_SELL_ACTIONS,
    EXECUTE_COPY,
    LEADER_ADDRESSES,
    RPC_HTTP_FALLBACKS,
    RPC_HTTP_URL,
    RPC_WS_URL,
    TAKE_PROFIT_CHECK_INTERVAL,
    TAKE_PROFIT_PCT,
    validate_config,
)
import runtime_controls as rc
from decoder import (
    QUOTE_TOKENS_BSC,
    get_trade_action,
    get_trade_type_and_summary,
    parse_leader_tx,
)
from executor import execute_copy_tx
from monitor import monitor_pending_transactions
from positions import PositionTracker

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger(__name__)


def _normalize_hash(h) -> str:
    """统一为小写 0x 前缀，保证 WS 与区块两路对同一笔 tx 判定一致。"""
    if h is None:
        return ""
    if hasattr(h, "hex"):
        s = h.hex()
    else:
        s = str(h)
    if not s:
        return ""
    if not s.startswith("0x"):
        s = "0x" + s
    return s.lower()


def _tx_looks_pending(tx: dict) -> bool:
    """未入块时收据往往不存在，解析会失败，应等区块线程带 blockNumber 再处理。"""
    bn = tx.get("blockNumber")
    if bn is None:
        return True
    if isinstance(bn, str) and bn.lower() == "pending":
        return True
    return False


def _token_leader_disposes(swap_info: dict) -> Optional[str]:
    """
    领袖这笔若在减持某个代币（换成 BNB/稳定币或其它币），返回 path 里被卖掉的 token。
    buy 不会返回；「非主流→非主流」在 decoder 里是 action=swap，也必须跟卖 path[0]。
    """
    path = swap_info.get("path") or []
    if len(path) < 2:
        return None
    action = get_trade_action(swap_info)
    t0 = (path[0] or "").lower()
    t1 = (path[-1] or "").lower()
    quotes = QUOTE_TOKENS_BSC

    if action == "sell":
        return path[0]
    if action != "swap":
        return None
    # 双稳定/双计价互换：不跟
    if t0 in quotes and t1 in quotes:
        return None
    # 起点是非计价币：视为在卖掉 path[0]（含 alt→alt）
    if t0 not in quotes:
        return path[0]
    return None


def make_callback(tracker: PositionTracker):
    seen_hashes: set = set()
    executed_buy_hashes: set = set()  # 已为此领袖 tx 执行过跟单买入，防止重复下单
    import threading
    _seen_lock = threading.Lock()

    def on_leader_tx(tx: dict, source: str = ""):
        w3 = tracker.get_w3()
        raw_hash = tx.get("hash")
        tx_hash_hex = _normalize_hash(raw_hash)
        if not tx_hash_hex:
            return

        swap_info = parse_leader_tx(tx, w3)
        if not swap_info:
            # 勿在「解析失败」时立刻去重：pending 无收据会误判，入块后应再解析（否则跟卖/收据解码永久丢失）
            if _tx_looks_pending(tx):
                logger.debug(
                    "[解析待收据%s] 无收据先跳过，入块后再解析 tx=%s...%s",
                    f"/{source}" if source else "",
                    tx_hash_hex[:12],
                    tx_hash_hex[-8:],
                )
                return
            to_addr = tx.get("to") or "合约创建"
            value_bnb = int(tx.get("value") or 0) / 1e18
            data = tx.get("input") or tx.get("data") or ""
            selector = ""
            try:
                selector = (str(data)[:10] if data else "").lower()
            except Exception:
                selector = ""
            logger.info(
                "[非swap%s] from=%s to=%s value=%.6f BNB selector=%s tx=%s...%s",
                f"/{source}" if source else "",
                (tx.get("from") or "")[:10],
                (to_addr or "")[:10],
                value_bnb,
                selector or "(none)",
                tx_hash_hex[:12],
                tx_hash_hex[-8:],
            )
            with _seen_lock:
                seen_hashes.add(tx_hash_hex)
                if len(seen_hashes) > 10000:
                    seen_hashes.clear()
                    executed_buy_hashes.clear()
            return

        # 已成功解析：再按 hash 去重（避免 WS 与区块各处理一次）
        with _seen_lock:
            if tx_hash_hex in seen_hashes:
                return
            seen_hashes.add(tx_hash_hex)
            if len(seen_hashes) > 10000:
                seen_hashes.clear()
                executed_buy_hashes.clear()

        action = get_trade_action(swap_info)
        trade_type, summary = get_trade_type_and_summary(swap_info, tx_hash_hex)
        src_tag = f"/{source}" if source else ""
        logger.info("[%s%s][action=%s] %s", trade_type, src_tag, action, summary)

        path = swap_info.get("path") or []

        # ── 领袖减持（含 sell 与 decoder 标成 swap 的 alt→alt）→ 跟卖 ──
        disposed = _token_leader_disposes(swap_info)
        if disposed is not None and rc.effective_execute_copy():
            if not rc.effective_copy_sell():
                logger.info(
                    "[跟卖已关闭] 领袖减持 %s… 请开 COPY_SELL_ACTIONS 或控制台「领袖跟卖」",
                    disposed[:10],
                )
                return
            if tracker.has_position(disposed):
                logger.info("[跟卖触发] 领袖卖出 %s，我们也持有，执行卖出", disposed[:10])
                sell_hash = tracker.trigger_leader_sell(disposed, sell_swap_info=swap_info)
                if sell_hash:
                    logger.info("[跟卖成功] tx=%s", sell_hash)
                else:
                    logger.warning("[跟卖失败] 已触发但未拿到交易哈希，请看上方 positions 日志")
                return
            logger.info(
                "[跟卖跳过] 领袖减持 %s… 我们无该代币未平仓（类型=%s）",
                disposed[:10],
                trade_type,
            )
            return

        # ── 跟单买入（同一笔领袖 tx 只跟一次，防止 WS+区块 重复触发） ──
        if rc.effective_execute_copy() and action == "buy":
            # 仅当「这笔要跟的代币」已有未平仓时跳过；其它代币持仓不影响
            if len(path) >= 2:
                target_token = path[-1]
                if tracker.has_position(target_token):
                    logger.info(
                        "[跳过跟买] 代币 %s… 已有未平仓，不重复开仓",
                        target_token[:10],
                    )
                    rc.record_event(
                        "跳过跟买",
                        "该代币已有未平仓",
                        token=(target_token or "")[:14],
                    )
                    return
            with _seen_lock:
                if tx_hash_hex in executed_buy_hashes:
                    logger.debug("[去重] 领袖 tx %s 已跟单，跳过", tx_hash_hex[:16])
                    return
                executed_buy_hashes.add(tx_hash_hex)
                if len(executed_buy_hashes) > 10000:
                    executed_buy_hashes.clear()
            router = swap_info.get("router") or tx.get("to")
            if not router:
                logger.warning("无法确定 Router 地址，跳过跟单")
                return
            copy_hash = execute_copy_tx(w3, swap_info, router)
            if copy_hash and len(path) >= 2:
                bought_token = path[-1]
                cost_bnb = int(swap_info["amount_in"] * COPY_AMOUNT_RATIO)
                tracker.record_buy(
                    token_address=bought_token,
                    cost_bnb=cost_bnb,
                    router_address=router,
                    buy_path=list(path),
                    buy_tx_hash=copy_hash,
                    aggregator_addr=swap_info.get("_aggregator_addr", ""),
                )
                logger.info(
                    "[跟单买入成功] tx=%s 代币=%s 成本=%.6f BNB (比例=%.4f)",
                    copy_hash, bought_token[:10], cost_bnb / 1e18, COPY_AMOUNT_RATIO,
                )
                rc.record_event(
                    "跟买",
                    "跟单买入已发送",
                    tx=copy_hash,
                    token=bought_token[:14],
                    cost_bnb=round(cost_bnb / 1e18, 8),
                )
            elif copy_hash:
                logger.info("[跟单成功] tx=%s (比例=%.4f)", copy_hash, COPY_AMOUNT_RATIO)
                rc.record_event("跟买", "跟单已发送(未记仓)", tx=copy_hash)
            else:
                logger.warning("[跟单发送失败]")
                rc.record_event("跟买失败", "发送失败或未返回交易", leader_tx=tx_hash_hex[:18])

    return on_leader_tx


def _make_w3_http(url: str) -> Web3:
    from web3.middleware import ExtraDataToPOAMiddleware
    w3 = Web3(Web3.HTTPProvider(
        url,
        request_kwargs={"proxies": {"http": None, "https": None}, "timeout": 8},
    ))
    w3.middleware_onion.inject(ExtraDataToPOAMiddleware, layer=0)
    return w3


def _block_poll_thread(leader_addresses: set, callback, tracker: PositionTracker, poll_interval: float = 3.0):
    """区块扫描线程：任何异常立刻切换到下一个节点，循环轮询所有备用节点。"""
    leader_set = {a.lower() for a in leader_addresses}
    last_block = None
    node_index = 0
    nodes = list(dict.fromkeys([RPC_HTTP_URL] + RPC_HTTP_FALLBACKS))

    def current_url():
        return nodes[node_index % len(nodes)]

    def switch_node(reason: str):
        nonlocal node_index, w3
        node_index += 1
        w3 = _make_w3_http(current_url())
        tracker.update_w3(w3)
        logger.warning("[区块扫描] 切换节点 → %s  原因: %s", current_url(), reason[:80])

    w3 = _make_w3_http(current_url())
    logger.info("[区块扫描] 使用节点: %s", current_url())

    while True:
        try:
            cur = w3.eth.block_number
            if last_block is None:
                last_block = max(cur - 3, 0)
                logger.info("[区块扫描] 从区块 #%d 开始监控（含最近回扫）", last_block + 1)

            processed_up_to = last_block
            for bn in range(last_block + 1, cur + 1):
                try:
                    block = w3.eth.get_block(bn, full_transactions=True)
                    for tx in (block.get("transactions") or []):
                        tx_dict = dict(tx) if hasattr(tx, "items") else tx
                        from_addr = (tx_dict.get("from") or "").lower()
                        to_addr = (tx_dict.get("to") or "").lower() if tx_dict.get("to") else ""
                        if from_addr in leader_set or to_addr in leader_set:
                            # 不预拉 receipt：聚合器 calldata 可直接解码，receipt 由 decoder 按需懒加载
                            callback(tx_dict, source="区块")
                    processed_up_to = bn
                except Exception as e:
                    switch_node(str(e))
                    break

            last_block = processed_up_to
        except Exception as e:
            switch_node(str(e))

        time.sleep(poll_interval)


async def run():
    validate_config()

    # 启动时依次尝试所有节点，直到连通为止
    all_nodes = list(dict.fromkeys([RPC_HTTP_URL] + RPC_HTTP_FALLBACKS))
    w3 = None
    for url in all_nodes:
        candidate = _make_w3_http(url)
        try:
            if candidate.is_connected():
                w3 = candidate
                logger.info("[启动] 使用节点: %s", url)
                break
        except Exception:
            pass
        logger.warning("[启动] 节点不可用，跳过: %s", url)

    if w3 is None:
        logger.error("所有 RPC 节点均不可用，请检查网络或节点配置")
        sys.exit(1)

    leaders = set(LEADER_ADDRESSES)

    # 仓位追踪 + 止盈
    tracker = PositionTracker(
        w3=w3,
        take_profit_pct=TAKE_PROFIT_PCT,
        check_interval=TAKE_PROFIT_CHECK_INTERVAL,
    )
    tracker.load_persistent_positions()
    tracker.reconcile_on_startup()
    callback = make_callback(tracker)

    logger.info(
        "BSC 监控启动 | 领袖: %s | 执行跟单=%s | 跟卖=%s | 止盈=%.0f%% | 检查间隔=%.0fs",
        list(leaders), EXECUTE_COPY, COPY_SELL_ACTIONS, TAKE_PROFIT_PCT, TAKE_PROFIT_CHECK_INTERVAL,
    )

    if config.DASHBOARD_ENABLE:
        dash_err = config.dashboard_config_error()
        if dash_err:
            logger.error("网页控制台未启动：%s。", dash_err)
        else:
            if config.dashboard_bind_is_all_interfaces():
                logger.warning(
                    "控制台监听 %s（全网卡）：务必在云安全组只放行你自己的 IP，并保管好 DASHBOARD_TOKEN。",
                    config.DASHBOARD_HOST,
                )
            from web_dashboard import start_dashboard_background

            start_dashboard_background(tracker)
            port = config.DASHBOARD_PORT
            host = config.DASHBOARD_HOST
            logger.info(
                "网页控制台 http://%s:%s — 口令见 DASHBOARD_TOKEN（仅监听本机时请自行做 SSH 端口转发）",
                host,
                port,
            )

    # 子线程 1：区块轮询（带自动节点轮换）
    t1 = Thread(target=_block_poll_thread, args=(leaders, callback, tracker, 1.0), daemon=True)
    t1.start()

    # 子线程 2：止盈检查
    if EXECUTE_COPY:
        t2 = Thread(target=tracker.run_take_profit_loop, daemon=True)
        t2.start()
        logger.info("[止盈线程] 已启动，每 %.0fs 检查持仓盈亏", TAKE_PROFIT_CHECK_INTERVAL)

    # 主协程：WS pending 订阅（可选，无 WS 地址时纯轮询）
    if RPC_WS_URL:
        try:
            await monitor_pending_transactions(RPC_WS_URL, leaders, callback, http_url=RPC_HTTP_URL)
        except Exception:
            logger.warning("WS 监控退出，区块轮询继续运行，按 Ctrl+C 退出")
    else:
        logger.info("[模式] 无 WS 地址，纯 HTTP 区块轮询模式运行")

    # WS 退出或未配置时，保持主线程存活（区块轮询在子线程）
    while True:
        await asyncio.sleep(60)


def main():
    try:
        asyncio.run(run())
    except KeyboardInterrupt:
        logger.info("用户中断，退出")
        sys.exit(0)
    except Exception as e:
        logger.exception("运行异常: %s", e)
        sys.exit(1)


if __name__ == "__main__":
    main()
