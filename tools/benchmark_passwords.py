"""用自造加密包比较密码候选筛选前后的实际耗时，无需用户文件或密码库。"""
from __future__ import annotations

import argparse
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from core.config import Config, detect_sevenzip
from core.pipeline import Pipeline
from core.store import TaskStore
from core.vault import PasswordVault


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--candidates", type=int, default=200)
    parser.add_argument("--baseline", action="store_true", help="禁用 ZIP 快速预筛")
    args = parser.parse_args()
    if args.candidates < 1:
        parser.error("--candidates 必须大于 0")
    if args.baseline:
        import core.pipeline as module
        module.make_zip_password_filter = lambda _: None
    exe = detect_sevenzip()
    scratch = ROOT / ".workspace"
    scratch.mkdir(exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="password_bench_", dir=scratch) as folder:
        root = Path(folder)
        (root / "data.bin").write_bytes(os.urandom(4096))
        candidates = [f"candidate-{i:05d}" for i in range(args.candidates)] + ["bench-secret!"]
        for kind, options in (
            ("ZipCrypto", ["-tzip", "-mem=ZipCrypto"]),
            ("ZIP-AES256", ["-tzip", "-mem=AES256"]),
            ("7z-AES", ["-t7z", "-mhe=on"]),
        ):
            archive = root / (kind + (".7z" if kind == "7z-AES" else ".zip"))
            proc = subprocess.run(
                [str(exe), "a", *options, "-pbench-secret!", str(archive), "data.bin"],
                cwd=root, capture_output=True, creationflags=(
                    subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0),
            )
            if proc.returncode:
                raise RuntimeError(proc.stderr.decode(errors="replace"))
            vault = PasswordVault(root / f"{kind}.db")
            store = TaskStore(root / f"{kind}.db")
            try:
                pipe = Pipeline(Config(exe, root / "work"), vault, store)
                calls = 0
                original = pipe.sz.extract

                def counted(*positional, **kwargs):
                    nonlocal calls
                    calls += 1
                    return original(*positional, **kwargs)

                pipe.sz.extract = counted
                start = time.perf_counter()
                result = pipe._attempt_extract(archive, root / kind, candidates, 1)
                elapsed = time.perf_counter() - start
                assert result.password == "bench-secret!", result
                assert (root / kind / "data.bin").read_bytes() == (root / "data.bin").read_bytes()
                print(f"{kind}: {args.candidates} wrong candidates, {elapsed:.3f}s, {calls} 7z calls")
            finally:
                vault.close()
                store.close()


if __name__ == "__main__":
    main()
