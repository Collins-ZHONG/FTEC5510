# FTEC5510

## 中期项目环境（Windows PowerShell）

需要 Python 3.11、uv 和网络访问。在项目根目录执行：

```powershell
uv venv --cache-dir .uv-cache --python 3.11 .venv
uv pip install --cache-dir .uv-cache --python .venv/Scripts/python.exe -r requirements.txt
$env:SOLCX_BINARY_PATH = (Join-Path (Get-Location) '.solcx')
.venv/Scripts/python.exe -c "import solcx; solcx.install_solc('0.8.20')"
.venv/Scripts/python.exe scripts/check_environment.py
```

最后一条命令编译并部署临时合约，在内存 EVM 上调用合约并检查状态；输出交易哈希和 Gas。每次运行都是新的内存链。编译器存放于项目的 `.solcx`，环境检查脚本会自动设置其路径；其他使用 `py-solc-x` 的脚本也应指定该目录。

无需激活环境即可使用上述命令；如需激活，可执行 `.venv/Scripts/Activate.ps1`。依赖清单固定直接依赖版本，间接依赖未锁定。

## 第 0 步接口约定

- 金额使用整数最小单位，50 表示 0.50；不使用浮点金额。
- 签名数据去除 `signature` 后以 `json.dumps(..., sort_keys=True, separators=(',', ':'), ensure_ascii=True)` 编码为 UTF-8，使用 `eth_account` 的 `encode_defunct` 签名；实现时字段类型必须明确一致。
- Owner 签署授权，Agent 对包含金额、收款方和幂等键的付款意图签名；验签时将恢复地址与预期签名者比较。授权需绑定 Agent，服务与收款方需通过授权或可信报价验证。
- 状态预留 `Reserved`、`DeliveryUncertain`、`Cancelled`、`Payable`、`Batched`、`Submitted`、`Settled`。预留及幂等检查在同一 SQLite 写事务内进行。

详细任务与验收标准见 [中期开发任务流程.md](中期开发任务流程.md) 和 [中期开发验收证据.md](中期开发验收证据.md)。

## 第 1 步：授权和风控

```powershell
.venv/Scripts/python.exe -m pytest -v tests/test_controls.py
```

`core/models.py` 定义带类型区分的签名记录；`core/crypto.py` 使用规范化 JSON 和 EIP-191 签名；`core/policy_guard.py` 检查签名、有效期、撤销、服务、收款方及单笔限额。调用方必须从可信网关状态提供 Owner、服务与收款方映射及撤销集合，不能直接采用请求中的值。网关需要在第二步的数据库事务中再次检查撤销状态并原子预留总预算；本步骤不提供防重放、幂等性或并发预算保护。

## 第 2 步：SQLite 账本和原子预留

```powershell
.venv/Scripts/python.exe -m pytest -v tests/test_ledger.py
```

`LedgerDB` 使用文件数据库、WAL 和独立连接。调用 `initialize()` 建表，然后由可信网关调用 `register_mandate()` 注册签名授权及服务商映射；`admit_and_reserve()` 在写事务内验证签名、授权状态、幂等键及预算，返回 `(payment, created)`。同内容重试返回已有记录；不同内容复用幂等键会拒绝。`revoke_mandate()` 与预留共用数据库写锁。调用方应为每次独立购买使用新的 intent ID、nonce 和幂等键。当前尚未实现服务交付、状态推进或链上结算；账本不是资金托管系统。
