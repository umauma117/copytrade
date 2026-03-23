# -*- coding: utf-8 -*-
"""
强制卖出脚本：立即卖出指定代币（或自动查所有聚合器代币余额并卖出）。
用法：
  python3 force_sell.py                   # 自动查余额并卖出所有有余额的代币
  python3 force_sell.py 0xABC... 0xDEF... # 只卖出指定代币
"""
import sys
import time
from web3 import Web3
from eth_account import Account
from dotenv import load_dotenv
import os

load_dotenv()

PRIVATE_KEY = os.getenv("FOLLOWER_PRIVATE_KEY", "")
RPC_HTTP    = os.getenv("RPC_HTTP_URL", "https://bsc-dataseed.binance.org/")
AGGREGATOR  = "0x1de460f363AF910f51726DEf188F9004276Bf4bc"
WBNB        = "0xbb4CdB9CBd36B01bD1cBaEBF2De08d9173bc095c"

# ─── 本次跟单中买入过的代币（从日志中整理） ───────────────────────
# 启动时如果未传参数，会自动扫描这里的列表
KNOWN_TOKENS = [
    "0x756e6E89df7afd990cA7FDde86A5ad75498F21C8",
    "0x03ec849D45fA2a3f7FF698e038F7F95DfFE08B09",  # 可能已清仓
    "0x441c1c995aA661a3e7D00c8C37b2c27d73f3ED7e",  # 可能已清仓
    "0x5f8Bf07186083c3F3b9d3a19d04c1fE1fcFDcFd7",  # 可能已清仓
]

# ─── 模板：1-desc swapType=5 卖出 Token→BNB ────────────────────────
# 从链上已成功卖出 tx 0x36f34b38 逆向获得，结构固定
# fields: [0]descs_offset [1]feeToken [2]amountIn [3]minReturn
#         [4]n_descs [5]desc0_offset
#         desc0: [6]swapType=5 [7]tokenIn [8]tokenOut=0 [9]pool=tokenIn
#                [10..21] 固定内部结构（fee=1%，fee_recipient=0xb8159ba3）
SELL_TEMPLATE_WORDS = [
    0x80,                                                           # [0]
    0,                                                              # [1] feeToken
    0,                                                              # [2] amountIn → 替换
    0,                                                              # [3] minReturn=0
    1,                                                              # [4] n_descs
    0x20,                                                           # [5] desc[0] offset
    5,                                                              # [6] swapType=5
    0,                                                              # [7] tokenIn → 替换
    0,                                                              # [8] tokenOut=0 (BNB)
    0,                                                              # [9] pool → 替换 (=tokenIn)
    0,                                                              # [10]
    1,                                                              # [11]
    0,                                                              # [12]
    0x140,                                                          # [13]
    0,                                                              # [14]
    0,                                                              # [15]
    0xa0,                                                           # [16]
    0,                                                              # [17]
    0,                                                              # [18]
    0,                                                              # [19]
    0x64,                                                           # [20] fee=100bps=1%
    0xb8159ba378904f803639d274cec79f788931c9c8,                    # [21] fee recipient
]

SELECTOR = bytes.fromhex("0b3f5cf9")

_ERC20_ABI = [
    {"constant": True, "inputs": [{"name": "_owner", "type": "address"}],
     "name": "balanceOf", "outputs": [{"name": "", "type": "uint256"}], "type": "function"},
]
_APPROVE_ABI = [
    {"constant": False,
     "inputs": [{"name": "_spender", "type": "address"}, {"name": "_value", "type": "uint256"}],
     "name": "approve", "outputs": [{"name": "", "type": "bool"}], "type": "function"},
    {"constant": True,
     "inputs": [{"name": "_owner", "type": "address"}, {"name": "_spender", "type": "address"}],
     "name": "allowance", "outputs": [{"name": "", "type": "uint256"}], "type": "function"},
]


def build_sell_calldata(token_addr: str, amount: int) -> bytes:
    token_int = int(token_addr, 16)
    words = list(SELL_TEMPLATE_WORDS)
    words[2] = amount        # amountIn
    words[7] = token_int     # tokenIn
    words[9] = token_int     # pool = tokenIn
    payload = b"".join(w.to_bytes(32, "big") for w in words)
    return SELECTOR + payload


def ensure_approve(w3, account, token_addr, spender, amount):
    token = w3.eth.contract(address=w3.to_checksum_address(token_addr), abi=_APPROVE_ABI)
    try:
        allowance = token.functions.allowance(account.address, w3.to_checksum_address(spender)).call()
        if allowance >= amount:
            print(f"  [已授权] allowance 足够，跳过 approve")
            return True
    except Exception:
        pass
    nonce = w3.eth.get_transaction_count(account.address, "pending")
    tx = token.functions.approve(
        w3.to_checksum_address(spender), 2**256 - 1,
    ).build_transaction({
        "from": account.address, "gas": 100000,
        "nonce": nonce, "chainId": w3.eth.chain_id,
    })
    signed = account.sign_transaction(tx)
    th = w3.eth.send_raw_transaction(signed.raw_transaction)
    print(f"  [Approve] tx={th.hex()}")
    receipt = w3.eth.wait_for_transaction_receipt(th, timeout=30)
    if receipt.get("status") == 0:
        print(f"  [Approve 失败] 链上 revert")
        return False
    # 额外 approve pool（pool=token 自身）
    try:
        allowance2 = token.functions.allowance(account.address, w3.to_checksum_address(token_addr)).call()
        if allowance2 < amount:
            nonce2 = w3.eth.get_transaction_count(account.address, "pending")
            tx2 = token.functions.approve(
                w3.to_checksum_address(token_addr), 2**256 - 1,
            ).build_transaction({
                "from": account.address, "gas": 100000,
                "nonce": nonce2, "chainId": w3.eth.chain_id,
            })
            signed2 = account.sign_transaction(tx2)
            th2 = w3.eth.send_raw_transaction(signed2.raw_transaction)
            print(f"  [Approve pool] tx={th2.hex()}")
            w3.eth.wait_for_transaction_receipt(th2, timeout=30)
    except Exception as e:
        print(f"  [pool approve 跳过] {e}")
    return True


def sell_token(w3, account, token_addr):
    cs_addr = w3.to_checksum_address(token_addr)
    token = w3.eth.contract(address=cs_addr, abi=_ERC20_ABI)
    balance = token.functions.balanceOf(account.address).call()
    if balance == 0:
        print(f"  余额为 0，跳过")
        return

    print(f"  余额 = {balance}")
    if not ensure_approve(w3, account, token_addr, AGGREGATOR, balance):
        return

    calldata = build_sell_calldata(token_addr, balance)
    nonce = w3.eth.get_transaction_count(account.address, "pending")
    gas_price = w3.eth.gas_price
    tx = {
        "to": w3.to_checksum_address(AGGREGATOR),
        "data": calldata,
        "value": 0,
        "gas": 500000,
        "gasPrice": int(gas_price * 1.2),
        "nonce": nonce,
        "chainId": w3.eth.chain_id,
    }
    signed = account.sign_transaction(tx)
    try:
        th = w3.eth.send_raw_transaction(signed.raw_transaction)
        print(f"  [卖出已发送] tx={th.hex()}")
        receipt = w3.eth.wait_for_transaction_receipt(th, timeout=60)
        status = receipt.get("status")
        print(f"  [结果] {'成功 ✓' if status == 1 else '失败 ✗ (status=0)'}")
    except Exception as e:
        print(f"  [卖出失败] {e}")


def main():
    if not PRIVATE_KEY:
        print("错误：未配置 FOLLOWER_PRIVATE_KEY")
        sys.exit(1)

    w3 = Web3(Web3.HTTPProvider(RPC_HTTP))
    if not w3.is_connected():
        print(f"节点连接失败: {RPC_HTTP}")
        sys.exit(1)
    from web3.middleware import ExtraDataToPOAMiddleware
    w3.middleware_onion.inject(ExtraDataToPOAMiddleware, layer=0)

    account = Account.from_key(PRIVATE_KEY)
    print(f"钱包地址: {account.address}")
    print(f"BNB 余额: {w3.eth.get_balance(account.address) / 1e18:.6f} BNB\n")

    tokens = sys.argv[1:] if len(sys.argv) > 1 else KNOWN_TOKENS

    for token in tokens:
        print(f"─── 卖出 {token} ───")
        try:
            sell_token(w3, account, token)
        except Exception as e:
            print(f"  异常: {e}")
        print()


if __name__ == "__main__":
    main()
