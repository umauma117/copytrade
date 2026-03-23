# 链上跟单脚本

通过监控指定钱包地址的链上交易，解析其与 Uniswap V2 兼容 DEX（如 Uniswap、PancakeSwap 等）的 swap 交易，并可选地使用跟单钱包自动执行同方向、同路径的跟单交易。

## 功能

- **监控领袖钱包**：使用 WebSocket 订阅 `newPendingTransactions`，实时捕获指定地址发出的交易。
- **解析 DEX Swap**：识别并解析 Uniswap V2 风格 Router 的常见 swap（如 `swapExactETHForTokens`、`swapExactTokensForETH`、`swapExactTokensForTokens` 及其 SupportingFeeOnTransferTokens 变体）。
- **跟单执行**：按配置比例与滑点，用跟单钱包向同一 Router 发起相同路径的 swap（仅当 `EXECUTE_COPY=true` 且已配置跟单私钥时）。

## 环境要求

- Python 3.9+
- 支持 WebSocket 的 RPC 节点（如 Alchemy、Infura 等），用于监控 pending 交易。

## 安装

```bash
cd copy-trading-bot
pip install -r requirements.txt
```

## 配置

1. 复制环境变量示例并编辑：

```bash
cp .env.example .env
# 编辑 .env，填入 RPC、领袖地址、可选跟单私钥等
```

2. 必填项：

| 变量 | 说明 |
|------|------|
| `RPC_WS_URL` | WebSocket RPC 地址，用于订阅 pending 交易 |
| `LEADER_ADDRESSES` | 要跟单的钱包地址，多个用英文逗号分隔 |

3. 执行跟单时还需：

| 变量 | 说明 |
|------|------|
| `FOLLOWER_PRIVATE_KEY` | 跟单钱包私钥（ hex，可带或不带 0x 前缀） |
| `EXECUTE_COPY` | 设为 `true` 时才会真正发跟单交易 |

4. 可选：

| 变量 | 说明 | 默认 |
|------|------|------|
| `RPC_HTTP_URL` | 区块扫描/发交易用 HTTP RPC（建议单独配置） | `https://bsc-dataseed1.binance.org` |
| `COPY_AMOUNT_RATIO` | 跟单金额相对领袖的比例（1.0 = 同比例） | 1.0 |
| `SLIPPAGE_BPS` | 滑点容忍，单位基点（50 表示 0.5%） | 50 |
| `COPY_SELL_ACTIONS` | 是否跟随卖出动作（false=只跟买） | true |

## 使用

- **仅监控、不跟单**：不设置 `FOLLOWER_PRIVATE_KEY`，或保持 `EXECUTE_COPY=false`（默认）。脚本会打印领袖的 swap 解析结果，不发送任何交易。

```bash
python main.py
```

- **开启跟单**：在 `.env` 中设置 `EXECUTE_COPY=true` 并填入 `FOLLOWER_PRIVATE_KEY`，再运行上述命令。脚本会在检测到领袖的 DEX swap 后，按比例与滑点自动发跟单交易。

注意：

- 跟单「ETH → Token」时，跟单钱包需有足够 ETH 并预留 gas。
- 跟单「Token → ETH」或「Token → Token」时，跟单钱包需持有足够数量的 path 中第一种 token，且已对该链上使用的 Router 做过 `approve`（脚本不会自动 approve）。
- 脚本内置的 Router 白名单包含 Uniswap V2、PancakeSwap V2 等常见地址；其他链或自定义 Router 若使用相同接口也会尝试解析并跟单。

## 风险与免责

- 跟单涉及链上资产与 gas，请仅在测试网或可承受损失的前提下使用。
- 私钥务必妥善保管，不要提交到版本库或泄露给他人。
- 本脚本按「所见即所跟」方式复制领袖的 swap 路径与方向，不构成投资建议；使用后果由使用者自行承担。

## 项目结构

```
copy-trading-bot/
├── main.py        # 入口：启动监控与跟单回调
├── config.py      # 从 .env 加载配置
├── monitor.py     # WebSocket 监控领袖地址的 pending 交易
├── decoder.py     # 解析 DEX swap 的 input data
├── executor.py    # 构建并发送跟单交易
├── abi.py         # Uniswap V2 Router 相关 ABI
├── requirements.txt
├── .env.example
└── README.md
```
