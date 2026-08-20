# pr-reviewer

面向 GitHub PR 的 LLM 代码审查 Agent，模型使用 DeepSeek V4 Pro，支持 Python / TypeScript / Java / Go。

## 快速开始

```bash
python3 -m venv .venv && .venv/bin/pip install -e .
export DEEPSEEK_API_KEY=sk-xxx

# 先跑本地分支，不需要 GitHub 权限
.venv/bin/python -m reviewer.cli review-diff --base main --repo-path /path/to/repo

# 审查一个 PR，输出到 stdout
.venv/bin/python -m reviewer.cli review-pr 42 --repo owner/name

# 确认结果后再发帖
.venv/bin/python -m reviewer.cli review-pr 42 --repo owner/name --post
```

`review-pr` 通过 `gh` CLI 访问 GitHub，沿用你已有的登录态，不需要额外配置 token。

离线自检（不联网、不消耗 token）：

```bash
./run_tests.sh
```

## 流水线

```
GitHub PR / git diff
        │
        ▼  parse + 行号栏渲染        diffing/parser.py
   按文件切分（必要时 chunk）        diffing/chunker.py
        │
        ▼  policy 路由选规则包        policy.py
   审查（可选 N 路 ensemble）        pipeline/review.py
        │
        ▼  scope 校验（丢弃越界引用） diffing/scope.py
   多数决聚合（N>1 时）              pipeline/review.py
        │
        ▼
   Stage 1 资格闸门（启发式 → LLM）  pipeline/qualify.py
        │  discard / pass / validate
        ▼
   Stage 2 深度验证（agentic + 工具） pipeline/validate.py
        │  TP / FP / FP_OOS / TP_SI / IND
        ▼
   Stage 3 应用裁决                  pipeline/apply.py
        │
        ▼  重写受影响的文件摘要 + 总摘要
   Markdown 渲染 → 发帖（去重 + 剪枝） pipeline/render.py, sources/github.py
```

## 关键实现决策

**结构化输出走 strict function call，不走 `json_object`。** DeepSeek 没有 `response_format: json_schema`，但 `/beta` 端点对函数 schema 支持 `strict: true`。所以审查结果的 Pydantic 模型被发布成一个工具，用 `tool_choice` 强制调用，换回语法级保证。`provider/strict_schema.py` 负责把 Pydantic schema 改写成 strict 要求的形态（每层 `additionalProperties: false`、所有属性进 `required`，可选字段保留 `anyOf [..., null]`）。

**模板不用 `str.format`。** 这些 prompt 里嵌了大量 JSON 和代码示例，`str.format` 会直接崩，或者逼着每个例子把花括号写两遍。`prompt.render()` 只替换传入的键，其余花括号原样保留。

**空 content 归类为 DEGENERATE 而非 FATAL。** DeepSeek 官方文档明写 JSON 输出可能偶尔返回空内容，当成致命错误会白白丢掉整轮审查。

**行号栏是 scope 校验的唯一依据。** 每行渲染成 `旧行号 | 新行号 标记内容`，hunk 头的行号列留空。模型引用的行号必须能在栏里落到实处，且至少有一条引用落在本次新增行上，否则整条评论丢弃。scope 校验跑两次：单个 reviewer 输出后一次，聚合后再一次（聚合会引入新的行号引用）。

**验证器无工具调用的裁决一律降级为 INDETERMINATE。** 没读过代码就下的结论是意见不是发现。

## 配置

| 开关 | 默认 | 说明 |
|---|---|---|
| `--ensemble N` | 1 | N>1 启用多路审查 + 多数决聚合。参考实现用 3；这里默认 1，等 benchmark 能证明 3 倍成本买到了准确率再打开 |
| `--no-validate` | 关 | 跳过资格闸门和深度验证。更快，但误报率会回到未过滤水平 |
| `--max-files` | 100 | 单次审查的文件上限 |
| `--max-reviews` | 5 | PR 上保留的历史报告数，超出剪掉最旧的 |

规则包路由见 `resources/policies/default.json`，按文件 glob 决定拼哪些规则文件。

## 成本

按官方非峰时价格（输入 cache miss $0.66/M，输出 $1.98/M）粗估，单文件走完完整链路约 **$0.14**，20 文件的 PR 约 **$2.8**。峰时（UTC 01:00–04:00 / 06:00–10:00）翻倍，定时任务排到非峰时可省一半。

## 部署：每个 PR 自动审查

`.github/workflows/pr-review.yml` 是可直接用的模板。装到**被审查的仓库**里（不是本仓库），然后配两个 secret。

### 三步

1. 把 `.github/workflows/pr-review.yml` 复制进目标仓库
2. 目标仓库 Settings → Secrets → Actions 添加 `DEEPSEEK_API_KEY`
3. 本仓库是私有的话，再加一个 `REVIEWER_REPO_TOKEN`（有本仓库读权限的 PAT）；本仓库公开则可删掉那一行

### 几个必须知道的点

**检出的是 `head.sha`，不是默认的 merge commit。** Actions 默认给你一个 PR 与 base 的临时合并结果，那不是作者写的代码。审查器每个读文件的阶段都必须看到 diff 所对应的树，所以工作流显式指定 `ref: github.event.pull_request.head.sha`，并用 `--repo-path` 指过去。

**`synchronize` 是日常真正起作用的事件。** 作者一推新 commit，上一次的审查就过期了。加上 `concurrency` + `cancel-in-progress`，推送期间的旧运行会被取消而不是和新运行抢着发评论。

**来自 fork 的 PR 拿不到 secret。** 这是 GitHub 的安全设计，不是配置问题：`pull_request` 事件在 fork 上下文里没有仓库 secret，工作流会因为缺 key 失败。想覆盖 fork 就得用 `pull_request_target`，那会让不受信任的代码在有 secret 的上下文里跑——**不要这么做**，除非你完全清楚风险。内部仓库不受影响。

**成本随 PR 大小线性增长。** `--max-files 60` 是护栏；一个改了 300 个文件的 PR 会审前 60 个并在报告里注明跳过数。按当前语料测算约 $0.02/文件。

### 换个姿势：定时扫描

不想每次推送都触发，或者想覆盖多个仓库，用 `scan` 子命令配 cron——见上一节。两者可以共存：Actions 管热路径，cron 兜底捡漏。

## 服务器部署：每次推送都审，且只审一次

三个进程，各司其职：

```
GitHub ──webhook──> serve ──入队──> 队列(磁盘) ──> worker ──> 发帖
                      │                            ↑
                      └─ 立刻返回 202               │
         cron ──> scan ────────对账、补漏───────────┘
```

```bash
export DEEPSEEK_API_KEY=sk-xxx
export GITHUB_WEBHOOK_SECRET=$(openssl rand -hex 32)

pr-reviewer serve  --queue /var/lib/pr-reviewer/queue --host 0.0.0.0 --port 8787
pr-reviewer worker --queue /var/lib/pr-reviewer/queue --v2 --settings settings.json
pr-reviewer queue  --queue /var/lib/pr-reviewer/queue      # 看积压
```

GitHub 仓库 Settings → Webhooks 添加 `https://你的域名/webhook`，content type 选
`application/json`，Secret 填上面那个，事件只勾 **Pull requests**。

### 为什么是三个进程

**webhook 必须在秒级返回**——GitHub 10 秒超时，而一次审查要几分钟。`serve` 只做验签、
入队、返回 202。

**队列必须落盘**。worker 崩了、机器重启了，任务不能蒸发。用的是目录 + 原子 rename：
`os.replace` 保证两个 worker 不会认领同一个任务，任务文件是可读 JSON，运维 `ls` 就能
看积压，也没有 schema 迁移问题。

**cron 扫描是对账循环，不是主力**。webhook 会丢投（服务重启、网络抖动），扫描把丢的
捡回来。webhook 正常时它几乎什么都不做——所有任务都被去重挡掉了。15~30 分钟一次足够。

完整的 systemd 单元、nginx 配置和安装步骤在 [`deploy/`](deploy/README.md)。

### worker 读的是哪份代码

`--settings` 里每个仓库的 `checkout` 指向一份本地 clone。worker 处理每个任务时，在**那个任务自己的 head commit** 上切一个 detached worktree，跑完释放。

这不是可选的讲究。worker 消费的连续两个任务往往是不同 PR、甚至不同仓库——**对上一个任务正确的检出，对下一个就是错的**。而验证器读错代码不会报错，它会对毫不相干的代码给出自信的裁决。

没配 `checkout` 的仓库，读文件的阶段会被关掉并打警告，而不是退回读 worker 的当前目录。

### 保证来自哪里

**幂等键是 `(仓库, PR 号, head commit)`**。每份报告把它回答的 commit 写进标记，判据是
「这个 commit 有没有报告」。所以 webhook 重投、worker 重试、扫描重复发现——三条路径都
收敛到每个修订一次审查。

没有这一条，你加的每个重试机制都会变成重复发帖的来源。

### 三个行为值得知道

**连推三次只审最后一次。** 新修订入队时会丢掉同一 PR 更早的 *待处理* 任务。已被认领的
不动——那正在跑，从外部取消会让 worker 给一个不存在的任务写结果。

**草稿 PR 不审**，直到标记为 ready。

**余额耗尽会停下并保住任务**，充值后重启 worker 原地继续，不需要重新触发。

## 定时扫描

`scan` 子命令按 `settings.json` 遍历仓库，决定哪些开放 PR 需要审查。示例见 `settings.example.json`。

每个仓库的 **`checkout` 是可选但重要的**：它指向该仓库的本地 clone，扫描时会为每个 PR 在其 head commit 上开一个 detached worktree，让深度验证器、agentic 审查器和 semgrep 读到**这个 PR 的代码**。

不配 `checkout` 时，这些读文件的阶段会被**关闭**（降级为纯 diff 审查），而不是让它们去读 cron 进程的当前目录——验证器读错仓库不会报错，它会对毫不相干的代码给出自信的裁决。

```bash
pr-reviewer scan --settings settings.json --dry-run   # 只打印决策，不发帖
pr-reviewer scan --settings settings.json
```

## Benchmark：让 prompt 改动可测量

这是唯一能回答"改完到底变好还是变差"的东西。

```bash
# 1. 抓 PR 进语料库（自动记录 pin commit）
.venv/bin/python -m reviewer.cli benchmark-capture --repo owner/name 101 102 103

# 2. 手工标注 corpus.json，然后把 labelled 改成 true
# 3. 打分
.venv/bin/python -m reviewer.cli benchmark-run --corpus corpus.json --run-dir runs/baseline

# 4. 换模型或改 matcher 后，先验 matcher 自己有没有退化
.venv/bin/python -m reviewer.cli calibrate-matcher
```

### 标注规范

一条 `ground_truth` = **一个根因、一个代码位置、一个 commit 能修完**。

- 同一个可空值的两处解引用 → 1 条
- 同一个 bug 模式在同方法内重复（`get(0)` 在 `for(j)` 循环里）→ 1 条
- 同类 bug 出现在不同方法 → 2 条
- 单个 PR 超过 10~12 条，说明在标症状不是标缺陷

字段：

| 字段 | 说明 |
|---|---|
| `description` | 「是什么 bug + 在哪 + 为什么要紧」。必须点名具体构造（变量/方法/表达式）和失败模式——matcher 就是靠这个配对的。"possible NPE" 是分类不是描述 |
| `min_severity` | `error` 明确改变运行时行为；`warning` 真缺陷但影响有界或非默认路径；`info` 罕用 |
| `value` | 与 severity 正交。`p1` 生产关键；`p2` 真缺陷但触发窄；`p3` 装饰性 / 仅测试 / 无调用方的潜在坑 |
| `requires_exploration` | 是否需要翻 diff 以外的文件才能确认。**打分按这个分桶** |
| `note` | 审计痕迹。修订标注时不要静默改——写清为什么 |

**刻意留几个零 bug 的干净 PR**，用来量"会不会无中生有"。考虑过但决定不标的，把理由写进 `note`——否则下一个人会把同一场争论再吵一遍。

标注锚定在 `pin_commit`。PR 后续新增 commit 不会自动更新标注。

### 输出

`report.md` 含四组数字：

- 总体 precision / recall / F1
- **按上报 severity 的 precision**
- **按 `requires_exploration` 的 recall**（diff-only vs cross-file）
- **按 value 分级的 recall**

第三组最有用：如果 diff-only recall 高而 cross-file recall 低，问题在深度验证器的工具使用，不在基础审查器。

### 关于基线数字

老项目**没有静态基线**，而且是刻意的——`assert precision >= X` 只能测"今天的模型有没有比上次挑的 X 差"，要么恒真无用，要么红了也说不出原因。

所以：**不要追某个 F1 数字，追自己上一次跑分的 delta。** 先跑出第一个 scorecard，那就是基线。

只作 sanity check 的量级参考（不是目标）：error 级 precision 0.55~0.75、recall 0.35~0.55、F1 0.45~0.60。低于这个区间说明有东西坏了，F1 超过 0.7 属于优秀。

另外：一次完整 corpus 跑分是**小时级**（老项目 ~30 个 MR 跑 3h45m），别按分钟级预期安排。

**改 prompt 的流程**：先跑基线 → 改一处 → 重跑 → 比较 → 再决定要不要保留。测试用例在修复合并**之后**才加进语料，否则会过拟合到那个具体例子。

## 目录

```
resources/          prompt 库
  role/             角色定型（每个 1–3 行）
  tasks/            任务模板：审查、多数决、资格闸门、深度验证、摘要、benchmark judge
  rules/            general.md + language_rules/{python,typescript,java,go}.md
  policies/         文件 glob → 规则文件的路由表
src/reviewer/
  provider/         DeepSeek 适配器、strict schema、错误分类与退避、JSON 修复
  diffing/          diff 解析与行号栏、scope 校验、chunk、token 计数
  sources/          github.py（gh CLI）、local_git.py
  pipeline/         review / qualify / validate / apply / render / orchestrator
  tools/            只读文件系统工具（供 agentic 验证器使用）
  benchmark/        语料、matcher、打分、带检查点的 runner
tests/              三个离线自检 + matcher 校准用例
```

## 尚未实现

按参考架构还差这些，按需要程度排序：

- **Sleep 对抗式验证子系统**（scout → prosecutor ↔ defense → bias probe）。只有在某一类缺陷的误报率无法接受时才值得上。
- **semgrep 静态分析合流**。findings 需要先转成评论 schema，再走同一条验证流水线。
- **V2 单 agent 审查器**（带工具的单次调用，替代 ensemble）。参考实现称其误报率往往更低且更便宜，值得等 benchmark 建好后 A/B。
- **定时扫描与 `!review` / `!do-not-review` 触发**。`sources/github.py` 已具备 fingerprint 去重、按时间判断是否重审、剪枝旧报告的能力，接一个 cron 即可。
- **行级评论**。当前发的是单条汇总评论；改成 inline review comment 需要 commit SHA + diff position 映射。
