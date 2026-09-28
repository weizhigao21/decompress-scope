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

### 第三批（2026-09-26，交付链路可观测性 + 工作目录残留）

起点不是性能，是一次实测：`.workspace/task_83` 里躺着 **5.38 GB / 123 文件**，而库里
`status=done`、界面显示「完成」。全项目 grep 确认**没有任何 workdir 清理机制**，
且交付失败的处理是 `report.warnings.append(...) + continue` —— 警告只活在本次运行的
内存里，窗口一关就没了。用户既拿不到产物，也无从知道磁盘被谁吃了。

测试基线：276 passed → **316 passed**（新增 36 个测试 + 事后补的 3 个溢出守卫，另 1 项 skip）。

| 缺陷 | 位置（旧实现） | 改法 | 验证（先在旧实现上跑红） |
|------|----------------|------|--------------------------|
| 交付失败不落库 | `_deliver` 的 `except` 只 `_warn(...) + continue` | `Task.delivery_error` 落库（`tasks` 表加列 + 老库 `ALTER TABLE` 迁移）；`_record_delivery_failure` 同时进 report 与事件流 | `test_copy_back_failure_is_persisted`；注入「不落库」后 4 条精准变红 |
| 归并失败**静默丢弃** | `_deliver_hierarchy` 的 `except (ValueError, OSError): continue`，连警告都没有 | 同上，并写明"内层包已不在父产物内" | `test_hierarchy_unlocatable_archive_is_reported_not_silent`；注入回 `continue` 后精准变红 |
| 跨卷交付后落点仍指工作目录 | `_deliver` 只有 `moved=True` 才更新 `extracted_dir` | 交付成功即更新（搬移与复制都算成功） | `test_successful_delivery_points_extracted_dir_at_target`、`test_cross_volume_delivery_keeps_workdir_copy`（后者是**既有测试**，语义变更后同步更新并写明理由） |
| 残留无法盘点、无法清理 | 无 | 新增 `core/workdir_cleanup.py`（`scan_workdir` 定性、`prune` 三道闸）+ CLI `clean-workdir` + `ui/workdir_window.py` + 残留计数入口（**现位于 设置 → 输出位置**，最初常驻主界面底部，后随低频设置一起移入设置窗口） | `tests/test_workdir_cleanup.py`（16 条）、`tests/test_ui_undelivered.py`（19 条） |
| 清理 > 2 GiB 后残留计数不刷新（**功能静默失效**） | `WorkdirWindow.cleaned = Signal(int)` —— PySide6 映射到 C++ **32 位** int，上限 2 GiB；`emit()` 越界**不抛异常**，只发一条 shiboken Overflow 警告再丢掉信号 | 改 `Signal("qint64")`；新增静态扫描禁止 ui 层出现裸 `Signal(int)` | 三条独立守卫：静态扫描 / 槽收到的值 / 端到端计数归零（当时断的是主界面页脚，入口移入设置后同样的三条断言改断设置窗口） |

**定性与安全边界**（核心决策）：

- 定性只有一条硬判据——**产物是否已确认交付到工作目录之外**。其余（交付失败、
  落点仍在工作目录内、任务非 DONE、库里无记录）一律标「请保留 / 待确认」，
  **默认不勾选**。task_83 那 5.38 GB 当场被正确判为「请保留」。
- 清理三道越界闸：目标必须是工作目录的**直属** `task_<数字>` 子目录（`resolve()` 后
  比对父目录）、不能是链接/junction、非勾选项不删。
- **实测发现 `os.path.islink()` 对 Windows junction 返回 `False`**（见下），
  只判 `islink` 会留一个越界删除口子，必须查 `st_file_attributes` 的 reparse 标志位。

**同时修掉一个界面层的可见性缺陷**：残留窗口的选择列在暗色底下几乎没有对比度——
`QCheckBox::indicator` 那套 QSS **不覆盖** item view 的指示器，于是落到平台默认样式。
新增 `QTreeView::indicator` 等规则复用同一套画法；守卫用**像素扫描**断言勾选框真的
画出 ACCENT 底（摘掉规则后命中 0 像素，精准变红）。

**实测暴露的第二个静默失效（清理真的用起来才发现）**：`Signal(int)` 在 PySide6 里是
**C++ 32 位 int**（上限 2,147,483,647 ≈ 2 GiB），而 `cleaned` 传的是**释放的字节数**。
用户清掉 task_83 那 5.38 GB 时，`emit()` **不抛异常**——只往 stderr 打一行
`libshiboken: Overflow: Value 5782098582 exceeds limits of type [signed] "int" (4bytes)`
和 `AttributeError: Slot 'MainWindow::_on_workdir_cleaned(int)' not found.`，
信号被静默丢弃：文件删干净了，但主界面页脚的残留计数不归零、"已释放 xx"提示不出现。
`try/except` 在调用方抓不到任何东西。改成 `Signal("qint64")`（9.2 EB，离溢出很远）。
守卫按三个层次写，缺一层就漏：**静态扫源码**（`re.search(r"Signal\(\s*int\s*\)")`，
跳过注释行——解释这条规则的注释里必然出现这个字面量）、**槽收到的值等于原值**
（断言"没报错"是假绿，旧实现确实不抛）、**端到端页脚计数归零**（用户真正失去的是这个）。

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

### 第四批（2026-09-26，分卷包的大小）

用户反馈：**分卷压缩包的大小只显示了首卷**。

根因一行：`probe.parse_slt` 里 `info.archive_size = archive_path.stat().st_size`，
而任务里存的 `archive_path` **只是首卷**（`pack.zip.001`）。实测一个 4 卷的包：

| 量 | 首卷 | 整组 |
|---|---|---|
| 包体大小 | 1,048,576 | **3,145,876** |
| compression_ratio（分母同上） | 3.00 | **1.00** |

不只是显示不准——`compression_ratio` 的分母就是它，**分母小则比值大**。
另造一个 3 MB 内容压成 4 卷（共 124 KB）的包实测：真比值 **25.4:1**，
只算首卷报成 **96.0:1**（高估 3.8 倍）。分卷越多偏得越多，默认 1000:1 的阈值下
正常包会被**误判成 zip bomb 并拒解**——安全机制反过来误伤正常文件。

改法：新增 `archive_detect.volume_group_total(path) -> (总字节, 卷数)`，
复用既有的 `volume_info` 分组标识（含扩展名，所以 `a.part01.rar` 与 `a.rar.001`
不会串组），一次目录遍历同时得出总量与卷数；非分卷短路成"自身大小、1 卷"，
只有真分卷才 listdir。`ArchiveInfo` 增加 `volume_count`，info 事件带出去，
界面 tooltip 写「压缩包 3.0 MB（4 个分卷）」——否则用户看到列里那个数比拖进来的
`.001` 文件大，会以为这次又算错了（他反馈的正是"大小不对"）。

测试基线 316 → **329 passed**（+9 单元 / +2 真 7z 集成 / +3 界面），全部做过注入验证：

| 注入 | 变红 |
|---|---|
| `probe` 退回 `stat().st_size` | 2 条集成（其中一条直接报"正常的分卷包被判成 zip bomb"） |
| `volume_group_total` 退回自身大小 | 5 条单元（另 3 条"配重"保持绿） |
| 去掉 `is_file()` 过滤 | 精准 1 条（目录名像分卷也不许计入） |
| 界面退回不写卷数 | 精准 1 条 |

比值那条测试刻意把阈值卡在**真比值与首卷比值之间**，并前置断言两者相差 2 倍以上——
没有这两步，用例在"整组本来就比首卷大一点点"的样本上会恒绿，测了个寂寞。

---

### 第五批（2026-09-26，分卷包的类型）

上一批的"顺带发现"落地：`.zip.001` 的「类型」列显示 **"Split"**。

根因不是"我们算错了"，而是 **7z 明明给了真答案，我们只读了第一行**。
实测一个 zip 分卷包的首卷，`7z l -slt` 报的是：

```
Path = p.zip.001
Type = Split            <- 外层：Split 容器
Volumes = 7
Total Physical Size = 208645
----
Path = p.zip
Type = zip              <- 里面真正的格式
```

`parse_slt` 用 `_TYPE.search(text)` 取第一个匹配，拿到的永远是容器名。
于是用户问「这是什么包」，界面答「这是分卷」——而分卷与否已经由大小列的
「（N 个分卷）」说过了，类型列再说一遍等于没说。

改法：新增 `probe._pick_format`，跳过容器名（`_NOT_A_FORMAT = {"", "split"}`）
取第一个真格式；**magic 嗅探退为兜底**，只在 7z 一个格式名都没给（头部加密包）
或只给了容器名时才读文件头。这样"格式"的首要来源重新变成 7z 自己的解析结果，
而不是我们去猜文件头。

#### 这批最值钱的教训：两条路径重叠会制造假绿

第一次注入验证时，把 `parse_slt` 退回旧实现（取第一个 Type）——
**离线样本那条测试红了，集成测试和界面端到端却全绿**。

原因：magic 兜底把失效的主路径顶住了。`.zip.001` 的首卷文件头恰好是
`PK\x03\x04`，兜底读一眼就能认出 zip，于是"7z 报告解析"整条主路径坏掉也测不出来。

这比"没写测试"更隐蔽——两条路互为掩护，任何一条单独坏掉都看不出来。
修法：集成那条**必须掐掉兜底**（`monkeypatch` 把 `probe.sniff_format` 打成一律返回空），
绿了才算证明真格式确实取自 7z 报告。不掐兜底的那版留给界面端到端，
它守的是"用户最终看到 zip"这个事实，两层各司其职。

判断一个测试有没有区分力的土办法：**问它"哪个合理的回归会让它变红"**。
答不上来的，它就是摆设。

测试基线 330 → **340 passed**（+9 解析层 / +1 真 7z 集成 / +1 界面断言）：

| 注入 | 变红 |
|---|---|
| `parse_slt` 退回 `_TYPE.search`（取第一个 Type） | 3 条（2 解析 + 1 集成·掐兜底版） |
| 兜底改成"无条件用文件头覆盖" | 3 条（2 条兜底语义 + 1 条集成） |

界面那条端到端在两次注入下都保持绿，这是**对的**：它断言的是用户可见结果，
而兜底确实保住了那个结果。这个区别写在两边的 docstring 里，免得后来人误判它没有区分力。

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

**2026-09-26 复核追加**：上面的改法只解决了"顺序"，没解决"库检索范围"。当时仍留着两处说不通的地方：

1. `candidates_for(source, limit=20)` 里的 `20` 是**硬编码**的，紧接着调用方又 `[:max_password_attempts]` 截断一次 —— **截断发生两次**，于是用户把「密码尝试」从 20 调到 200 完全不生效。设置项在说谎。
2. 该函数在 `source` 非空时只查 `source=?` 与 `source=''` 两组，**跨来源的条目永远不会被试**。而 `record_success` 是按 `extract_source(文件名)` 写入的，所以来源一分组就互不通气。

现已改为：库配额取自 `cfg.max_password_attempts`；检索改为单条 SQL 的三段式排序（同来源 → 无来源 → 其他来源，段内 `hit_count DESC, last_hit_at DESC`），`limit` 作用在**排序去重之后**，0 = 不限。跨来源参与的理由见 `PasswordVault.candidates_for` 的 docstring —— 站点归属是**排序信号**，不是隔离边界（同一发布者批量打包、用户复用同一密码都是常态）。

**注意别改回去**：`test_candidates_ordering` / `test_candidates_limit_truncates_after_priority` / `test_candidate_budget_follows_setting_and_reaches_other_sources` 三条钉住了新语义，把 `limit` 改回硬编码 20 或把 SQL 改回按来源隔离，它们会精准变红（已做过注入验证）。

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

   **2026-09-26 复测与上界**（起因：有人问"我写的破解器 15 万次/秒，提前算好 MD5 不该很快吗"）。
   实测单次「错误密码」尝试：`.7z` 19.4 ms、`.7z`(`-mhe=on`) 18.8 ms、`.zip`(ZipCrypto) 24.8 ms、
   `.zip`(AES-256) 30.8 ms → **32–53 次/秒**。同机裸 MD5 循环 1,424,659 次/秒。
   差距的成因有三层，**都不是代码写得慢**：

   - **进程创建 8.9 ms 起跳**（实测纯 `7z.exe` 启动），这一项就把上界锁在 ~113 次/秒。
     想提速只能离开子进程模型。
   - **快不快看 KDF 轮数，能不能预计算看有没有盐**：7z = SHA-256 迭代 2¹⁹（524,288 轮）；
     WinZip AES = PBKDF2-HMAC-SHA1 1000 轮 + 每文件盐；RAR5 = PBKDF2-HMAC-SHA256 2¹⁵ 轮 + 盐。
     **有盐 → 预计算表在原理上就不成立**（目标哈希随每个包的盐重新生成，每换一个包就要重算整张表）。
     MD5 则根本不参与任何压缩格式的密码校验——所以"校验一下 MD5"这个思路从根上就不对。
   - **ZipCrypto（传统 ZIP 加密）是唯一能跑到"十几万次/秒"的压缩包加密**（2026-09-26 补）：
     密钥派生只是 3 个 CRC32 状态、每字节 3 次查表（约 36 次查表/候选，无盐）；
     校验位是解密后 12 字节加密头的**最后 1 字节**——`bit3=0` 时比 CRC32 高字节，
     `bit3=1`（流式写、CRC 在数据描述符里）时比 **DOS 时间的高字节**。
     纯 Python 进程内实现实测 **124,744 次/秒**（自造包）；**拿 2.6 GB 真包的加密头测 131,989 次/秒**。
     所以有人问"我那边十几万次/秒"时，第一句该问的是**哪种加密**：
     ZipCrypto 完全做得到，7z-AES 做不到 —— 两者差的不是代码。
   - **进程内验证对 zip 系有效**：ZipCrypto 见上（~13 万次/秒）；zip AES 用
     `hashlib.pbkdf2_hmac`（C 实现）实测 2,009 次/秒（≈43× 提速），但要自己解析 local header
     取 0x9901 extra 里的盐与 2 字节验证值。
     **7z 不能这么干**：52 万轮 KDF 纯 Python 逐次调用要 0.29 s，比让 `7z.exe` 干（~19 ms）还慢 15 倍。

   **真实素材对照**（2026-09-26，取自本机的 BaiduNetdisk 下载目录）：

   | 包 | 加密形态 | 现在每试一个密码 | 进程内可做到 | 差距 |
   |---|---|---|---|---|
   | `RJ01716841.zi删p`（2.6 GB，`flag=0x0809`） | **ZipCrypto** | 53 次/秒（18.7 ms） | **132,000 次/秒** | **~2500×** |
   | `RJ01658185.7z.001`（4.9 GB，`LZMA2:23 7zAES:19`，140/160 条目加密） | 7z AES-256 | 56 次/秒 | ~110 次/秒（进程内无收益，启动就 9 ms） | — |

   结论：**ZipCrypto 包在被无偿地按 7z 子进程的慢路径处理**。这不是"算法贵"，
   是"拿一把 20 ms 的锤子去敲一颗 8 µs 的钉子"。而 7z-AES 那一侧，慢是真的、
   改不动的慢。

   结论：只要还走 `7z.exe`，`max_password_attempts` 每加 1 就是每包约 20 ms。
   配额定在 20 时，单个需密码的包最坏约 0.4 s —— 这是**设计上限，不是瓶颈**。

   **2026-09-26 后续**：用户随即撞上「这个数字改不动」。原因不是控件坏了，而是上限
   被写死成 **200**，且在**两处**各写了一份 —— `core/appconfig.py` 的 `_CLAMP`
   （`normalize()` 在加载与保存前都会跑，连手改 config.json 都会被打回）与
   `ui/settings_window.py` 的 `setRange(1, 200)`。用户想让它试完整个库，输入被无声
   夹回，界面上完全看不出是谁夹的。

   已改：上限放开到 **2000**（与 `core/vault.py` 的 `_SCAN_CAP = 2000` 对齐，避免
   "配额允许 3000 条、库却只吐 2000 条"的第二层隐形天花板）；新增
   `core.appconfig.clamp_bounds(name)`，设置窗口所有数字框的范围**只从这一份定义取**
   （`SettingsWindow._spin`），UI 里不再出现第二份常量；控件下方实时显示
   "最坏 ≈ N 秒/需密码的包"，让放开的代价可见。

   **守卫**：`test_numeric_control_ranges_come_from_core_bounds`（钉 UI 与 core 的
   一致性）、`test_attempts_can_go_past_200_and_survives_save`（钉"真的能调上去并存住"）、
   `test_attempts_hint_shows_worst_case_cost`。注入验证：上限退回 200 → 第二条红；
   UI 自写一份范围 → 第一条红。

   **2026-09-26 三度后续：`7z t` 干跑预筛 —— 实测否决（不改）**
   （动机：想省掉每个失败候选在输出目录里「建目录 → 删目录」的开销，改成先用 `7z t`
   干跑筛出正确密码、再真解压一次）。三条实测把它否掉了：

   - **失败路径几乎省不到东西**。真实包 `RJ01716841.zi删p` 是**单条目**包
     （`7z l -slt` 只吐 1 个 `Path`，就是那个 2.6 GB 的文件本体），错误密码下
     `x` 17–23 ms、`t` 13–16 ms —— 7z 试一条就收手，两者只差进程启动的噪声。
     自建 30 条目 ZipCrypto 包：`x` 32 ms、`t` 14.5 ms，差的 18 ms 是 7z 给**每个条目
     建了一个 0 字节文件**。即：最坏情况一条候选省约 18 ms，20 条候选共约 0.36 s。
   - **成功路径要付一遍全量解压**。`7z t` 的语义是**校验完整性**：正确密码下它会把每个
     条目解压出来算 CRC。自建包实测 `t -pRIGHT` 22.7 ms，而纯进程启动 15 ms —— 4.5 MB
     全解了。推演到 2.6 GB 的真实包（未直接实测，因为不知道真包密码）：等于每次成功解压
     之前先白白解压一遍（Deflate 十几~几十秒），然后 `x` 再解一遍。
     **拿 0.36 s 的收益去换分钟级的代价。**
   - **顺带挖到一个地雷**：`7z t <包> -p<密码> <条目名>`（带条目过滤）在**过滤不到任何条目**时
     返回 **exit=0 + `No files to process`**。实测 `t -pWRONG <最小条目>` → exit 0、
     "Everything is Ok"。也就是说，「只测一个最小条目来加速」这条路会把**错误密码判成成功**。
     将来任何「`t` + 过滤」的写法都必须先排除 `No files to process`，否则就是静默放行错误密码。

   结论：密码筛选路径**维持原状**。真实包上每条候选 17–23 ms，配额定 20 时最坏约 0.4 s，
   相对动辄几十秒的解压是 1% 量级。要动就该动解压本身（并行跑多个互不依赖的包），
   而不是继续在筛密码上抠毫秒。
2. **7z 风格分卷识别**：`.001` 命中、`.002+` 正确跳过，行为正确。
   ⚠️ 更正（见第四批）：**识别**确实没问题，但同一批改动之外还藏着"只按首卷算包体大小"的
   缺陷——分卷那条链路当时只验了"入队几条"，没验"大小报多少"。
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

---

## 2026-09-28：0.3.0 发布补充

以上内容保留各批次审查的历史背景。本次发布补充如下：

- 覆盖输出先暂存再交付，解压失败保留已有目录；重跑指纹涵盖原始包和分卷变化；密码候选与库保留大小写差异；嵌套分卷仅首卷入队。
- 增加当前用户 Windows 右键菜单和紧凑解压窗口，菜单对所有文件与文件夹显示图标；窗口显示阶段、进度和耗时，并在自动密码失败后显示密码输入表单。
- Windows 7-Zip 子进程隐藏控制台；提供带图标、禁用 UPX 的单文件 EXE 打包，配置与数据写入用户 Local AppData。
- 检查视频内嵌 ZIP 的目录与边界，恢复有效 ZIP 后交给 7-Zip；自动查找同目录的配套分卷载体，归并内层分卷继续解压，源视频保持不变。

完整测试基线：**376 passed，1 skipped**。内嵌 ZIP 已覆盖加密、干扰尾部、取消清理与跨载体分卷的真实 7-Zip 测试。用户提供的大文件样本已验证 ZIP 边界，实际内容解压仍待正确密码验证。使用与打包说明见 [README](../README.md)，版本变更见 [更新记录](../CHANGELOG.md)。
