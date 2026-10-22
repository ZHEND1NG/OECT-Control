# OECT 更新记录

## 2026-10-22：完整 GitHub 项目整理

- 源码统一放在 `OECT/`，包含全部 9 个 Python 文件。
- 仓库根目录提供英文安装及使用说明、`requirements.txt`、`.gitignore` 和 Windows 启动脚本。
- 启动方式：在仓库根目录执行 `python -m OECT.cli`，或安装后使用 `start_oect.bat`。
- `gui.py` 的默认输出路径改为仓库内的 `experiment_data/oect`，适配目前的目录层级。已有电脑保存的 Output 设置仍会优先恢复；必要时在界面中修改。
- 本目录属于完整项目，不再采用旧版“只替换 7 个部件”的安装方式。

## 已包含的功能更新

- 模式统一为 `linear` / `cycle`，各自通过 `Enable Reference feedback` 控制 Reference 测量和控制。
- linear Reference 使用起点、终点及中间点数；中间点数为 0 时起终点必须相等。
- linear Reference 根据绝对 tolerance 和固定 Gate 步长反馈；检查间隔可设置为 1、2、5 秒等。
- cycle Reference 保留原有多次采样、过线/容差判定、往返控制。
- Gate 上下限、暂停/恢复、暂停期间修改参数、参数变更记录继续有效。
- 实时读数显示详细数值；Gate V 坐标刻度随缩放调整精度；右上图显示 Gate Current。

使用方法见 [仓库 README](../README.md)。详细控制行为及旧接口兼容规则见 [CONTROL_DETAILS.md](../docs/CONTROL_DETAILS.md)。
