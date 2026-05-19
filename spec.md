# Portfolio Tracker — 需求规格书 v1

## 一、项目概述
资产跟踪程序，统计总资产和资产变化，DeFi 仓位管理与告警。
不做前端，通过 Discord slash 命令交互 + 定时推送。

## 二、数据口径
- **净值 = 总资产 - 总负债**（两个值都展示）
- **估值货币**：USD
- **代币归并**：延续 TOKEN_CONVERT 规则（stETH/WETH→ETH，USDT/USDC/DAI→USD 等）
- **合约持仓**：只看最终净值
- **地址脱敏**：Discord 展示时前6后4

## 三、数据源

### EVM 链上（30-40 个地址）
- DeBank API 全链扫描（API key 已有）
- DeFi 仓位明细 + 借贷健康度
- 不直连链上 RPC，全靠 DeBank

### Solana（10+ 个地址）
- 目标：达到 DeFi 仓位级别（借贷/LP 等）
- 先找免费数据源（Helius RPC / Solscan / Shyft），不行再考虑付费
- 先用 1 个地址测试

### 交易所（Binance / Bybit / Bitget / OKX）
- 直接用交易所 API Key（只读权限）
- 全量覆盖：现货、合约（U本位+币本位）、杠杆、理财/earn、借贷、质押、资金账户
- 密钥统一存 macOS Keychain（蛋蛋管理）

### 价格
- CoinGecko 为主（不调用交易所价格）
- API 挂了提示错误

## 四、告警
- **借贷健康度**：按协议推荐值（不手动设阈值），统一叫"健康度"
- **检查频率**：每 1 小时
- **告警策略**：触发 1 次就告警，冷却 2 小时
- **恢复通知**：健康度恢复安全后推送
- **只推 Discord**（频道 1476897040868053115）
- 其他告警类型后续再加

## 五、Discord 交互

### 推送频道
1476897040868053115（蛋蛋1号发送）

### Slash 命令
| 命令 | 说明 |
|------|------|
| /portfolio | 总览（净值/总资产/负债/按来源拆分） |
| /assets | 按币种列出持仓（Top N） |
| /assets all | 全量信息显示 |
| /defi | DeFi 仓位详情（含借贷健康度） |
| /exchange | 交易所信息显示（含借贷健康度） |
| /history | 历史净值趋势 |
| /refresh | 手动刷新（仅老板可用） |
| /alerts | 查看/设置告警规则（Discord 内可改） |

### 权限
- 只有老板能 refresh 和改配置

### 日报
- 每天早上 09:00 推送资产快照

## 六、地址管理
- 配置文件可改
- Discord 命令可动态增删
- 先不区分地址分组/类别

## 七、持久化 & 历史
- 永久保存
- 快照：每天一次（日报）+ 手动 refresh 也存
- 变化维度：日/周/月/年
- 统计维度：币种级别 + 总资产（来源先不考虑）

## 八、部署
- Mac Mini，Docker 化
- 数据请求尽量并行

## 九、旧代码复用
- PortfolioService 路径复用（砍 Aggregator）
- DeBank/Binance/Bybit adapter 复用
- TOKEN_CONVERT 归并规则复用
- Keychain 工具复用
- 前端弃用
- 新增：Bitget adapter、OKX adapter、Discord slash 命令、告警引擎、历史存储
