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

`LedgerDB` 使用文件数据库、WAL 和独立连接。调用 `initialize()` 建表，然后由可信网关调用 `register_mandate()` 注册签名授权及服务商映射；`admit_and_reserve()` 在写事务内验证签名、授权状态、幂等键及预算，返回 `(payment, created)`。同内容重试返回已有记录；不同内容复用幂等键会拒绝。`revoke_mandate()` 与预留共用数据库写锁。调用方应为每次独立购买使用新的 intent ID、nonce 和幂等键。目前服务交付及账本状态推进尚未实现，账本还未接入链上结算；账本不是资金托管系统。

## 第 3 步：本地合约与 Gas 实验

```powershell
.venv/Scripts/python.exe -m pytest -v tests/test_settlement.py
.venv/Scripts/python.exe -m experiments.bench_gas
```

合约使用模拟余额：部署者负责充值，Owner 创建自己的会话并指定结算网关，网关按采购明细逐项结算。批次 ID 在同一会话内不可重复。该合约不直接验证第一步的签名，网关必须先完成链下授权及账本检查；目前两者尚未接成完整流程。模拟余额不是实际代币或资金托管。

实验在独立的新 Py-EVM 链上分别执行同一份 10 笔清单，N 为每个交易中的原始采购笔数，不按服务商预聚合；使用 Solidity 0.8.20、优化器 200 runs。总 Gas 只包含成功的结算交易，不包含部署、充值、创建会话或重复批次失败交易。运行结果与完整回执保存在 `outputs/gas_benchmark.json`；每次运行会覆盖该文件，内存链在进程结束后消失。
