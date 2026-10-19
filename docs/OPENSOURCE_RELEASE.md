---
AIGC:
  ContentProducer: '001191110102MAD55U9H0F10002'
  ContentPropagator: '001191110102MAD55U9H0F10002'
  Label: '1'
  ProduceID: '4d0a5f37-9f21-4c8e-b7a3-53f0d1c9a7bb'
  PropagateID: '4d0a5f37-9f21-4c8e-b7a3-53f0d1c9a7bb'
  ReservedCode1: 'a1e6c9d4-2b7f-4a53-9c81-6de2f4b70a19'
  ReservedCode2: 'a1e6c9d4-2b7f-4a53-9c81-6de2f4b70a19'
---

# 开源版发布：打包边界与发布流程

> 本文回答一个问题：**这个仓库里哪些东西要进开源包、哪些绝不能进、哪些要先脱敏**。
> 打包命令：`bash scripts/package_opensource.sh`（产出 `dist/Sharp-opensource-YYYYMMDD.zip`，
> 内置泄漏自检 + **成品包自检**，发现问题直接**失败**而不是产出可疑的包）。

## 1. 三条边界（按理由分类，不按目录）

| 边界 | 判据 | 典型条目 |
|---|---|---|
| **绝不能进包** | 泄露即造成实质损害：私钥、真实凭据、真实客户/靶场数据 | `datas/sharp/secrets.env`（真实模型 token）、`datas/sharp/license_signing_key.b64`（**授权签名私钥**：泄露=任何人可自造授权）、`datas/sharp/*.db*`（含真实目标与凭证）、`reports/`（真实报告） |
| **不该进包** | 体积、可复现性、再分发许可不清 | `container/vendor/`（≈283MB 第三方二进制，改由 `fetch_vendor.sh` 按固定版本拉取）、`container/device-tools/panda-dex-dumper`（上游再分发条款未声明）、`runtime/.venv/`、`dist/`、各类缓存 |
| **要脱敏后进包** | 内容本身能开源，但夹带了环境/第三方信息 | `dispatch.yaml` 的默认端点与模型、文档里的真实靶标域名、`packaging/`（商业打包工具链） |

## 2. 逐项清单

### 2.1 排除：安全类（**硬性，打包脚本会校验**）

| 路径 | 理由 |
|---|---|
| `datas/sharp/secrets.env` | 真实模型 API token |
| `datas/sharp/license_signing_key.b64` | 授权签名私钥。**建议进一步移出项目目录**（放 `~/.config/sharp/` 或密码管理器）——只靠 `.gitignore` 兜着太薄，一次误打包就全丢 |
| `datas/sharp/*.db` `*.db-wal` `*.db-shm` | 运行库：真实目标、跑分令牌、凭证、结论 |
| `reports/` | 真实渗透报告（客户 / 靶场数据） |
| `.secrets`、`license.key`、`*.key` | 其他密钥material |
| `datas/.DS_Store`、`**/.DS_Store` | 无意义且可能带路径信息 |
| `tools/issue_license.py` | 授权签发工具（商业侧），配合私钥使用 |

### 2.2 排除：体积与可复现性

| 路径 | 体积 | 替代方式 |
|---|---|---|
| `container/vendor/` | ≈283 MB | `cd container && ./fetch_vendor.sh`（版本已在脚本里固定；`PRINT_ONLY=1` 可只看 URL 不下载） |
| `container/device-tools/panda-dex-dumper` | 1.7 MB | 第三方 ELF 二进制，再分发条款未声明；需要 Android DEX 分析的用户自取 |
| `runtime/.venv/` | 42 MB | `uv sync --project runtime --locked` |
| `runtime/.coverage`、`runtime/.pytest_cache/`、`__pycache__/`、`*.pyc` | — | 重新生成 |
| `dist/`、`build/`、`.buildenv/` | — | 构建产物 |

### 2.3 脱敏后进包

| 位置 | 问题 | 处理 |
|---|---|---|
| `dispatch.yaml` → `ANTHROPIC_BASE_URL` | 默认值指向某个第三方中转网关（域名略）。开源项目把**别人的网关**设为默认 API 端点是明确的红旗（流量经第三方） | 默认改为官方 `https://api.anthropic.com`；第三方网关只在文档里作为通用示例 |
| `dispatch.yaml` → `ANTHROPIC_MODEL` | 默认 `grok-4.5`（只有走该中转才存在的模型名） | 默认改为 `sonnet` 别名（版本无关，可被 `SHARP_MODEL` 覆盖）；模型名与端点都应显式配置 |
| `docs/ARCHITECTURE.md`、`docs/CHANGELOG.md` | 出现过往真实靶标域名 | 替换为 `app.example.com` 之类中性示例，并保留"示例已脱敏"的说明 |
| `packaging/` + `.github/workflows/build-windows-exe.yml` | 商业桌面版的打包工具链（PyInstaller spec、授权 license 组装） | **不进开源包**。这是开放核心边界（见 §3）。`runtime/src/sharp/{licensing,frozen_main}.py` 保留在源码里（AGPL 要求：分发了二进制就要给完整对应源码），只是没有 `packaging/` 就构建不出那个二进制 |
| `docs/FRONTEND_AUDIT_REPORT.md` | 自曝式前端审计（存储型 XSS / token 明文存储等） | **先复核再决定**。已复核：`renderMd()` 已加转义、token 已迁到 `sessionStorage` 并清理 `localStorage`（两条高危已修）→ 已在报告顶部加「复核状态」后随包发布，作为"我们自己审自己"的证据 |

### 2.4 必须新增（否则不算"一个开源版本"）

| 文件 | 作用 |
|---|---|
| `SECURITY.md` | 威胁模型 + 残留风险 + 运维加固清单 + 报告渠道。**这是这类工具最该有的一份**：靶标内容对 agent 是敌手可控输入 |
| `CONTRIBUTING.md` | 开发/验证流程（三道检查）、房规、以及本项目反复踩到的"接线型缺陷"守卫惯例 |
| `THIRD_PARTY_NOTICES.md` | 随包再分发的第三方组件（前端 JS、字体）许可 + 构建期下載的二进制清单（不随包分发，给出固定版本） |
| `README.en.md` | 英文 README（国际采用的第一步） |
| `.github/ISSUE_TEMPLATE/`、`pull_request_template.md` | 让 issue/PR 带上"是否有错误写入共享状态""守卫是否变异验证过"这类关键信息 |
| 本文 | 打包边界的唯一口径，避免每次发布靠记忆 |

## 3. 开放核心边界（写清楚，否则 issue 区会被刷屏）

- **开源**：Sharp 核心（server / dispatcher / 协议 / worker 镜像 / 前端 / 文档），AGPL-3.0。
- **商业**：`packaging/` 构建的桌面版二进制 + `tools/issue_license.py` 签发的离线授权（`license.key`）。
  授权校验**离线完成、不联网回连**（`runtime/src/sharp/licensing.py`）。
- 源码运行（`sharp serve` / `sharp dispatch`）**不需要** license，只有 frozen 打包版入口
  （`frozen_main.py` 的 `require_license()`）才校验。
- README 必须说明这一点，否则"为什么仓库里有个 license.key 流程"会反复被问。

## 4. 发布流程

```bash
# 1) 出包（含泄漏自检）
bash scripts/package_opensource.sh

# 2) 自检在包里再跑一次（脚本已内建，这里是独立复核）
unzip -l dist/Sharp-opensource-*.zip | grep -iE "secret|\.key|\.db|license_signing|reports/"   # 应为空

# 3) 从**解压后的副本**跑测试，证明"包是自洽的"
cd /tmp && unzip -q ~/.../dist/Sharp-opensource-*.zip && cd Sharp-opensource-*/
uv run --project runtime --with pytest --with httpx pytest -q

# 4) 首次推送前的最后一道人工确认
git init && git add -A && git status --porcelain | grep -iE "secrets|\.key|\.db|reports/"        # 必须为空
```

**首发建议顺序**：`git init` + 首个 commit（先跑 §4 的第 4 步）→ 推 GitHub → 打开 GitHub 私密漏洞报告
→ 建 issue 模板生效 → 再发第一条公告。**不要**在没有 §4 第 4 步确认的情况下 `git push`。

## 5. 已知的后续项（本版未做）

1. **英文文档（进行中）**：`README.en.md` / `docs/ARCHITECTURE.en.md` / `docs/USAGE.en.md` 已出英文版
   （ARCHITECTURE 与 USAGE 是**摘要**，中文为唯一权威，见 CONTRIBUTING「Documentation language」）。
   仍缺：`CHANGELOG`（可缓）与**提示词模板**（国际贡献者真正会碰的部分，建议下一批翻）。
2. **worker 镜像发布到 registry（如 ghcr.io）**：把"几分钟编译"变成 `docker pull`，是采用率上最大的单点改善。
3. ~~提示词补"不可信内容"口径~~ —— **已补（2026-09-16）**：见 ARCHITECTURE §10.5 与 `test_untrusted_content_guard.py`。
4. **打包元数据**：`runtime/pyproject.toml` 缺 `license` 字段、项目 URL 与 classifiers（需先定 GitHub 地址）。
5. **许可策略决定**：AGPL-3.0 保持（防 SaaS 白嫖）还是源码改 Apache-2.0 + 商业授权（换采用率）。
   这决定 README / CONTRIBUTING 的措辞，需先定。
