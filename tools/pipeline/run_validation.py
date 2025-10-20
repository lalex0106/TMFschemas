#!/usr/bin/env python3
"""运行 TMF 官方校验脚本并输出结果文件。"""

from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="执行 TMF schema 校验")
    parser.add_argument(
        "--schemas",
        type=Path,
        default=Path("dist/validation"),
        help="待校验的 schema 根目录，默认为 dist/validation",
    )
    parser.add_argument(
        "--config",
        type=Path,
        default=Path(".circleci"),
        help="TMF 校验配置所在目录，默认为 .circleci",
    )
    parser.add_argument(
        "--validator",
        type=Path,
        default=Path(".circleci/validate.js"),
        help="校验脚本路径，默认为 .circleci/validate.js",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=Path("dist/validation/validation_results.txt"),
        help="校验结果输出文件，默认为 dist/validation/validation_results.txt",
    )
    return parser.parse_args()


def ensure_directory(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)


def main() -> None:
    args = parse_args()
    if not args.validator.exists():
        raise SystemExit(f"未找到校验脚本: {args.validator}")
    if not args.schemas.exists():
        raise SystemExit(f"待校验目录不存在: {args.schemas}")

    command = ["node", str(args.validator), str(args.schemas), str(args.config)]
    try:
        completed = subprocess.run(
            command,
            check=False,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
        )
    except FileNotFoundError as exc:
        raise SystemExit("未找到 Node.js，请先安装或配置 node 命令") from exc

    ensure_directory(args.output)
    args.output.write_text(completed.stdout, encoding="utf-8")
    print(completed.stdout)
    if completed.returncode != 0:
        print(
            "⚠️  校验检测到错误，详情请查看", args.output,
            file=sys.stderr,
        )
    sys.exit(completed.returncode)


if __name__ == "__main__":
    main()
