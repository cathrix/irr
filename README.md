# IRR Config Generation

从 `AS-CATHRIX-CUST` 生成合资格客户 ASN 列表和 BIRD 前缀配置，并通过 ARIN 官方 IRR REST API 维护 `AS-CR`。

## 配置

编辑 `config/irr.json`：

- `full_set`：完整 AS-SET，默认 `AS-CATHRIX`，包含自有 ASN 和 `AS-CR`。
- `customer_set`：客户 AS-SET，默认 `AS-CATHRIX-CUST`，包含客户 ASN 和客户的 AS-SET。
- `target_set`：展平后的客户 AS-SET，默认 `AS-CR`。
- `irr_host` / `irr_port`：IRRd 查询服务器，默认 `rr.ntt.net:43`。
- `default_max_asns`：固定为 100。
- `exceptions`：SET 名称映射到允许的 ASN 数量上限。已配置 `AS-MOEDOVE: 500`；超过 500 仍会被整个排除。删除这项即可恢复默认 100。
- `arin`：创建目标对象时使用的 Org Handle、联系人和描述。默认 Org 为 `CN-578`，admin/tech POC 为 `CNOC24-ARIN`。

配置使用 JSON，Python 标准库即可读取，无需安装 Python 依赖。

## 三个 AS-SET 的分工

| AS-SET | 内容 | 用途 |
| --- | --- | --- |
| `AS-CATHRIX` | 自有 ASN（如 `AS62748`）和 `AS-CR` | 完整视图；`irr.conf` 包含其中的自有 ASN 和前缀 |
| `AS-CATHRIX-CUST` | 客户 ASN 和客户的 AS-SET | 客户集合的入口 |
| `AS-CR` | 所有客户 ASN 的展平结果 | 由 Action 维护，**不包含**自有 ASN |

`AS-CATHRIX` 中的 `AS-CR` 成员会被跳过，避免把 `AS-CR` 的内容重复计入自有 ASN；自有子 SET 会完整展开，因为它们属于我们而不是客户。

## 两层解析规则

1. 用 IRRd `!iAS-CATHRIX-CUST` 非递归查询客户集合的直接成员。
2. 顶层直接 ASN 纳入结果；对每个直接子 AS-SET 调用 `bgpq4 -t -j -L 1`，只取得该子 SET 的直接 ASN。
3. 子 SET 内的其他 AS-SET 不展开。每个子 SET 的直接 ASN 去重后计数，100 个允许，101 个排除，例外使用明确指定的上限。
4. 合格客户 ASN 合并、去重、按数字排序。一个 ASN 即使来自被拒绝的 SET，若同时属于其他合格 SET，仍会保留。
5. 用 IRRd `!iAS-CATHRIX` 读取自有成员，跳过 `AS-CR`，合并出完整 ASN 列表（客户 + 自有）。
6. 对完整 ASN 列表分批调用 `bgpq4 -4/-6 -j` 查询 IRR route/route6 前缀。保留精确前缀，不使用 `-A` 聚合或自动放宽匹配范围。

前缀数据来自 IRR 登记，不表示当前实际 BGP 宣告，也不额外进行 RPKI 验证。服务器使用其默认 IRR source 集合及优先级。

## GitHub Actions

在仓库 **Settings → Secrets and variables → Actions** 添加名为 `ARIN_API_KEY` 的 repository secret。该 Key 的 ARIN Online 账户必须有目标组织和对象的管理权限。

- 每天 UTC 00:00 生成文件，在默认分支上同步 ARIN，并把 `irr.conf` 写入 `prod` 分支。
- 手动执行 **IRR Config Generation**：行为与定时任务完全一致，同样同步 ARIN 并更新 `prod`；两者都仅允许在默认分支执行。
- push / pull request 执行测试和生成，不同步 ARIN，也不写 `prod`。
- 上传在生成 job 的最后一步，API Key 仅注入该步骤。
- 不生成 artifact；生成结果只写入 `prod` 分支。被拒 SET 和每个 SET 的计数显示在 workflow run 的 Summary 页面。

### prod 分支

`prod` 分支保存 `irr.conf` 和 `result.txt`，由 Action 通过 GitHub Contents API 写入，不需要手动创建分支，也不需要配置额外的 token（使用内置 `GITHUB_TOKEN`，job 已声明 `contents: write`）。

- 首次运行时 `prod` 不存在，Action 会自动创建分支并写入文件。
- 之后每次生成都会覆盖 `prod` 上的 `irr.conf` 和 `result.txt`，各提交一次。
- 生成前会从 `prod` 取回上一次的 `result.txt`，用于计算 `result.txt` 中括号里的增减量；首次运行时没有历史，显示 `+0`。
- 即使前缀列表没有变化也会提交，以便 `Generated at` 时间戳始终反映最近一次生成时间。因此 `prod` 每天会多一个提交，这是预期行为。
- 提交信息带 `[skip ci]`，不会触发新的 workflow。

消费方可以直接拉取 `prod` 分支的 `irr.conf`，例如：

```sh
curl -fsSL https://raw.githubusercontent.com/<owner>/<repo>/prod/irr.conf -o irr.conf
```

注意：`prod` 分支是生成物的唯一来源，**不要手动修改**，下一次运行会覆盖。需要调整内容请改 `config/irr.json` 后重新生成。

- 查询或生成失败时任务失败，阻止上传；全部 ASN 被排除时也阻止上传，避免意外清空目标。某一个地址族没有前缀时，允许输出空列表。
- 超过数量限制的 SET 属于策略排除，记录到报告但不使任务失败；其他合格 SET 继续处理。

## 本地运行

安装 Python 3.9+ 和 bgpq4（需要支持 `-L`、JSON 输出）：

```sh
# Ubuntu
sudo apt-get install bgpq4
# macOS
brew install bgpq4

python3 -m unittest discover -s tests -v
python3 scripts/irr.py generate
```

生成成功后，可用环境变量 `ARIN_API_KEY` 提供密钥并执行：

```sh
python3 scripts/irr.py upload
```

不要将密钥放进配置或提交到 Git。生成阶段无需 ARIN Key，只需能访问 IRRd TCP 43。

## 输出

`generated/` 中包含：

- `irr.conf`：包含 `ALL_ASNS`（客户 + 自有）、`ALL_PREFIXES_V4`、`ALL_PREFIXES_V6` 三个 define，顺序固定，空列表为 `[]`，BIRD 语句带分号。文件首行是 `# Generated at <UTC 时间>`，之后空一行。
- `result.txt`：本次生成的汇总，包含 ASN 数量、IPv4/IPv6 前缀数量（括号内是与上次相比的增减）、以及被排除的 AS-SET。内容同时显示在 workflow run 的 Summary 页面。

## ARIN API

实现依据 [ARIN IRR RESTful API User Guide](https://www.arin.net/resources/manage/irr/irr-restful/#as-set-objects)：

- `GET /rest/irr/as-set/AS-CR` 读取现有对象。
- 存在时 `PUT /rest/irr/as-set/AS-CR` 更新。
- 仅 GET 返回 404 时，通过 `POST /rest/irr/as-set?orgHandle=CN-578` 创建。
- 使用 `Authorization: ApiKey <key>`，`Accept` 和 `Content-Type` 均为 `application/rpsl`。Key 不放在 URL。

更新时保留现有描述、联系人等元数据，替换全部 `members`，移除 `mbrs-by-ref`，使成员明确等于生成的 ASN 列表。现有对象仍须满足 ARIN 的对象权限限制；公共查询看到空结果不代表有权限管理该对象。

尚未使用真实 API Key 执行远程写入；离线测试覆盖创建及更新请求。首次上线前，建议先手动执行一次 workflow，在 Summary 页面确认各 SET 的计数符合预期，再检查 `prod` 分支上的 `irr.conf`。
