# 解压开镜

> 把互联网上那些乱七八糟的多层压缩包，一次性解干净。

从网盘拖下来的一堆 `.zip`，打开里面还是 `.rar`，再打开里面是加密的 `.7z`，密码写在文件名里、或者藏在站点域名里——手动解压是纯粹的体力劳动。**解压开镜**把这个过程做成一条自动流水线：递归解压到最内层、自动提取并尝试密码、把试通的密码记进密码库，下次遇到同来源的包直接命中。

![主界面](shots/05_main_new.png)

## 核心能力

- **递归解压** — BFS 逐层展开内层压缩包，深度上限可配（默认 5 层）。内层识别不只看扩展名：`core/archive_detect.py` 读文件头 magic bytes，能认出改了名的伪装包（`.dat`、无扩展名等）。
- **密码三级来源** — 按命中率排序依次尝试：文件名/注释正则提取 → 来源域名派生 → 密码库历史记录 → 内置常用字典。
- **密码库自学习** — 试通的密码自动入库并累计命中次数，按来源分组。下次遇到同一站点的加密包，一次命中。
- **分卷归并** — 识别 `.partNN.rar` 与 `.7z.001` 两派命名，只解首卷，不产生注定失败的任务。
- **护栏** — 解压后总大小上限（默认 50 GiB）与压缩比上限（默认 1000:1），防 zip bomb；加密包同样生效（拿到密码后复探真实大小）。
- **输出落点可选** — 解到源目录、或解到隔离工作目录再决定是否复制回源；同名输出自动避让，绝不覆盖。
- **两套入口** — 命令行（`cli.py`）与图形界面（PySide6），共用同一套引擎。

## 环境要求

| 依赖 | 说明 |
| --- | --- |
| Python | 3.10+ |
| [7-Zip](https://www.7-zip.org/) | 必需。引擎是 `7z.exe` 子进程，不是纯 Python 归档库——格式覆盖、GBK 文件名还原、加密与分卷探测都靠它 |
| PySide6 | 仅图形界面需要；纯命令行可不装 |

`7z.exe` 的定位顺序：显式 `--sevenzip` 参数 → 环境变量 `SEVENZIP_PATH` → 常见安装路径（`C:\Program Files\7-Zip\` 等）→ 系统 `PATH`。

## 快速开始

```bash
# 图形界面
python cli.py gui

# 或直接拖文件夹进去（界面支持拖入即开始）
python -m ui
```

命令行递归解压：

```bash
python cli.py extract "E:/下载/某漫画" --source example.com
```

## 命令行参考

```bash
# 递归解压（可传多个文件或目录）
python cli.py extract <路径...> [--source 站点域名] [--max-depth 5]
                              [--max-total-gb 50] [--max-ratio 1000]
                              [--samedir | --workdir-only] [--subdir 名字]
                              [--no-delete-intermediate] [--delete-original]
                              [--no-sniff] [--no-config]

# 密码库
python cli.py pass-add <密码> [--source 站点域名]
python cli.py pass-list

# 配置（持久化到 config.json）
python cli.py config                              # 打印当前配置
python cli.py config --set max_depth=6            # 可重复传多个
python cli.py config --set output_mode=samedir
```

## 密码从哪来

候选密码按下列顺序合并，大小写去重，取前 `max_password_attempts` 个（默认 20）逐个尝试：

1. **文件名 / 注释里提取** — 命中的提示词如 `解压码`、`密码`、`passwd`，以及 `[xxx]` 方括号内容
2. **来源派生** — 由站点域名变形而来，实测命中率最高的一档
3. **密码库命中** — 该来源下历史试通的密码，按命中次数排序
4. **内置常用字典** — 兜底

> 顺序很关键：前两档是"这个包特有的线索"，后两档是"通用猜测"。高价值候选必须排在截断之前。

## 输出落点

三个维度正交，组合出四种行为：

| 配置项 | 取值 | 说明 |
| --- | --- | --- |
| `output_mode` | `samedir`（默认） | 解到 `<源目录>/_解压开镜/<包名>/` |
| | `workdir` | 解到隔离工作目录 `task_<id>/out` |
| `copy_back_to_source` | 布尔 | 仅 `workdir` 模式有意义：是否复制回源目录 |
| `overwrite_existing` | 布尔（默认关） | 关闭时同名输出自动避让为 `包名 (2)`，绝不覆盖 |

## 架构

```
core/          纯 Python 引擎，零 Qt 依赖（有守卫测试保证）
  pipeline.py      主流程编排：入队 → 探测 → 试密码 → 解压 → 内层入队 → 落地
  sevenzip.py      7z.exe 子进程封装
  probe.py         元数据探测、加密判定、zip bomb 检查
  archive_detect.py 文件头嗅探、分卷识别、复合文档排除
  password_finder.py 密码候选生成与排序
  vault.py         密码库（SQLite）
  store.py         任务状态机存储
  output_plan.py   输出落点决策（纯函数）
  appconfig.py     用户偏好持久化
  config.py        运行期配置 + 7z 定位

ui/            PySide6 图形界面，单向依赖 core
  main_window.py     主窗口（拖入即开始）
  settings_window.py 设置窗口
  vault_window.py    密码库管理窗口
  theme.py           设计令牌与样式表
  worker.py          工作线程（QThread 桥接 core 的纯回调事件）

cli.py         命令行入口
```

**关键约束**：`core/` 不允许导入任何 Qt 模块，保证引擎可独立测试、可被 CLI 与 GUI 共用。这条约束由 `tests/test_core_no_qt.py` 静态守卫。

## 配置项

配置持久化在项目根目录 `config.json`（首次启动自动生成，也可用 `cli.py config --set` 或设置窗口修改）。

| 键 | 默认 | 含义 |
| --- | --- | --- |
| `max_depth` | 5 | 递归解压层数上限 |
| `max_total_gb` | 50 | 解压后总大小上限（GiB） |
| `max_ratio` | 1000.0 | 压缩比上限 |
| `max_password_attempts` | 20 | 每个包尝试的密码候选数上限 |
| `sniff_archives` | true | 是否启用文件头嗅探识别伪装包 |
| `output_mode` | `samedir` | 输出落点模式 |
| `subdir_name` | `_解压开镜` | `samedir` 模式下的容器目录名 |
| `copy_back_to_source` | true | `workdir` 模式下是否复制回源目录 |
| `overwrite_existing` | false | 是否允许覆盖同名输出 |
| `delete_intermediate` | true | 成功后清理中间层压缩包 |
| `keep_original` | true | 是否保留原始输入包 |

## 测试

```bash
python -m pytest -q
```

当前基线 **180 passed**。测试覆盖真实 `7z.exe` 的端到端解压链路（含加密包、伪装包、分卷、bomb 拦截）与离屏渲染的界面冒烟测试。

## 说明

仅供个人整理自己的下载文件使用，请遵守内容来源方的服务条款与当地法律法规。
