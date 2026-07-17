# 贡献指南 / Contributing

中文说明在前；English notes at the bottom.

## 开发环境

```powershell
python -m venv .venv
# Windows
.\.venv\Scripts\activate
# macOS / Linux
# source .venv/bin/activate

pip install -r requirements-dev.txt
# 或者可编辑安装：
# pip install -e ".[dev]"
```

需要 **FFmpeg / ffprobe** 在 `PATH` 里（smoke 测试会用到）。

CJK 字体：

- Windows：微软雅黑等系统字体
- Ubuntu CI：`fonts-noto-cjk` / `fonts-wqy-zenhei`
- macOS：系统字体或 Noto Sans CJK

## 测试

推荐一条命令跑完语法检查 + 单元测试 +（有 FFmpeg 时）短片 smoke：

```powershell
python scripts\run_tests.py
```

首次环境可顺带装开发依赖：

```powershell
python scripts\run_tests.py --install-dev
```

只跑单元测试 / 强制 smoke / **lint** / **全面 max 套件**：

```powershell
python scripts\run_tests.py --unit-only
python scripts\run_tests.py --smoke
python scripts\run_tests.py --lint         # ruff 门禁（规则在 pyproject.toml）
python scripts\run_tests.py --max          # 长期开发：compile + lint + 全量 + 源码入口 smoke
```

| 命令 | 包含 | 用途 |
|------|------|------|
| `run_tests.py`（默认） | 单元 + smoke（有 FFmpeg 时）；**排除** `max`/`slow`；默认不跑 ruff | 日常 / PR 快速反馈 |
| `--unit-only` | 无 FFmpeg 依赖 | CI 无编码器或极速 |
| `--smoke` | 强调短片 e2e | 改渲染时 |
| `--lint` | `ruff check scripts tests` | 提交前 / CI lint job |
| `--max` | lint + 全部用例 + 源码入口/import/doctor 冒烟 | 发版前 / 大改后 / 长期回归 |

真实制品门禁在 CI 的 `sdist-smoke` job：构建 sdist、解包、从解包目录重建 wheel，并运行非媒体长测。修改 `MANIFEST.in`、`pyproject.toml`、资源定位或 launcher 时，必须同时保证该 job 通过；`run_tests.py --max` 本身不等同于隔离安装测试。

也可用 pytest：

```powershell
python -m pytest tests/ -v
python -m pytest tests/ -v -m smoke
python -m pytest tests/ -v -m "not smoke and not max and not slow"
python -m pytest tests/ -v -m max
```

### 主要测试文件

测试文件在 `tests/test_*.py`（含 UX/job/并发/`test_max_*`）。新增时按 `tests/test_<主题>.py` 命名。
标记：`smoke`（短 e2e）、`max`（全面套件）、`slow`（较慢，仅 `--max`）。

| 文件 | 覆盖 |
|------|------|
| `tests/test_core.py` | 核心渲染、换行、调度 |
| `tests/test_smoke_render.py` | FFmpeg 合成冒烟 |
| `tests/test_pipeline_scenarios.py` | 导出→翻译→导入渲染、lint、规则、XLSX、workdir |
| `tests/test_human_cli_workflows.py` | 人工 CLI 操作矩阵 |
| `tests/test_resume_and_process.py` | job 目录、进程跟踪、翻译 progress |
| `tests/test_output_fps_separation.py` | 弹幕帧率 / 成片帧率分离 |
| `tests/test_leadin_duration.py` | lead-in 时长校验 |
| `tests/test_perf_encode.py` | 编码选项 / 静态帧复用 |
| `tests/test_render_preset.py` | `--render-preset` YAML |
| `tests/test_fps_probe_edges.py` | 29.97 分数帧率探测 / 长序列 sparse 补帧 |
| `tests/test_batch_b.py` | HTML 解析变体、布局预设、移动模式 |
| `tests/test_bugfix_regressions.py` | 历史修复回归（帧采样、duration、fade 等） |
| `tests/test_audit_*.py` | 审计修复：CLI/clean、HTML parser、deep fixes、P0/P1/P2 |
| `tests/test_html_variants.py` | HTML 格式兼容性（单引号、多 class、legacy） |
| `tests/test_validate_and_offset_edges.py` | duration/offset 边界 |
| `tests/test_p2_hardening.py` | validate/hex/emote classes 等加固 |
| `tests/test_font_resolve.py` | 跨平台 CJK 字体检测 |
| `tests/test_doctor_import.py` | `--doctor` 不依赖 .env/API |

### 如何添加新测试

1. 在 `tests/` 下新建 `test_<主题>.py`。
2. 用 `pytest` 标准写法；需要 FFmpeg 的测试加 `@pytest.mark.smoke`。
3. 测试夹具放 `tests/fixtures/`。
4. 本地私有样例（如 `samples/local_chat.html`）会被部分测试可选加载；默认被 `.gitignore` 忽略，**不要提交**。

## 提交前检查

1. `python scripts\run_tests.py` 绿色
2. 改了脚本/测试时再跑 lint：`python scripts\run_tests.py --lint`（CI 也会跑）
3. 若改了渲染逻辑，再跑预览确认：

```powershell
python scripts\render_cn_chat.py video.mp4 chat.html `
  --reuse-translation --translation-json translations\example_translation.json `
  --preview-frame 60
```

4. 不要提交：API Key、私有聊天/视频、`.env`、大体积输出

## 模块入口

| 脚本 | 作用 |
|------|------|
| `scripts/render_cn_chat.py` | 高层 pipeline CLI（翻译 + 渲染） |
| `scripts/twitch_chat_burn.py` | 底层解析 / 渲染 / 合成 |
| `scripts/translate_chat_openai.py` | OpenAI 兼容翻译 |
| `scripts/layout_preset.py` | 布局 YAML |
| `scripts/render_preset.py` | 编码/性能 YAML |
| `scripts/chat_parser.py` | HTML 解析 |

安装后的 console scripts（见 `pyproject.toml`）：

- `twitch-chat-overlay`
- `twitch-chat-burn`
- `twitch-chat-translate`

---

## English (short)

1. Create a venv, `pip install -r requirements-dev.txt` (or `pip install -e ".[dev]"`).
2. Install FFmpeg/ffprobe and a CJK font.
3. Run `python scripts/run_tests.py` (add `--smoke` when FFmpeg is available; `--lint` for ruff).
4. Do not commit secrets, private VODs/chat HTML, or large generated media.
5. Prefer small, tested PRs that fix correctness first. Lint config lives in `pyproject.toml` (`ruff check scripts tests`).
