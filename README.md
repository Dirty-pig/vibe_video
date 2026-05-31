# vibe_video

一个把语音输入变成可执行 Prompt 的实验项目。

这个仓库包含两部分：

- 一组本地 Python 脚本，用于录音、语音转写、Prompt 编译、文件转写
- 一个 VS Code 扩展，用于在编辑器里直接录音、生成 Prompt、复制结果

核心流程是：

1. 从麦克风或音视频文件拿到中文语音
2. 用 faster-whisper 做转写
3. 把转写结果交给 DeepSeek 做结构化 Prompt 编译
4. 输出 compiled prompt、agent payload，以及可在 VS Code 中直接使用的结果

## Features

- 麦克风实时录音并转写
- 将自然语言口述编译成结构化 Prompt
- 支持多种模式路由，例如 feature、debug、refactor、plan、research、writing
- 保存最近一次编译产物到 `prompt_outputs/`
- 支持音视频文件离线转写
- 提供一个可直接在 VS Code 中使用的侧边栏扩展

## Repo Layout

```text
vibe_video/
|- mic_stream_stt.py                # 麦克风录音 + faster-whisper 转写
|- mic_prompt_deepseek.py          # 录音/转写 + DeepSeek Prompt 编译
|- mic_prompt_service.py           # 供 VS Code 扩展调用的 Python 后端
|- translate_audio.py              # 音频/视频文件转写脚本
|- prompt_compiler/                # Prompt 编译模板、模式配置、输出 schema
|- prompt_outputs/                 # 最近一次输出和运行记录
|- transcripts_cache/              # 录音缓存
|- mic_transcripts.md              # 录音转写归档
|- vscode_extension/               # VS Code 扩展
```

## Environment

当前代码默认偏向下面这套环境：

- Windows
- Python 3.10+
- NVIDIA GPU + CUDA
- Node.js 20+，用于构建 VS Code 扩展

说明：仓库里的转写代码当前直接写死了 `device="cuda"`，所以如果机器没有 CUDA，脚本会直接报错，而不是自动回退到 CPU。

## Python Dependencies

仓库里目前没有 `requirements.txt`，按现有代码至少需要这些 Python 依赖：

```bash
pip install numpy sounddevice faster-whisper openai
```

另外通常还需要：

- 本地可用的 FFmpeg
- 正常工作的 CUDA / cuDNN 环境

## DeepSeek API Key

推荐通过环境变量配置：

```powershell
$env:DEEPSEEK_API_KEY="your_deepseek_key"
```

或者长期写入用户环境变量：

```powershell
[Environment]::SetEnvironmentVariable("DEEPSEEK_API_KEY", "your_deepseek_key", "User")
```

说明：代码里目前也保留了硬编码兜底值。对于公开仓库，更建议只保留环境变量方案。

## Quick Start

### 1. 麦克风转写

把麦克风语音转成文本，并追加到 `mic_transcripts.md`：

```bash
python mic_stream_stt.py
```

常用参数：

```bash
python mic_stream_stt.py --list-devices
python mic_stream_stt.py --mic-device 1
python mic_stream_stt.py --sample-rate 16000
```

### 2. 麦克风转写并编译 Prompt

这是仓库里最核心的命令行入口：

```bash
python mic_prompt_deepseek.py
```

运行后会完成：

- 麦克风录音
- faster-whisper 转写
- DeepSeek 结构化编译
- 输出最近一次结果到 `prompt_outputs/latest_agent_payload.json`
- 追加运行记录到 `prompt_outputs/prompt_runs.md`

常用参数：

```bash
python mic_prompt_deepseek.py --list-devices
python mic_prompt_deepseek.py --mic-device 1
python mic_prompt_deepseek.py --context "这是一个 VS Code 扩展项目"
python mic_prompt_deepseek.py --print-payload-only
```

### 3. 音视频文件转写

把本地音频或视频文件转成文本：

```bash
python translate_audio.py path/to/file.mp4
```

当前支持的扩展名包括：

`wav`, `mp3`, `m4a`, `flac`, `aac`, `ogg`, `opus`, `wma`, `mp4`, `mkv`, `mov`, `webm`

## VS Code Extension

`vscode_extension/` 目录里包含一个本地扩展，主要能力是：

- 在侧边栏里显示 Voice Prompt 面板
- 直接开始/停止录音
- 调用 Python 后端生成 Prompt
- 自动复制编译后的 Prompt 到剪贴板

### Build

```bash
cd vscode_extension
npm install
npm run compile
```

### Run in VS Code Extension Host

1. 用 VS Code 打开 `vscode_extension/`
2. 执行 `npm install`
3. 执行 `npm run compile`
4. 按 `F5` 启动 Extension Development Host

### Extension Config

扩展目前暴露了两个主要配置项：

- `voicePrompt.backendCommand`: 自定义 Python 后端启动命令
- `voicePrompt.autoCopyToClipboard`: 录音结束后是否自动复制 Prompt

默认快捷键：

```text
Ctrl+Alt+R
```

## Outputs

运行过程中常见输出位置：

- `mic_transcripts.md`: 麦克风转写文本归档
- `prompt_outputs/latest_agent_payload.json`: 最近一次编译结果
- `prompt_outputs/prompt_runs.md`: Prompt 编译运行记录
- `transcripts_cache/`: 临时 WAV 缓存

## Notes

- 当前实现明显偏向中文普通话场景
- 当前实现默认使用 GPU 推理，不是 CPU 友好配置
- Python 根目录和 `vscode_extension/backend/` 下存在一份几乎相同的后端代码
- 如果你准备继续公开维护这个仓库，建议后续补上 `requirements.txt`、安装说明，以及更明确的模型和显卡要求
