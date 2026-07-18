# Twitch Chat Translator Overlay

将 Twitch 直播录像的聊天弹幕翻译成中文（或其他语言），以半透明覆盖层形式压制到视频画面上。

发布记录：[`CHANGELOG.md`](https://github.com/yigenhuobahh/twitch-chat-translator-overlay/blob/main/CHANGELOG.md)

> **输入**：一段录像视频 + TwitchDownloader 导出的聊天 HTML  
> （可选）用本工具 `--download` / 菜单「下载素材并继续」调用 TwitchDownloaderCLI 自动获取  
> **输出**：带中文翻译弹幕的 MP4 视频

## 极速上手（一键安装 / 一键运行）

> 如果您没搞懂，可以把该readme下载或复制粘贴发给AI，如果发现确实是脚本问题，可以开issue或者pr

**Windows（推荐）**

```powershell
# 1. 下载后双击执行
install.bat

# 2. 双击 run.bat
#    [1] 新建配置 → 问答引导，生成带逐项注释的 jobs\xxx.yaml
#    [2] 使用已有配置 → 一键复用
#    或: run.bat new
#    或: run.bat example_job
#    跑完成功后会问：是否打开输出目录、是否清理 workdir/temp 下 *.partial*
#    （无 workdir 时不提供清理，避免误清视频目录旁其他 job）

```

**Linux / macOS**

```bash
bash install.sh
bash run.sh          # 菜单：新建 / 复用
bash run.sh new
bash run.sh example_job
```

**等价命令行**

```powershell
pip install -r requirements.txt
python scripts\render_cn_chat.py --init
python scripts\render_cn_chat.py --doctor
# 缺 FFmpeg 时可: python scripts\render_cn_chat.py --doctor --offer-fix
python scripts\render_cn_chat.py video.mp4 chat.html --mode preview --render-original
python scripts\render_cn_chat.py --job jobs\example_job.yaml
# 可选: --init-job / --list-jobs；预览可加 --output preview.mp4
# 便携 FFmpeg: 解压到 tools/ffmpeg/（bin 下有 ffmpeg/ffprobe）
```

> **配置文件**：`jobs/*.yaml` 里每个参数都有中文注释；示例见 [`jobs/example_job.yaml`](jobs/example_job.yaml)。CLI 显式参数优先于 YAML。
> **默认输出**：不带 `--output` 时为源视频同目录 `<视频名>_chat.mp4`。
> **`--mode`**：`auto`/`full` 完整流程；`preview` 默认 10s；`translate` 只翻译；`render` 只渲染（需 reuse/original）。
> **短名预设**：`--layout-preset compact`、`--render-preset fast`。
> **可复用配置**：`jobs/*.yaml` 默认**不写死** video/chat（注释掉）；运行时询问或
> `python scripts\render_cn_chat.py --job style.yaml video.mp4 chat.html`。
> 只有在 YAML 里**取消注释**路径时才固定跟配置走。
> **预设短名**：`layout_preset: default|compact|mobile`、`render_preset: default|fast|hq`
> （`default` 对应 `layout_default.yaml` / `render_default.yaml`，不是翻译用的 `profiles/default.yaml`）。

### 并行跑多个任务安全吗？

| 场景 | 是否安全 | 说明 |
|------|----------|------|
| 默认（自动 `job_*` 目录） | **安全** | 同 `--out-dir` 下每个进程独立 `job_<时间>_<pid>_<uuid>`，中间帧不互踩 |
| 最终默认名都是 `<视频>_chat.mp4` | **已处理** | 若输出目录已有同名成片，会改发为 `<视频>_chat__job_xxx.mp4`，避免后写覆盖先写 |
| 各用各的 `--workdir` / `--output` | **最推荐** | 完全隔离，路径最好认 |
| `--no-job-dir` 共用 out-dir | **不安全** | 共享 `overlay_frames`，后写覆盖；仅兼容旧行为 |
| 运行中 `--clean` | **安全** | 默认只删 `*.partial.mp4`；`--clean-all` 清已结束/stale running job（仍 skip 存活 pid） |

```powershell
# 推荐：两个片子并行，各自 workdir + output
python scripts\render_cn_chat.py a.mp4 a.html --render-original --workdir work\a --output out\a_chat.mp4
python scripts\render_cn_chat.py b.mp4 b.html --render-original --workdir work\b --output out\b_chat.mp4
```

底层引擎请勿对同一 `--out-dir` 加 `--no-job-dir` 并行。

---

## 这个工具解决什么问题

你有一段 Twitch 直播录像（比如游戏反应视频），想给观众做中文字幕。但 Twitch 聊天弹幕不是字幕，它包含：

- 主播和观众的英文发言
- Twitch 官方表情（emote）和第三方表情（BTTV/FFZ/7TV）
- 频道梗、缩写、俚语
- 用户名、@提及、链接

这个工具把以上内容整体处理成一个画面左下角（可调位置）的聊天框，翻译成中文后叠加到视频上。表情图片保留原样，翻译文本替换英文。

## 完整工作流

```
  素材获取（二选一）
    A) TwitchDownloader GUI 导出视频 + HTML
    B) 本工具可选增强（需 TwitchDownloaderCLI）：
       python scripts\render_cn_chat.py --download <vod-or-clip-url>
       或 run.bat → [3] 下载素材并继续
        │
        ▼
  ┌─────────────────────────────┐
  │ 1. 解析 HTML                 │  提取消息、用户名、表情图片
  │ 2. 导出待翻译 JSON           │  每条消息一个 index + 原文
  │ 3. 翻译（可选人工复核）       │  OpenAI 兼容 API 批量翻译
  │ 4. 翻译质检（lint）          │  检查空翻译、丢 token、过长等
  │ 5. 人工复核 XLSX（可选）      │  Excel 里改翻译，回写 JSON
  │ 6. 渲染弹幕帧序列            │  Pillow 逐帧画 PNG
  │ 7. FFmpeg 合成视频           │  叠加到源视频，输出 MP4
  └─────────────────────────────┘
        │
        ▼
  带中文弹幕的 MP4 视频
```

### 可选：CLI 下载素材（免 GUI）

1. 安装 [TwitchDownloaderCLI](https://github.com/lay295/TwitchDownloader/releases)（可选增强）：  
   - **自动**：`python scripts\render_cn_chat.py --offer-td-cli`（确认后从 GitHub 下对应平台 zip 到 程序显示的可信工具目录；`--yes` 跳过确认）
   - **install 结束时**：会询问是否安装（默认否）  
   - **手动**：解压 CLI zip 到 程序显示的可信工具目录，或设 `TWITCHDOWNLOADER_CLI` / PATH

> 安全说明：源码检出使用项目内 tools；安装版使用用户数据目录。程序不会从当前素材目录的 tools 加载可执行文件。

2. 下载 VOD/聊天（聊天**必须**嵌入表情，工具已固定加 `-E`）：

```powershell
python scripts\render_cn_chat.py --download https://www.twitch.tv/videos/123456789 --download-only
# 可选裁切（仅 VOD）:
python scripts\render_cn_chat.py --download https://www.twitch.tv/videos/123456789 --begin 0:01:00 --end 0:05:00
# 同一 VOD 多段裁切 → 自动拼接视频并合并聊天（成片时间轴从 0 连续）:
python scripts\render_cn_chat.py --download https://www.twitch.tv/videos/123456789 `
  --segment 0:10:00-0:12:30 --segment 0:40:00-0:43:00 --download-only
```

**多段裁切 + 切段 + CFR 帧率**（合并后切除不需要的时间段并强制帧率）:

```powershell
python scripts/render_cn_chat.py --download https://www.twitch.tv/videos/123456789 `
  --segment 1:21:13-1:38:06 --segment 1:42:05-2:17:43 `
  --cut 21:01-22:59 --download-output-fps 60 --download-encoder auto --download-only
```

也可在 `run.bat` → `[3] 下载素材并继续` 里选 **裁切模式 [2] 多段裁切拼接**，按行输入多段 `起点 终点`。

### 下载后的媒体健康门禁

下载段、合并视频和最终 chat 成片都会在发布前做健康检查。下载段或合并视频检查失败时会停止后续流程；最终成片检查失败时会停止发布并保留 partial 文件供排查，不会把异常媒体当作成功成品。

- `--download-trim-mode Safe`：默认且推荐。Safe 可规避 TwitchDownloader `Exact` 裁切常见的时间戳偏移；只有明确需要精确裁点时才改为 `Exact`。
- `--media-check fast`：默认，检查流结构、时长、起点、帧率、无关轨道和异常长 AAC 包；`decode` 额外完整解码，适合重要长片验收；`off` 仅用于排障，不建议日常使用。
- `--media-repair audio`：**默认开启**；下载段或合并视频的健康门禁失败时自动尝试非破坏性修复，修复并复检通过后自动继续。它会生成同目录 `*.repaired.mp4`，保留原下载文件，并复用 `--download-encoder auto|qsv|nvenc|amf|x264` 的统一编码选择。最终 chat 成片门禁只负责阻止发布，不自动修复；需要排障或只检测不修复下载媒体时可显式传 `--media-repair off`。

```powershell
python scripts/render_cn_chat.py --download <VOD_URL> --download-only `
  --download-trim-mode Safe --media-check decode --media-repair audio
```

> 健康检查不负责 ASS/SRT/Aegisub；它只确保本工具的下载、拼接和 overlay 输入/输出能安全进入下一阶段。

3. 交互模式下下载完会进入下一步菜单（预览 / 手翻 / 翻译）；`--download-only` 或 `--yes` 只打印路径。

## 安装方式

### 方式 A：直接跑仓库脚本（推荐日常开发）

```powershell
pip install -r requirements.txt
python scripts\render_cn_chat.py --doctor
```

### 方式 B：可编辑 / 打包安装

```powershell
pip install -e .
# 然后可直接用入口命令：
twitch-chat-overlay --doctor
```

console scripts 定义在 `pyproject.toml`：`twitch-chat-overlay` / `twitch-chat-burn` / `twitch-chat-translate`。

打包说明：

- 代码以 `scripts/` 下的 flat `py-modules` 形式安装（不改成 package）。
- 公开 `profiles/*.yaml`、`configs/rules.example.yaml`、完整 `jobs/example_job.yaml` 与 `.env.example` 会通过 setuptools `data-files` 打进 wheel/sdist（安装后通常在 `share/twitch-chat-translator-overlay/`）。
- 资源解析优先使用显式路径、当前目录和源码仓库；wheel 安装后会自动搜索解释器、用户安装目录及控制台入口前缀下的 `share/`。布局/编码短名、`--profile` 和 `--rules` 都支持该搜索顺序。
- `.env` 会按「当前工作目录 → 仓库根 → 模块旁」顺序加载（不覆盖已有环境变量）。

## 更新与历史迁移

- Git 检出请使用 `update.bat`（Windows）或 `bash update.sh`（Linux/macOS）。更新器只接受 fast-forward；任何拉取失败都会停在依赖安装之前。
- GitHub ZIP / sdist 没有可拉取的 Git 历史，不能原地更新。请下载新的归档到新目录。
- 仓库在 **2026-07** 做过一次历史重写。此前创建的旧 clone 无法 fast-forward，必须迁移到 fresh clone。

旧 clone 迁移：

1. 只备份本机配置：`.env`、`jobs/*.yaml`、自定义 `profiles/*.yaml` 与 `configs/launcher.local.yaml`。
2. 在新目录重新 clone 当前仓库。
3. 将上述本机配置恢复到新 clone，再运行安装和 doctor。

不要把旧仓库历史与新 clone 合并。更新脚本不会自动执行破坏性的历史修复。

## 渲染编码预设

布局用 `--layout-preset`（几何），编码/性能用 `--render-preset`（encoder / crf / overlay-codec / 静态帧复用等）。**命令行显式参数优先覆盖 YAML。**

```powershell
python scripts\render_cn_chat.py video.mp4 chat.html `
  --render-preset profiles\render_fast.yaml `
  --layout-preset profiles\layout_compact.yaml `
  --render-original --preview-clip 10
```

公开预设：

- `profiles/render_default.yaml` — 均衡（默认取向：x264 + **vp9** overlay）
- `profiles/render_fast.yaml` — 预览/草稿（**png** overlay + veryfast）
- `profiles/render_hq.yaml` — 更高画质

## 环境要求

| 依赖 | 说明 |
|------|------|
| Python 3.10+ | 运行脚本 |
| FFmpeg + ffprobe | 必须在 PATH 中；用于视频探测和合成 |
| CJK 字体 | Windows 默认用微软雅黑 `msyh.ttc`；Linux/Mac 自动检测 Noto/WQY |
| OpenAI 兼容 API | **仅翻译时需要**；用 `--render-original` 不翻译时不需要 |

安装 Python 依赖：

```powershell
pip install -r requirements.txt
```

翻译 API 配置（复制 `.env.example` 为 `.env`）：

```ini
OPENAI_COMPAT_BASE_URL=https://api.openai.com/v1
OPENAI_COMPAT_MODEL=gpt-4o-mini
OPENAI_COMPAT_API_KEY=your_key
```

支持任何 OpenAI 兼容接口（OpenAI / DeepSeek / Moonshot / 本地 Ollama 等）。不翻译时这个文件可以不配。

## 快速开始

### 最简单的用法：不翻译，直接烧录原始英文弹幕

```powershell
python scripts\render_cn_chat.py video.mp4 chat.html --render-original --output out.mp4
```

### 翻译 + 渲染一条龙

```powershell
python scripts\render_cn_chat.py video.mp4 chat.html --output out.mp4
```

这会自动：解析 HTML → 导出 JSON → 调 API 翻译 → 渲染帧 → 合成视频。
翻译 JSON 默认存为 `<视频名>_translation.json`，下次可用 `--reuse-translation` 复用。
若目标 JSON 已有非空 `translation`，默认**不会覆盖导出**（防丢译）；强制重导用 `--force-export`。

### 分步操作（推荐，便于人工校对）

**第 1 步：导出待翻译 JSON**

```powershell
python scripts\render_cn_chat.py video.mp4 chat.html --manual-translation --translation-json translations\my_chat.json
```

**第 2 步：翻译**

```powershell
python scripts\translate_chat_openai.py translations\my_chat.json --target-language zh
```

这会原地更新同一个 JSON，不渲染视频；翻译完成后再进入复核或渲染步骤。

**第 3 步（可选）：导出人工复核表**

```powershell
python scripts\render_cn_chat.py video.mp4 chat.html --review --translation-json translations\my_chat.json --review-xlsx reviews\review.xlsx
```

在 Excel 里编辑 `translation` 列，改完回写：

```powershell
python scripts\render_cn_chat.py video.mp4 chat.html --reuse-translation --review-done --translation-json translations\my_chat.json --review-xlsx reviews\review.xlsx --output out.mp4
```

**第 4 步：渲染视频**

```powershell
python scripts\render_cn_chat.py video.mp4 chat.html --reuse-translation --translation-json translations\my_chat.json --output out.mp4
```

> **交互暂停（默认）**：在 TTY 下刚跑完 API 翻译时，会在渲染前停一下。  
> 回车继续整片；`P` / `P 30` 先渲一小段预览（有 `--workdir` 写到 `workdir/temp`，否则 `outputs/_preview`）；`S` 停在复核，稍后用 `--review-done`。  
> 脚本/CI 请加 `--yes` 跳过暂停。

### 快速预览（不渲染整片）

生成某一秒的静态预览图：

```powershell
python scripts\render_cn_chat.py video.mp4 chat.html --reuse-translation --translation-json translations\my_chat.json --preview-frame 60
```

渲染开头 10 秒短片：

```powershell
python scripts\render_cn_chat.py video.mp4 chat.html --reuse-translation --translation-json translations\my_chat.json --preview-clip 10 --output preview.mp4
```

### 用 job.yaml 一键跑（推荐日常）

把路径和模式写进 YAML，之后只需 `--job`：

```yaml
# jobs/my_vod.yaml（完整注释示例见 jobs/example_job.yaml）
video: path/to/video.mp4
chat_html: path/to/chat.html
mode: preview          # 或 full / translate / render
render_original: true  # 先对齐时间轴/布局，再关此项做翻译
# layout_preset: compact
# render_preset: fast
```

```powershell
python scripts\render_cn_chat.py --init              # 引导生成 jobs\xxx.yaml
python scripts\render_cn_chat.py --job jobs\my_vod.yaml
# CLI 显式参数优先于 YAML，例如: --mode full --output out.mp4
```

## 关键概念

### 任务配置（job.yaml）

`jobs/*.yaml` 是「一条片子」的参数快照：输入路径、`mode`、是否 `render_original` / `reuse_translation`、布局/编码短名等。一般相对路径相对 **YAML 所在目录** 解析；若 YAML 旁没有指定的 profile 或 rules，则继续从项目和已安装资源目录查找。命令行同名参数覆盖文件。`python scripts\render_cn_chat.py --init` / `run.bat new` 会生成带中文注释的模板。

### 弹幕帧率 vs 成片帧率

这是两个独立的参数，容易混淆：

| 参数 | 控制什么 | 默认值 | 典型值 |
|------|----------|--------|--------|
| `--fps` | 弹幕层每秒画多少张 PNG | 15 | 15（默认，足够流畅）、30（更细） |
| `--output-fps` | 最终 MP4 视频每秒多少帧 | 跟随源视频 | 60（源是 60fps）、30 |

**为什么要分开**：弹幕文字变化不快，15fps 画弹幕层足够流畅；但源视频可能是 60fps，成片应保持 60fps 不掉帧。把弹幕层降到 15fps 可以大幅减少渲染时间，同时成片仍是 60fps。

### 时间偏移（offset）

Twitch 聊天 HTML 里的时间戳是**直播开始后的秒数**，而视频文件是从某个时间点开始录的。两者之间有个偏移。

- **自动检测**：工具会比较 HTML 中第一条消息的时间戳和视频时长，推断偏移。
- **手动指定**：`--offset 7264` 表示聊天时间戳减去 7264 秒后对齐视频。
- **预览确认**：自动检测的偏移可能不准，用 `--preview-frame` 或 `--preview-clip` 先看几秒确认。

### overlay-codec：PNG vs VP9

弹幕帧序列叠加到视频上有两条技术路线：

| | PNG 序列 | VP9 WebM（CLI 默认） |
|---|---|---|
| 原理 | 逐帧 PNG 图片直接叠加 | 先编码成透明 WebM 视频再叠加 |
| 画质 | 像素级精确 | 有压缩损失，边缘可能有瑕疵 |
| 速度 | 通常更快（少一步编码） | 多一步 VP9 编码 |
| 磁盘 | 临时 PNG 体积更大 | 一个 WebM 文件，更省盘 |
| 使用 | `--overlay-codec png` | `--overlay-codec vp9` |

**argparse 默认是 `vp9`**（`render_default` 同向）。预览/草稿可用 `profiles/render_fast.yaml`（`overlay_codec: png`），或显式 `--overlay-codec png`。需要像素级精确、磁盘够用时再切 PNG。

### 布局预设

用 YAML 文件保存 overlay 位置、字号、透明度等参数，不用每次敲一长串命令行：

```yaml
# profiles/layout_compact.yaml
name: layout_compact
layout:
  x: 12
  y: 420
  width: 420
  height: 260
  font_size: 14
  fps: 24
  max_visible: 8
  msg_lifetime: 10.0
  bg_alpha: 200
  emote_height: 20
```

```powershell
python scripts\render_cn_chat.py video.mp4 chat.html --layout-preset profiles\layout_compact.yaml
```

命令行参数会覆盖预设文件中的同名值。

### 分辨率自适应（run.bat 默认路径）

`run.bat` / `layout_default` / `layout_mobile` 的绝对像素是按约 **1920×1080** 写的。源视频分辨率差很多（常见 360p/720p 下载切片）且**没有**设置 `*-ratio`、框又大部分在画面外时，引擎会**自动按源分辨率缩放** x/y/w/h/字号/表情，并打 `[INFO] 已按源分辨率自适应布局 …`。

- **会自适应**：默认布局、wizard 生成的 job、只改了输出路径没改坐标的情况  
- **不覆盖**：已设任意 `*-ratio`；或绝对框已经完全落在源画面内的自定义裁切  
- **仍可手调**：需要精确构图时继续用像素参数或下面的比例参数

同时，默认 `--max-visible 0` 会按当前框高/字号自动填满；若显式 N 大于可容纳行数，会自动钳制，避免矮框上弹幕叠在顶部。

### 比例布局参数

当你想**显式**用一套参数适配多种分辨率时，用比例参数代替固定像素：

| 参数 | 换算方式 | 示例 |
|------|----------|------|
| `--x-ratio` | X 坐标 = 源视频宽度 × ratio | `--x-ratio 0.01` → 1920×0.01=19px |
| `--y-ratio` | Y 坐标 = 源视频高度 × ratio | `--y-ratio 0.30` → 1080×0.30=324px |
| `--width-ratio` | 弹幕框宽 = 源视频宽度 × ratio | `--width-ratio 0.26` → 1920×0.26=499px |
| `--height-ratio` | 弹幕框高 = 源视频高度 × ratio | `--height-ratio 0.34` → 1080×0.34=367px |
| `--font-size-ratio` | 字号 = 源视频高度 × ratio | `--font-size-ratio 0.014` → 1080×0.014=15px |

**规则**：ratio > 0 时覆盖对应像素值；ratio = 0 时回退到 `--x`/`--y`/`--w`/`--h`/`--font-size`。设置 `--font-size-ratio` 时，`--emote-height` 也会按字号比例自动调整。有 ratio 时**不再**做上面的绝对布局自适应。

**典型场景**：720p 素材想复用 1080p 的布局参数——把固定像素换成 ratio，同一套预设就能适配两种分辨率。

### 手机阅读模式

`profiles/layout_mobile.yaml` 是专门的手机观看预设，核心思路：**同一块电脑弹幕区域，用浮动堆叠 + 限流让消息更易读**。

**与默认布局的区别：**

| 行为 | `layout_default` | `layout_mobile` |
|------|-------------------|-----------------|
| 区域 | `x=15, y=327, 497×363` | **相同**（同一块画布） |
| 堆叠方式 | `lanes`（按时间过期沉积） | `float`（Twitch 上浮，仅容量顶出，无时间过期） |
| 最大条数 | 0（按框高/字号自动填满） | 0（同上；另用 float 上浮 + 限流） |
| 每条最多行数 | 不限 | 2 行（超出显示 `...`） |
| 入场限流 | 无 | 0.35 秒间隔（高频聊天时延后新消息） |
| 弹幕层帧率 | 30fps | 15fps（省渲染时间） |
| 成片帧率 | 跟随源视频 | 跟随源视频（不受弹幕层 15fps 影响） |

```powershell
python scripts\render_cn_chat.py video.mp4 chat.html `
  --reuse-translation --translation-json translations\my_chat.json `
  --layout-preset profiles\layout_mobile.yaml `
  --preview-clip 15 --output mobile_preview.mp4
```

**注意事项：**
- 该预设的绝对坐标按 1080p 编写；非 1080p 源在**未设 ratio 且框越界**时会自动缩放（见上文「分辨率自适应」）。需要精确构图时仍可手改 `x/y/w/h/font_size` 或改用比例参数。
- 高频聊天中，`arrival_interval` 会延后新消息；到达视频末尾仍未上屏的排队消息不会补播。
- 若需要固定条数或最短可见保护，用 `--max-visible N` 或 `--stack-mode lanes --min-visible-seconds ...` 覆盖预设。

### 翻译规则文件

YAML 格式，用于频道梗、专名、数字梗的归一化。API 翻译完成后，工具按 `original`
精确匹配规则并覆写对应的 `translation`；`preserve_patterns` 只跳过这一步规则清洗，
不控制消息是否调用翻译 API：

```yaml
# configs/rules.example.yaml
normalizations:
  - name: "channel meme"
    match: ["GG", "gg", "good game"]
    translation: "打得好"

preserve_patterns:
  - "^\\[[^\\]]+\\]$"        # 纯表情消息跳过规则覆写
  - "^@[A-Za-z0-9_]+"        # @用户名消息跳过规则覆写
```

### 翻译质检（lint）

渲染前检查翻译 JSON 的常见问题：

```powershell
python scripts\render_cn_chat.py --lint-translation translations\my_chat.json
```

检查项：空翻译、emote 方括号 token 丢失、@用户名丢失、URL 丢失、翻译过长。

### 翻译 profile

控制翻译风格、术语表、保留项：

```yaml
# profiles/default.yaml
name: default
context: "Twitch livestream chat. Keep emote names, @usernames, URLs concise."
glossary:
  GG: "打得好"
preserve:
  - emotes
  - usernames
  - urls
translation_style:
  tone: "口语化、简洁"
  max_length_hint: "尽量短句"
```

## 命令行参数速查

> ⭐ 标记的参数最常用，新手先看这些。

### 弹幕层 — 常用参数

| 参数 | 默认 | 说明 |
|------|------|------|
| ⭐ `--fps` | 15 | 弹幕层采样帧率（只影响聊天画面，不影响成片）。15 默认足够流畅；需要更细可 `--fps 30` |
| ⭐ `--x` / `--y` | 15 / 327 | 弹幕框左上角坐标 |
| ⭐ `--w` / `--h` | 497 / 363 | 弹幕框宽高 |
| ⭐ `--font-size` | 15 | 字体大小 |
| ⭐ `--bg-alpha` | 255 | 背景透明度（0=透明，255=不透明） |
| ⭐ `--max-visible` | 0 | 最多同时显示几条消息；**默认 0=按框高/字号自动填满**；显式 N 固定条数；若 N 大于可容纳行数会自动钳制并告警 |
| ⭐ `--stack-mode` | lanes | `lanes`=按时间过期沉积（默认），`float`=Twitch 上浮（仅容量顶出，`layout_mobile` 使用） |

### 弹幕层 — 进阶参数

| 参数 | 默认 | 说明 |
|------|------|------|
| `--font-path` | auto | 字体文件（auto 自动检测 CJK 字体） |
| `--x-ratio` / `--y-ratio` | 0 | 按源视频宽/高换算 X/Y；大于 0 时覆盖对应像素坐标 |
| `--width-ratio` / `--height-ratio` | 0 | 按源视频宽/高换算弹幕框宽/高；大于 0 时覆盖对应像素尺寸 |
| `--font-size-ratio` | 0 | 按源视频高度换算字号；大于 0 时也会按字号同步表情高度 |
| `--msg-lifetime` | 14.0 | 仅 `stack_mode=lanes`；**float 上浮模式忽略** |
| `--max-message-lines` | 0 | 单条消息最多显示行数；0 不额外限制 |
| `--min-visible-seconds` | 0.0 | 仅 `stack_mode=lanes`：已上屏最短可见秒数；**float 忽略**；0 允许立即顶替 |
| `--arrival-interval` | 0.0 | 新消息最小入场间隔秒数；0 不限流 |
| `--emote-height` | 22 | 表情图片高度像素 |

### 成片输出

| 参数 | 默认 | 说明 |
|------|------|------|
| ⭐ `--output` | `<视频名>_chat.mp4` | 最终输出路径；不带时默认存到源视频同目录 |
| ⭐ `--encoder` | x264 | 编码器：x264 / nvenc / qsv / amf / auto |
| ⭐ `--crf` | 18 | 质量（越小越好，18 是甜点） |
| `--output-fps` | 跟随源 | 强制成片帧率 |
| `--video-preset` | 编码器默认 | x264: ultrafast~veryslow；nvenc: p1~p7 |
| `--audio-codec` | aac | 音频编码：aac（重编码）或 copy（直接拷贝） |
| `--overlay-codec` | vp9 | 弹幕层中间格式：`vp9`（默认）或 `png`（`render_fast` 使用） |
| `--render-preset <yaml>` | — | 编码/性能预设（encoder/crf/overlay-codec 等；CLI 覆盖 YAML） |

### 流程控制（`render_cn_chat` pipeline）

| 参数 | 说明 |
|------|------|
| ⭐ `--translation-json <path>` | 翻译 JSON 路径（默认 `<视频名>_translation.json`） |
| ⭐ `--reuse-translation` | 复用已有翻译 JSON（不再调 API） |
| `--force-export` | 允许覆盖已有非空 translation 的 JSON（默认拒绝，防丢译） |
| `--strict-import` | 导入翻译渲染时身份不一致则硬失败（转发给 burn；默认跳过错配） |
| ⭐ `--preview-clip <s>` | 渲染 N 秒短片；默认从 0 秒开始 |
| ⭐ `--preview-frame <s>` | 只生成第 N 秒的预览图 |
| ⭐ `--render-original` | 不翻译，用原始英文渲染 |
| ⭐ `--layout-preset <yaml>` | 渲染布局预设 |
| ⭐ `--render-preset <yaml>` | 编码/性能预设 |
| `--preview-dense` | 与 `--preview-clip` 联用：自动选弹幕最密时间窗，并从对应源视频位置截取 |
| `--skip-translate` | 只导出翻译 JSON，不调用翻译和渲染 |
| `--manual-translation` | 不调用 LLM；导出待翻译 JSON + 人工复核表后停止 |
| `--review` | 导出人工复核 XLSX/TSV |
| `--review-done` | 从 XLSX/TSV 回写翻译后渲染 |
| `--lint-translation` | 翻译质检 |
| `--rules <yaml>` | 应用翻译规则文件 |
| `--profile <yaml>` | 翻译 profile |
| `--doctor` | 环境诊断 |
| `--workdir <dir>` | pipeline 工作目录（中间文件进 `workdir/temp`，默认输出归档到此） |
| `--clean` | 清理临时文件后退出：默认只删 `*.partial.mp4`；加 `--clean-all` 才删已结束/stale running 的 `job_`/`batch_`；`--clean-progress` 才删进度文件 |
| `--clean-all` | 与 `--clean` 联用：删除 workdir/out 下工具 job 目录（跳过仍存活 pid 的 running） |
| `--keep-temp` | 保留中间文件 |
| `--lazy-message-images` | 长片省内存：消息图按需渲染 + LRU 缓存 |

### 底层引擎专用（`twitch_chat_burn`，pipeline 内部会调用）

这些参数**只在** `twitch_chat_burn` / `twitch-chat-burn` 上直接暴露；日常请用上面的 pipeline 开关：

| 参数 | 说明 |
|------|------|
| `--export-translation <path>` | 只导出待翻译 JSON（stream 时间戳；已有译文默认拒绝覆盖） |
| `--force-export` | 与 export 联用：强制覆盖已有非空 translation |
| `--import-translation <path>` | 导入翻译 JSON 并渲染 |
| `--strict-import` | 导入时 author/timestamp/original 不一致硬失败（pipeline 同名开关会转发） |
| `--job-dir <dir>` | 本次运行独立 job 目录（默认在 `--out-dir` 下自动创建） |
| `--out-dir <dir>` | 中间文件 / 默认输出目录 |
| `--clean` | 清理 `--out-dir` 临时文件后退出；默认 partials only；`--clean-all` 清已结束/stale running job；或 `--job-dir` 只清一个 |
| `--clean-all` | 与 `--clean` 联用：删除工具 `job_`/`batch_` 目录（跳过存活 pid 的 running） |

## 运行环境诊断

```powershell
# 基础诊断
python scripts\render_cn_chat.py --doctor

# 带输入文件诊断
python scripts\render_cn_chat.py video.mp4 chat.html --doctor
```

会检查：FFmpeg/ffprobe 是否可用、字体能否加载、视频能否读取、HTML 能否解析、翻译 API 是否配置。

## 运行测试

```powershell
# 安装开发依赖
pip install -r requirements-dev.txt

# 默认：单元 + smoke（有 FFmpeg 时）；不含 max/slow 长套件；默认不跑 ruff
python scripts\run_tests.py --install-dev

# 只跑单元测试（不需要 FFmpeg）
python scripts\run_tests.py --unit-only

# 代码规范（Ruff；配置见 pyproject.toml；CI 单独一步）
python scripts\run_tests.py --lint

# 全面 max 套件（发版前 / 大改后 / 长期回归：含 lint）
python scripts\run_tests.py --max

# 直接用 pytest
python -m pytest tests/ -v
python -m pytest tests/ -v -m "not max and not slow"
python -m pytest tests/ -v -m max
```

维护者交接与正确性契约见 [MAINTAINERS.md](MAINTAINERS.md)（英文摘要 README.en.md 已同步近期安全/下载行为）。

## 项目结构

```
scripts/
  render_cn_chat.py           # 主入口：一键 pipeline（解析→翻译→渲染→合成）
  twitch_chat_burn.py          # 核心引擎：HTML 解析、Pillow 渲染、FFmpeg 合成
  chat_parser.py               # HTML 解析（从 burn 中抽出，burn re-export）
  translate_chat_openai.py     # OpenAI 兼容翻译器
  chat_window.py               # 时间偏移计算、预览窗口过滤
  overlay_config.py            # Overlay 配置数据类
  encode_options.py            # 编码器选择和参数构建
  layout_preset.py             # YAML 布局预设加载
  render_perf.py               # 帧复用、空白跳过等性能优化
  process_util.py              # 子进程管理、job 目录
  run_meta.py                  # 运行元数据
  translation_support.py       # 翻译错误分类、退避、磁盘缓存
  common_utils.py              # 字体检测、颜色转换、参数校验
  run_tests.py                 # 测试入口脚本
configs/
  rules.example.yaml           # 翻译规则示例
profiles/
  default.yaml                 # 翻译 profile 示例
  layout_default.yaml          # 默认布局预设
  layout_compact.yaml          # 紧凑布局预设
  layout_mobile.yaml           # 手机阅读模式预设
  render_default.yaml          # 默认编码预设
  render_fast.yaml             # 快速预览编码预设
  render_hq.yaml               # 高画质编码预设
translations/                  # 翻译 JSON 文件
reviews/                       # 人工复核表
outputs/                       # 生成视频（.gitignore）
tests/                         # 测试（含 smoke / max / concurrent / UX）
  fixtures/                    # HTML 变体、翻译 JSON 等测试夹具
  test_max_*.py                # 全面套件（run_tests.py --max）
  test_*.py                    # 测试模块（500+ 用例；以当前 CI / pytest 输出为准）
requirements.txt               # 运行依赖
requirements-dev.txt           # 开发/测试依赖
.env.example                   # 翻译 API 配置模板
pyproject.toml                 # 打包 / console scripts / 公开 YAML data-files
MANIFEST.in                    # sdist 公开资源白名单
```

## 常见问题

**Q: 自动检测的 offset 不准怎么办？**
A: 用 `--preview-frame 0` 看第一秒画面，如果弹幕位置不对就手动调 `--offset`。也可以 `--preview-clip 10` 渲染 10 秒短片确认。

**Q: 翻译 API 报限速（429）怎么办？**
A: 减小 `--batch-size`（默认 10）和 `--workers`（默认 4）。翻译脚本内置指数退避，但严重限速时仍需降速。

**Q: 渲染很慢怎么办？**
A: 降低 `--fps`（15 足够流畅）；试 `--render-preset profiles\render_fast.yaml` 或 `--overlay-codec png` + `--video-preset veryfast`；有 GPU 时试 `--encoder qsv` 或 `--encoder nvenc`。

**Q: 源视频音画不同步（视频比音频晚约 1 秒）怎么办？**
A: 工具会自动检测并处理（首帧冻结，双轨从 0 开始），不需要手动干预。成片时长约等于源时长。

**Q: 想保留英文原文不翻译呢？**
A: 用 `--render-original`，直接用原始聊天内容渲染 overlay。

**Q: 输出文件在哪？**
A: 不带 `--output` 时，默认输出到源视频同目录下 `<视频名>_chat.mp4`。带 `--workdir` 时输出归档到 workdir 下。如果同目录已有旧成品，会自动备份为 `.bak`（用 `--no-backup-prev` 禁用）。

## License

MIT License. See [LICENSE](LICENSE).
