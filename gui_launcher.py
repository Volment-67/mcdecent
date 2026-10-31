"""图形界面入口（供 PyInstaller / pythonw 使用）。

额外支持 `--selftest`：在无控制台的冻结程序里跑一遍命令层，把输出写到
``%MCDECENT_SELFTEST_LOG%``（默认 exe 同目录 selftest.log），用于验证打包后
命令模块是否都被正确注册（冻结环境里 pkgutil 不能枚举源码目录）。
"""

import contextlib
import io
import os
import sys
import traceback
from pathlib import Path


def _selftest_log_path() -> Path:
    env = os.environ.get("MCDECENT_SELFTEST_LOG")
    if env:
        return Path(env)
    try:
        return Path(sys.executable).resolve().parent / "selftest.log"
    except Exception:  # noqa: BLE001
        return Path.cwd() / "selftest.log"


def _run_cli(argv: list[str], buf: io.StringIO) -> int:
    from mcdecent import cli

    try:
        with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(buf):
            return int(cli.main(argv) or 0)
    except SystemExit as exc:  # argparse 的 --version/--help 会走这里
        return int(exc.code or 0)


def _selftest() -> int:
    buf = io.StringIO()
    rc = 1
    try:
        rc = _run_cli(["--version"], buf)
        buf.write("\n")
        rc = _run_cli(["list", "--json"], buf)
        _run_cli(["game", "status", "--json"], buf)
    except Exception:  # noqa: BLE001
        buf.write("\n[selftest 异常]\n" + traceback.format_exc())
        rc = 1
    try:
        _selftest_log_path().write_text(buf.getvalue(), encoding="utf-8")
    except Exception:  # noqa: BLE001
        pass
    return rc


def main() -> int:
    if "--selftest" in sys.argv:
        return _selftest()
    from mcdecent.gui import main as gui_main

    return gui_main()


if __name__ == "__main__":
    sys.exit(main())
