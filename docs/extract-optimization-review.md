# 解压链路优化建议

评审日期：2026-09-26
评审范围：`core/pipeline.py`、`core/probe.py`、`core/sevenzip.py`、`core/archive_detect.py`、`core/password_finder.py`、`core/output_plan.py`
基线：`python -m pytest -q` → 170 passed

---

## 结论速览

| 编号 | 环节 | 问题 | 级别 | 依据 | 状态 |
|------|------|------|------|------|------|
| P0-1 | 探测 probe | 加密包的 zip bomb 检查完全失效 | 严重 | 代码推导 | 已修复 |
| P0-2 | 密码候选 | 来源派生密码被 `[:20]` 截断挤掉 | 严重 | 代码推导 | 已修复 |
| P0-3 | 扫描入队 | `partNN` 命名分卷重复入队 | 高 | 代码推导 | 已修复 |
| P1-1 | 扫描入队 | 全目录嗅探使扫描耗时 +184% | 中 | 本机实测 | 已修复 |
| P1-2 | 落地产物 | 重跑不复用已完成任务 | 中 | 代码推导 | 已修复 |
| P1-3 | 调度 | 任务级串行，无并发 | 中 | 代码推导 | **实测后决定不做**（见下） |
| P1-4 | 落地产物 | 复制回源双倍 IO（可用 move 消除） | 低 | 代码推导 | 已修复 |
| P2-1 | 体验 | `extract_timeout` 无界面/CLI 入口 | 低 | 代码推导 | 已修复 |
| P2-2~4 | 体验 | 无总进度 / 重试成本高 / `list_timeout` 无入口 | 低 | 代码推导 | 未实施 |

测试基线：**170 → 196 passed**（累计新增 26 个测试）。

---

## 实施进展

### 第一批（2026-09-26，P0 三项 + P1-1）

测试基线：170 passed → **180 passed**（新增 10 个回归测试，全部先验证过"在旧实现上跑红"）。

| 编号 | 状态 | 改动位置 | 验证 |
|------|------|----------|------|
| P0-1 | 已修复 | `core/probe.py` 新增 `probe_with_password`；`core/pipeline.py` `_process` 中密码确定后复探校验，超限则删产物置 FAILED | `test_encrypted_bomb_rejected`（改前 `done=1` 跑红） |
| P0-2 | 已修复 | `core/password_finder.py` `build_candidates`：`derived_from_source` 提到 vault 之前 | `test_source_derived_survives_truncation`、`test_source_password_found_when_vault_saturated`（改前端到端 `needs_password=1` 跑红） |
| P0-3 | 已修复 | `core/archive_detect.py` 新增 `volume_info`；`core/pipeline.py` 新增 `_collapse_volumes`，run 入队前收敛为只解首卷 | `test_part_volumes_collapse_to_single_task`、`test_missing_first_volume_warned_and_skipped`（回退法验证精准变红） |
| P1-1 | 已修复 | `core/archive_detect.py` 新增 `_NOT_ARCHIVE_EXTS`，`looks_like_archive` 开头短路 | `test_known_media_ext_skips_sniffing`（改前触发文件读取跑红）+ `test_unknown_ext_still_sniffs`（防退化配重） |

**P1-1 实测收益**：同样的 3 万小文件目录，扫描耗时 **7782.4 ms → 2848.2 ms**，已回到纯 `rglob` 基线（2744.9 ms），即嗅探开销基本归零。且识别结果不变（伪装 `.dat` 仍能识别）。

### 第二批（2026-09-26，P1-2 / P1-4 / P2-1）

测试基线：180 passed → **196 passed**（新增 16 个测试）。

| 编号 | 状态 | 改动位置 | 验证 |
|------|------|----------|------|
| P1-2 | 已修复 | `core/store.py` 新增 `find_done`（取**最近一次**结论）；`Pipeline.skip_done` 默认开；CLI `--force`；`AppConfig.skip_done` + 设置窗口开关 | `test_rerun_skips_already_done_archive`（改前 `assert 0 == 1`、症状里直接出现 `pack (2)`）；`test_rerun_reprocesses_after_output_removed` 用**回退法**验证（摘掉产物存在性检查后精准变红） |
| P1-4 | 已修复 | `core/output_plan.py` 新增 `same_volume`；`Pipeline._place_tree` 同卷 `os.rename`、跨卷回落 `copytree` | `test_workdir_mode_copies_back_to_source_dir`、`test_delivered_output_dir_points_at_real_path` 改前跑红；`test_cross_volume_delivery_keeps_workdir_copy` 锁住"跨卷行为不变" |
| P2-1 | 已修复 | `AppConfig.extract_timeout`（`_CLAMP` 30~86400s）+ `as_overrides` + 设置窗口控件 + CLI `--timeout` | `test_extract_timeout_reaches_config`、`test_config_set_extract_timeout_clamped`；`test_make_cfg_uses_preferences` 用**回退法**验证（从 `as_overrides` 摘掉后精准变红） |

**P1-4 实测收益**（200 MB 产物，同卷）：

```
copytree（复制，工作目录留底）  0.605 s
os.rename（同卷搬移）          0.001 s   -> 约 780x，且省下 200 MB 额外磁盘
```

同卷 rename 是元数据操作，耗时与字节数无关。跨卷仍走复制并保留工作目录副本作为安全网——**优化只在同卷生效，跨卷行为不变**。

### 顺带修掉的一个潜伏 bug（P1-4 暴露）

`Pipeline._fill_report` 筛 `output_dirs` 的条件写反了：

```python
parent_ids = {t.parent_id for t in run_tasks if t.parent_id is not None}
if t.id not in parent_ids:      # 旧：按"自己没有子任务"筛 -> 报的是被搬空的内层目录
    report.output_dirs.append(t.extracted_dir)
```

本意是"只报最外层落点"，实际报的却是**最内层**目录——而内层产物早被 `_deliver_hierarchy` 归并到外层、内层 `out_dir` 随之被收掉。于是 `output_dirs` 指向一个不存在的路径。

之所以长期没暴露：UI 的 `_open_results` 会静默跳过不存在的目录，CLI 也只是打印一行死路径，都没报错。修法是改判 `t.parent_id is None`。这条在改动前由 `test_disguised_inner_zip_expanded` 等三条测试跑红捕获。

### P1-3 并发：实测结论（本轮**未实施**）

在 16 核机器上，对真实 `7z x` 负载做串行 vs 4 并发的对照（E 盘，机械硬盘）：

| 场景 | 串行 | 并发 x4 | 加速 |
|------|------|---------|------|
| 60 个小包（各约 2 MB，存储型） | 0.889 s | 0.282 s | **3.16x** |
| 6 个大包（各约 60 MB，存储型） | 0.458 s | 0.243 s | **1.89x** |
| 8 个 deflate 包（解压后约 800 MB） | 1.379 s | 0.377 s | **3.66x** |

加速是真实的，但**归因很重要**：单独测 60 次 `7z` 空转启动（不做任何解压）耗时 **0.534 s**，占小包串行总耗时（0.889 s）的 **60%**（单次启动 8.9 ms）。也就是说，小包场景的收益主要来自**掩盖 7z 进程启动开销**，而不是磁盘并行。

而物理上限由磁盘决定：E 盘实测顺序写入（已 `fsync`）仅 **104.8 MB/s**。解压一个 10 GB 的包，写盘下界就是约 95 秒，与并发度无关；4 路并发在机械盘上还会因读/写磁头争抢而互相拖慢。

**结论：收益集中在"小包多、总量小"的场景，在绝对时间上往往只有零点几秒；而真正耗时的"大包"场景受磁盘带宽限制，并发帮不上忙、在 HDD 上甚至可能变慢。**

因此 P1-3 暂不实施。若后续确要上，建议按此顺序降低风险：

1. 先做**只并行探测**（probe 阶段只读、彼此独立），把 2 次 7z 启动中的 1 次移出关键路径——不需要动 sqlite 连接模型；
2. 并发默认关，或取 `min(4, cpu//4)` 且在小文件数超过阈值时才启用；
3. 真要并行解压，再改 sqlite（每线程独立连接或 `check_same_thread=False` + 互斥锁）并给 `_plans` / `report` 加锁。

### 仍未实施

P2-2 总进度、P2-3 重试成本、P2-4（`list_timeout` 界面入口未暴露，`extract_timeout` 本轮已补）。

### 实施中的一个取舍（P0-1）
复探拿不到目录时（`probe_with_password` 返回 None）**放行而非拒绝**。理由是把它当成 bomb 会误杀"能解但探不到"的正常包——安全与可用性之间，这里选了不误伤。

---

## 零、先说三个被实测校准的假设

评审过程中我提出的三个"性能直觉"有两个被本机实测推翻，记录下来避免后续误优化：

1. **"每个候选密码跑一次完整解压，代价高昂"——不成立。**
   实测（30 MB 不可压缩数据，AES-256 zip + 头部加密 7z）：
   - 错误密码 `7z x` → 0.012s（7z 在首个文件即失败，不会读完数据）
   - 错误密码 `7z x`（7z 格式）→ 0.017s
   - 正确密码 `7z x` → 0.058s
   20 个候选密码总开销约 0.24s，**不是瓶颈**，无需引入 `l`/`t` 预验证。

2. **"用 `7z l -p密码` 预验证可以加速"——只对部分格式有效。**
   实测：zipcrypto（AES-256 zip）用错误密码执行 `7z l` 返回 **exit=0**（中央目录未加密，检不出）；7z 头部加密返回 exit=2（能检出）。所以该优化只能选择性用于 7z/rar，收益有限。

3. **"7z 分卷会被拆成多个任务"——不成立。**
   实测 `7z a -v1m` 产生的 `vol.7z.001 ~ vol.7z.031`：`.001` 扩展名命中入队，`.002~.031` 扩展名不命中、magic 也不命中，全部正确跳过。**7z 风格分卷处理是正确的。**（但 `partNN` 风格仍有问题，见 P0-3。）

---

## 一、P0 正确性缺陷

### P0-1 加密包的 zip bomb 检查完全失效

**位置**：`core/probe.py:88-94`、`core/pipeline.py:163`

**现象**：`probe()` 用空密码执行 `7z l -slt`。若压缩包头部加密（`-mhe=on` 的 7z、加密文件名的 rar），`l` 直接报密码错误，于是返回：

```python
info = ArchiveInfo(path=archive, needs_password_for_listing=True)
```

此时 `entries` 为空、`total_uncompressed = 0`。紧接着 `check_bomb(info, cfg)`：

```python
if info.total_uncompressed > cfg.max_total_uncompressed:   # 0 > 50GiB → False
    ...
if (info.archive_size > 0 and info.total_uncompressed > cfg.ratio_floor_bytes ...):  # 0 > 1GiB → False
    ...
return None   # 恒通过
```

**影响**：唯一的安全防线（50 GiB 上限 + 1000:1 压缩比）对**所有头部加密的包**都不生效。加密型 zip bomb（攻击者可轻易构造）会直接写爆磁盘。

**改法（推荐 a）**：

a) **密码确定后复探一次**。`_attempt_extract` 成功返回密码后，在 `_process` 里用该密码再调一次 `probe`（`list_raw(archive, password=pwd)`），拿到真实 `total_uncompressed` 再做一次 `check_bomb`；超限则删掉已解压产物并把任务置为 FAILED（消息写明是 bomb 拦截）。成本：一次 `l` 调用（毫秒级），只在加密包上多付。

b) **解压中兜底**。在 `sevenzip.extract` 的 reader 线程里周期性统计 `out_dir` 实际落盘字节数，超过阈值即 kill 进程。这条能覆盖"密码正确但 bomb 在 unzip 后才显形"的极端情况，但实现更重。

**验证方式**：构造一个头部加密、解压后远超上限的包（`7z a -mhe=on`，内容为大量零字节），断言 `run()` 结果为 FAILED 且消息含 bomb 关键字；并在改前跑红——当前实现下该包会成功解压（证明缺陷真实存在）。

---

### P0-2 密码候选排序会挤掉高价值候选

**位置**：`core/password_finder.py:65-84`、`core/pipeline.py:170-172`

**现象**：

```python
candidates = build_candidates(archive.name, task.source, vault_candidates)[: self.cfg.max_password_attempts]
```

`build_candidates` 的合并顺序是：

```
文件名内提示 → vault_candidates(最多 20 条) → derived_from_source(来源域名) → BUILTIN_DICT
```

`vault.candidates_for(source, limit=20)` 本身就最多返回 20 条。当库里该来源/无来源分组累计命中满 20 条时，`build_candidates` 结果的前 20 项**全部是库内密码**，"来源派生"（`source` 与 `www.source`）和内置字典永远排在第 21 位之后，被切片丢弃。

**影响**：`derived_from_source` 是命中率最高的一类候选（大量站点直接拿域名当密码），却被最小概率的库尾项挤出。用户表现为"明明密码就是域名，工具却报需要密码"。

**改法**：调整优先级为

```
文件名内提示 → 来源派生 → 库命中(按 hit_count) → 内置字典
```

即把 `derived_from_source(source)` 移到 `vault_candidates` 之前。若担心来源派生与文件名提示冲突，可保留现顺序但改为**分段配额**（例如库最多占 `max_password_attempts` 的 60%，其余留给派生与字典）。

**验证方式**：构造 vault 中 20 条均不匹配的密码 + 一个用来源域名作密码的包，断言能解出；改前跑红（会被截断而失败）。

---

### P0-3 `partNN` 命名分卷重复入队

**位置**：`core/pipeline.py:108-132`、`core/config.py:9-12`

**现象**：`DEFAULT_ARCHIVE_EXTS` 含 `.rar`。`xxx.part1.rar`、`xxx.part2.rar` 的后缀都是 `.rar`，扩展名判定双双命中，且都带 RAR magic，于是**各自成为一个独立任务**。`part2.rar` 单独解压会失败（缺首卷），用户看到一条莫名其妙的 FAILED，还可能触发重复的探测/解压开销。

（对照：`x.7z.001/.002` 与 `x.zip`+`x.z01` 这两类命名是安全的，见第零节第 3 条。）

**改法**：入队前做一次分卷归并：

- 识别模式：`\.part\d+\.(rar|zip|7z)$`（大小写不敏感）、`\.(7z|zip|rar)\.\d{3}$`
- 同一前缀（去掉分卷序号后的 stem）的组内，只保留序号最小者作为任务，其余标记为 `skipped` 并**不计入失败**
- 若扫描结果里某组的首卷缺失（用户只拖入了 `part2.rar`），给一条明确警告："检测到分卷但缺少首卷 part1.rar，无法解压"

**验证方式**：造 `a.part1.rar`/`a.part2.rar`（可用 7z 分卷后重命名模拟判定逻辑，或直接对判定函数单测），断言只产生 1 个任务；断言非首卷单独输入时报告 warnings 含"缺少首卷"。

---

## 二、P1 性能

### P1-1 全目录嗅探开销（本机实测 +184%）

**位置**：`core/pipeline.py:439-445`（`_enqueue_children`）、`core/archive_detect.py:33`

**实测数据**（同一进程内创建 + 测量，规避 Git Bash 与 Python 的盘符歧义）：

```
样本: 30000 个小文件（300 目录 × 100 个 .jpg）
纯 rglob        :   2744.9 ms
rglob + 嗅探(现行):   7782.4 ms
→ 嗅探额外开销 5037 ms (+184%)
```

**根因**：`_looks_like_archive` 的逻辑是"扩展名命中白名单 → True；否则若开启嗅探 → 读文件头"。对 `.jpg` 这类扩展名未命中的文件，逐个执行 `open()` + `read(4096)`，3 万个文件就是 3 万次系统调用。且嵌套解压时**每一层都重复一次**。

**改法（按收益排序）**：

a) **增加"已知非压缩扩展名"短路集合**。媒体（`.jpg/.jpeg/.png/.gif/.webp/.mp4/.mkv/.mp3/.flac/.txt/.pdf/.nfo`）、Office（`.doc/.xls/.ppt`）等命中即返回 False，**不读文件**。风险极低，收益立竿见影。

b) **更好：用 probe 已有的 entries 预判**。`probe()` 已经把条目清单放进了 `info.entries`。先扫一遍 entries 看有无压缩扩展名，没有就根本不必 `rglob`。注意加密包拿不到 entries（此时回退到 a 或原逻辑）。

> 注意：不要退化成"只对白名单扩展名做判定"——那会丢掉伪装包（`.dat` 实为 zip）的识别能力，现有端到端测试会红。优化只能是"减少无谓的读取"，不能是"取消嗅探"。

**验证方式**：对 a) 加断言测试（`.jpg` 不应触发文件读取，可用 monkeypatch 计数 `Path.open`）；对 b) 加 benchmark 断言耗时下降。

---

### P1-2 重跑不复用已完成任务

**位置**：`core/pipeline.py:82`（`run`）

**现象**：`run()` 每次把 inputs 全量入队，从不查询 `TaskStore` 里已 DONE 的记录。用户误点两次"开始解压"，或中断后重跑，会对同一批包再解一遍——samedir 模式下因 `overwrite_existing=False` 会生成一堆 `包名 (2)`、`包名 (3)`。

**改法**：`run()` 开始时按 `(archive_path, size, mtime)` 指纹查询 store 的 DONE 记录；命中且目标产物目录仍存在 → 跳过（记为 skipped，不计失败）。作为可选开关（默认关，避免"改了包内容却被跳过"的困惑）。

**验证方式**：同一输入连跑两次，断言第二次的 `report.done == 0` 且 skipped 计数正确。

---

### P1-3 任务级串行

**位置**：`core/pipeline.py:133-147`（BFS 主循环）

**现象**：严格串行，一次一个包。目录含几十个独立小包时，CPU 与磁盘都利用不足。

**改法**：引入 `max_workers` 做任务级并发。**但这不是纯收益，前提条件必须先满足**：

- `TaskStore` / `PasswordVault` 的 sqlite 连接默认 `check_same_thread=True`，**跨线程用会直接抛异常**。并发前必须改成每线程独立连接（或用连接池 + 锁）。
- `report` 的累加、`self._plans` 字典的读写都需要加锁或改为线程本地。
- 同一批任务里的嵌套关系（父任务产物 → 子任务输入）构成隐式依赖，不能盲目并发，需按 depth 分层调度。

**建议**：先测量实际收益（多个小包 vs 单个大包场景差异极大），确认存在真实瓶颈再动手，不要为并发而并发。

---

### P1-4 复制回源双倍 IO（可用 move 消除）

**位置**：`core/pipeline.py:491-529`（`_deliver`）

**现象**：workdir 模式 + `copy_back=True` 时用 `shutil.copytree` 复制整个产物树回源目录，磁盘占用与耗时都是解压本身的量级。当前默认 `output_mode=samedir` 已规避此路径，但用户切到 workdir 模式就会遇到。

**改法**：若 `out_dir` 与 `final_dir` 位于**同一文件系统**，用 `os.rename` / `shutil.move` 代替 `shutil.copytree`——同盘 rename 是元数据操作，几乎零成本。跨盘时再回退到 copytree。可用 `Path.stat().st_dev` 是否相同来判断同盘。

**注意**：move 会移走 workdir 里的产物，需确认后续流程（`_prune_consumed_archives` 还会访问 `plan.out_dir`）不再依赖其存在。当前 `_deliver` 在 `_prune_consumed_archives` **之前**执行，顺序上需要一并调整或改为"复制后删源"。

---

## 三、P2 体验与健壮性

| 编号 | 问题 | 位置 | 改法 |
|------|------|------|------|
| P2-1 | `extract_timeout` 默认 3600s，超大包会被误杀，且 UI/CLI **无任何入口**修改 | `core/config.py:57` | 加入 `AppConfig` 并在设置窗口暴露；或改为按解压速度动态续期 |
| P2-2 | 只有单任务进度，无队列总进度 | `core/pipeline.py` | 事件流增加 `queue_total / queue_done` 字段 |
| P2-3 | 失败/待密码任务重试需整批重跑 | `cli.py:226` | 结合 P1-2 的指纹机制，支持"仅重试失败项" |
| P2-4 | 未显式防护符号链接逃逸（Zip Slip） | `core/sevenzip.py` | 7z 默认不还原 symlink，风险低；建议补一条断言测试把该行为固化，防止未来换参数时回归 |

---

## 四、实测证明现状良好，不建议改动

避免"优化"反而引入风险，以下三项经实测确认健康：

1. **密码逐候选尝试**：错误密码 7z 快速失败（0.012s/次），不是性能瓶颈。
2. **7z 风格分卷识别**：`.001` 命中、`.002+` 正确跳过，行为正确。
3. **输出编码与参数组合**：`-y -sccUTF-8 -spd`（`core/sevenzip.py:44-45`）覆盖了防交互、统一编码、防通配符误解析三个坑，保留。

---

## 五、建议实施顺序

```
P0-1 (bomb 失效)  → 安全底线，优先
P0-2 (候选截断)   → 影响解压成功率，一行改动级
P0-3 (分卷入队)   → 影响正确性
P1-1 (嗅探开销)   → 收益明确、改动局部、风险低
P1-2 (幂等跳过)   → 依赖 store，中等改动
P2-1 (超时暴露)   → 顺手补
P1-3 / P1-4       → 需要先验证收益与调整顺序，暂缓
```

前四条都建议遵循项目既有约定：**先写测试在旧实现上跑红，再实现跑绿**，用回退法验证测试的精确性。
