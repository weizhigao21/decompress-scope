# 密码查找速度优化

密码候选仍按「文件名提示 → 来源派生 → 密码库 → 内置字典」排序，数量上限保持不变。
此次优化减少 ZIP 错误候选启动 7-Zip 的次数，不扩展候选或进行暴力枚举。

## 本机实测

2026-09-29，Windows、Python 3.10.11、7-Zip 26.01；自造 4 KiB 单文件加密包，
先尝试 200 个错误 ASCII 密码，再尝试正确密码。时间包含最后一次真实解压，不包含首次列目录。

| 格式 | 原耗时 | 优化后 | 原 7-Zip 解压调用 | 优化后调用 |
| --- | ---: | ---: | ---: | ---: |
| ZIP / ZipCrypto | 5.643 秒 | 0.117 秒 | 202 次 | 4 次 |
| ZIP / AES-256 | 5.687 秒 | 0.258 秒 | 202 次 | 2 次 |

这次测量约快 48 倍与 22 倍。加密头有随机数据，短校验可能偶然命中，
调用次数与耗时会随包和设备变化。RAR、7z、未能确认结构的 ZIP 继续使用原来的验证路径；
本次 7z 测量仍在约 7.5–9 秒，不承诺这类格式有同样的提升。大文件实际解压时间另计。

复现方法（全部使用自造数据，不读取用户密码库，临时文件自动清理）：

```bash
python tools/benchmark_passwords.py --candidates 200 --baseline
python tools/benchmark_passwords.py --candidates 200
```

## 校验边界

- ZipCrypto 只解读 12 字节加密头；普通条目检查 CRC 高字节，流式条目检查 DOS 时间高字节。
  格式依据：[PKWARE APPNOTE](https://pkware.cachefly.net/webdocs/APPNOTE/APPNOTE-6.3.10.TXT)、
  [CPython ZIP 实现](https://github.com/python/cpython/blob/3.10/Lib/zipfile.py)。
- WinZip AES 使用盐和两字节密码校验值快速排除候选，依据
  [WinZip AES 规范](https://www.winzip.com/en/support/aes-encryption/)。
- 两种短校验都会出现偶然命中，因此通过预筛后仍须由 7-Zip 完整解压、校验内容，
  才会标记完成或记录成功密码。不会仅凭加密头判断密码正确。
- 非 ASCII 密码、未知加密方式、分卷、不一致或无法解析的 ZIP 头部保留原验证路径。
- 查找阶段显示候选进度，筛选每条候选前检查取消请求；人工输入继续由 7-Zip 验证。

回归覆盖真实 ZipCrypto / AES-128 / AES-192 / AES-256、流式 ZIP、
真实短校验碰撞、非 ASCII 候选、取消、格式回退与视频内嵌 ZIP。
