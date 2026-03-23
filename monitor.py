# -*- coding: utf-8 -*-
"""监控领袖钱包的链上交易（pending 与已确认），含自动重连。"""
import asyncio
import json
import logging
import time
from typing import Callable, Optional, Set

import websockets
from web3 import Web3
from web3.middleware import ExtraDataToPOAMiddleware

logger = logging.getLogger(__name__)

# 每次重连等待的基础时间（秒），指数退避，最长 60 秒
_RECONNECT_BASE = 3
_RECONNECT_MAX = 60


def _make_w3(http_url: str) -> Web3:
    # proxies={"http": None, "https": None} 让 requests 跳过系统代理（SOCKS/HTTP），
    # 直连 RPC 节点，避免代理软件拦截导致 ProxyError / RemoteDisconnected
    w3 = Web3(Web3.HTTPProvider(
        http_url,
        request_kwargs={"proxies": {"http": None, "https": None}, "timeout": 10},
    ))
    w3.middleware_onion.inject(ExtraDataToPOAMiddleware, layer=0)
    return w3


def _fetch_tx_http(http_url: str, tx_hash: str) -> Optional[dict]:
    """用 HTTP RPC 同步拉取交易详情，避免为每条 hash 都开新 WebSocket 连接。"""
    w3 = _make_w3(http_url)
    try:
        tx = w3.eth.get_transaction(tx_hash)
        return dict(tx) if tx else None
    except Exception as e:
        logger.debug("HTTP 拉取 tx %s 失败: %s", tx_hash, e)
        return None


async def monitor_pending_transactions(
    ws_url: str,
    leader_addresses: Set[str],
    on_tx: Callable[[dict], None],
    http_url: Optional[str] = None,
):
    """
    通过 WebSocket 订阅 newPendingTransactions，收到 txHash 后拉取完整交易，
    若 from 在 leader_addresses 中则回调 on_tx(tx_dict)。
    断连后自动重连，指数退避最长 60 秒。
    """
    leader_addresses = {a.lower() for a in leader_addresses}
    seen: set = set()
    retry_delay = _RECONNECT_BASE

    # 推导 HTTP URL：ws -> http，wss -> https
    if not http_url:
        http_url = ws_url.replace("wss://", "https://").replace("ws://", "http://")

    # 复用同一个 Web3 实例拉取交易详情
    _w3_http = _make_w3(http_url)

    def _fetch_tx(tx_hash: str) -> Optional[dict]:
        try:
            tx = _w3_http.eth.get_transaction(tx_hash)
            return dict(tx) if tx else None
        except Exception as e:
            logger.debug("HTTP 拉取 tx %s 失败: %s", tx_hash, e)
            return None

    while True:
        try:
            async with websockets.connect(
                ws_url,
                ping_interval=None,   # 关闭客户端 ping，由服务端控制心跳
                close_timeout=10,
                open_timeout=20,
                max_size=2**22,
            ) as ws:
                # 订阅 pending 交易
                await ws.send(
                    json.dumps({
                        "jsonrpc": "2.0",
                        "id": 1,
                        "method": "eth_subscribe",
                        "params": ["newPendingTransactions"],
                    })
                )
                raw = await asyncio.wait_for(ws.recv(), timeout=15)
                sub = json.loads(raw)
                if "error" in sub:
                    raise RuntimeError(f"订阅失败: {sub['error']}")
                logger.info("已订阅 newPendingTransactions: %s", sub.get("result"))
                retry_delay = _RECONNECT_BASE  # 连接成功，重置退避

                while True:
                    try:
                        # 60 秒无消息则超时，下方只 continue（节点可能安静段）
                        msg = await asyncio.wait_for(ws.recv(), timeout=60)
                    except asyncio.TimeoutError:
                        logger.debug("60s 无消息，继续等待...")
                        continue

                    data = json.loads(msg)
                    raw = data.get("params", {}).get("result")
                    if not raw:
                        continue
                    # 与 main 一致：统一小写 0x，避免同一笔 tx 因大小写被处理两次
                    tx_hash = (raw if isinstance(raw, str) else str(raw)).strip()
                    if not tx_hash.startswith("0x"):
                        tx_hash = "0x" + tx_hash
                    tx_hash = tx_hash.lower()
                    if tx_hash in seen:
                        continue
                    seen.add(tx_hash)
                    if len(seen) > 20000:
                        seen.clear()

                    # 在线程池中调用同步 HTTP 请求，不阻塞事件循环
                    loop = asyncio.get_event_loop()
                    try:
                        tx = await loop.run_in_executor(None, _fetch_tx, tx_hash)
                        if not tx:
                            continue
                        from_addr = (tx.get("from") or "").lower()
                        to_addr = (tx.get("to") or "").lower() if tx.get("to") else ""
                        if from_addr in leader_addresses or to_addr in leader_addresses:
                            on_tx(tx)
                    except Exception as e:
                        logger.warning("处理 tx %s 失败: %s", tx_hash, e)

        except (
            websockets.exceptions.ConnectionClosedError,
            websockets.exceptions.ConnectionClosedOK,
            websockets.exceptions.WebSocketException,
            OSError,
            asyncio.TimeoutError,
        ) as e:
            logger.warning("WebSocket 断连: %s，%s 秒后重连...", e, retry_delay)
            await asyncio.sleep(retry_delay)
            retry_delay = min(retry_delay * 2, _RECONNECT_MAX)
        except Exception as e:
            logger.error("未知错误: %s，%s 秒后重连...", e, retry_delay)
            await asyncio.sleep(retry_delay)
            retry_delay = min(retry_delay * 2, _RECONNECT_MAX)


def run_monitor_blocks(
    rpc_url_http: str,
    leader_addresses: Set[str],
    on_tx: Callable[[dict], None],
    from_block: Optional[int] = None,
    poll_interval: float = 3.0,
):
    """
    轮询新区块，扫描区块内 from 在 leader_addresses 的交易并回调。
    适用于仅提供 HTTP RPC、无 WebSocket 的环境；延迟较高。
    """
    w3 = _make_w3(rpc_url_http)
    leader_addresses = {a.lower() for a in leader_addresses}
    last_block = from_block
    if last_block is None:
        last_block = w3.eth.block_number

    while True:
        try:
            current = w3.eth.block_number
            for bn in range(last_block + 1, current + 1):
                block = w3.eth.get_block(bn, full_transactions=True)
                for tx in block.get("transactions") or []:
                    from_addr = (tx.get("from") if isinstance(tx, dict) else tx["from"] or "").lower()
                    if from_addr in leader_addresses:
                        on_tx(dict(tx) if hasattr(tx, "items") else tx)
            last_block = current
        except Exception as e:
            logger.warning("轮询区块异常: %s", e)
        time.sleep(poll_interval)
