import * as vscode from 'vscode';
import { ChildProcessWithoutNullStreams, spawn } from 'child_process';
import * as fs from 'fs';
import * as path from 'path';


type BackendEvent = {
  type: 'event';
  event: string;
  payload?: Record<string, unknown>;
};

type BackendResponse = {
  type: 'response';
  id: number;
  ok: boolean;
  payload?: Record<string, unknown>;
  error?: string;
};

type BackendMessage = BackendEvent | BackendResponse;

type PromptResult = {
  raw_text: string;
  compiled_prompt: string;
  agent_input: {
    prompt?: string;
  };
  mode: string;
  confidence: number;
  language: string;
  language_probability: number;
  record_seconds: number;
  infer_seconds: number;
  api_seconds: number;
  dropped_chunks: number;
  stop_reason: string;
  message?: string;
};

type WebviewMessage = {
  command?: 'toggleRecording' | 'copyPrompt' | 'insertPrompt';
};

class VoicePromptController {
  private context: vscode.ExtensionContext;
  private statusBar: vscode.StatusBarItem;
  private panel: vscode.WebviewPanel | undefined;
  private sidebarView: vscode.WebviewView | undefined;
  private process: ChildProcessWithoutNullStreams | undefined;
  private nextRequestId = 1;
  private pending = new Map<number, { resolve: (value: Record<string, unknown>) => void; reject: (reason?: unknown) => void }>();
  private stdoutBuffer = '';
  private isBackendReady = false;
  private isRecording = false;
  private lastRawText = '';
  private lastCompiledPrompt = '';
  private lastError = '';

  constructor(context: vscode.ExtensionContext) {
    this.context = context;
    this.statusBar = vscode.window.createStatusBarItem(vscode.StatusBarAlignment.Left, 100);
    this.statusBar.command = 'voicePrompt.toggleRecording';
    this.statusBar.tooltip = 'Voice Prompt';
    this.statusBar.show();
    this.updateStatusBar();
  }

  public async initialize(): Promise<void> {
    try {
      await this.startBackend();
    } catch (error) {
      this.lastError = String(error);
      this.render();
      void vscode.window.showErrorMessage(`Voice Prompt backend failed to start: ${String(error)}`);
    }
  }

  public async dispose(): Promise<void> {
    try {
      if (this.process && !this.process.killed) {
        await this.sendRequest('shutdown', {});
      }
    } catch {
      // Ignore shutdown failures during dispose.
    }
    this.process?.kill();
    this.statusBar.dispose();
    this.panel?.dispose();
  }

  public async openPanel(): Promise<void> {
    if (this.panel) {
      this.panel.reveal(vscode.ViewColumn.Beside);
      this.render();
      return;
    }

    this.panel = vscode.window.createWebviewPanel(
      'voicePromptPanel',
      'Voice Prompt',
      vscode.ViewColumn.Beside,
      { enableScripts: true }
    );

    this.panel.onDidDispose(() => {
      this.panel = undefined;
    }, undefined, this.context.subscriptions);

    this.panel.webview.options = { enableScripts: true };
    this.attachWebviewMessageHandler(this.panel.webview);

    this.render();
  }

  public resolveWebviewView(webviewView: vscode.WebviewView): void {
    this.sidebarView = webviewView;
    this.sidebarView.webview.options = { enableScripts: true };
    this.attachWebviewMessageHandler(this.sidebarView.webview);
    this.sidebarView.onDidDispose(() => {
      this.sidebarView = undefined;
    });
    this.render();
  }

  public async copyPromptCommand(): Promise<void> {
    await this.copyPromptToClipboard();
  }

  public async toggleRecording(): Promise<void> {
    await this.startBackend();
    await this.openPanel();

    if (!this.isRecording) {
      await this.startRecording();
      return;
    }

    await this.stopRecording();
  }

  private findBundledBackendPython(): string | undefined {
    const userProfile = process.env.USERPROFILE || '';
    const candidates = [
      path.join(userProfile, 'miniconda3', 'envs', 'yolo', 'python.exe'),
      path.join(userProfile, 'anaconda3', 'envs', 'yolo', 'python.exe'),
      path.join(userProfile, 'miniforge3', 'envs', 'yolo', 'python.exe'),
    ];

    return candidates.find((candidate) => fs.existsSync(candidate));
  }

  private getBundledBackendRoot(): string {
    return path.join(this.context.extensionPath, 'backend');
  }

  private resolveBackendLaunch(backendRoot: string): { command: string; args: string[]; shell: boolean } {
    const config = vscode.workspace.getConfiguration('voicePrompt');
    const configuredCommand = (config.get<string>('backendCommand') || '').trim();
    const backendScript = path.join(backendRoot, 'mic_prompt_service.py');

    if (configuredCommand) {
      return {
        command: configuredCommand,
        args: [],
        shell: true,
      };
    }

    const detectedPython = this.findBundledBackendPython();
    if (detectedPython) {
      return {
        command: detectedPython,
        args: ['-u', backendScript],
        shell: false,
      };
    }

    return {
      command: 'conda',
      args: ['run', '-n', 'yolo', 'python', '-u', backendScript],
      shell: false,
    };
  }

  private async startBackend(): Promise<void> {
    if (this.process && this.isBackendReady) {
      return;
    }

    const backendRoot = this.getBundledBackendRoot();
    const backendLaunch = this.resolveBackendLaunch(backendRoot);

    if (!fs.existsSync(path.join(backendRoot, 'mic_prompt_service.py'))) {
      throw new Error(`Bundled backend not found: ${backendRoot}`);
    }

    this.isBackendReady = false;
    this.lastError = '';
    this.updateStatusBar();
    this.render();

    const backendEnv: NodeJS.ProcessEnv = {
      ...process.env,
      PYTHONIOENCODING: 'utf-8',
      PYTHONUTF8: '1',
    };

    this.process = spawn(backendLaunch.command, backendLaunch.args, {
      cwd: backendRoot,
      shell: backendLaunch.shell,
      env: backendEnv,
    });

    this.process.on('error', (error: Error) => {
      this.lastError = `Backend process error: ${String(error)}`;
      this.render();
    });

    this.process.stderr.on('data', (chunk: Buffer) => {
      const text = chunk.toString('utf8');
      const nextError = text.trim();
      this.lastError = this.lastError ? `${this.lastError}\n${nextError}` : nextError;
      this.render();
    });

    this.process.on('exit', (code: number | null) => {
      this.isBackendReady = false;
      this.isRecording = false;
      if (code !== 0 && code !== null) {
        this.lastError = this.lastError
          ? `${this.lastError}\nBackend exited with code ${code}`
          : `Backend exited with code ${code}`;
      }
      this.updateStatusBar();
      this.render();
      this.process = undefined;
    });

    this.process.stdout.on('data', (chunk: Buffer) => {
      this.stdoutBuffer += chunk.toString('utf8');
      let newlineIndex = this.stdoutBuffer.indexOf('\n');
      while (newlineIndex !== -1) {
        const line = this.stdoutBuffer.slice(0, newlineIndex).trim();
        this.stdoutBuffer = this.stdoutBuffer.slice(newlineIndex + 1);
        if (line) {
          this.handleBackendLine(line);
        }
        newlineIndex = this.stdoutBuffer.indexOf('\n');
      }
    });

    await new Promise<void>((resolve, reject) => {
      const timer = setTimeout(() => {
        clearInterval(interval);
        reject(new Error('Timed out waiting for backend readiness.'));
      }, 60000);

      const interval = setInterval(() => {
        if (!this.process) {
          clearInterval(interval);
          clearTimeout(timer);
          reject(new Error(this.lastError || 'Backend exited before becoming ready.'));
          return;
        }

        if (this.isBackendReady) {
          clearInterval(interval);
          clearTimeout(timer);
          resolve();
        }
      }, 100);
    });
  }

  private handleBackendLine(line: string): void {
    let message: BackendMessage;
    try {
      message = JSON.parse(line) as BackendMessage;
    } catch {
      this.lastError = line;
      this.render();
      return;
    }

    if (message.type === 'event') {
      if (message.event === 'ready') {
        this.isBackendReady = true;
        this.lastError = '';
      }
      if (message.event === 'recording_state') {
        this.isRecording = Boolean(message.payload?.recording);
      }
      this.updateStatusBar();
      this.render();
      return;
    }

    const pending = this.pending.get(message.id);
    if (!pending) {
      return;
    }
    this.pending.delete(message.id);
    if (message.ok) {
      pending.resolve(message.payload || {});
    } else {
      pending.reject(new Error(message.error || 'Unknown backend error'));
    }
  }

  private attachWebviewMessageHandler(webview: vscode.Webview): void {
    webview.onDidReceiveMessage(async (message: WebviewMessage) => {
      if (message.command === 'toggleRecording') {
        await this.toggleRecording();
      }
      if (message.command === 'copyPrompt') {
        await this.copyPromptToClipboard();
      }
      if (message.command === 'insertPrompt') {
        await this.insertPromptAtCursor();
      }
    }, undefined, this.context.subscriptions);
  }

  private sendRequest(command: string, payload: Record<string, unknown>): Promise<Record<string, unknown>> {
    if (!this.process || !this.process.stdin.writable) {
      return Promise.reject(new Error('Backend process is not available.'));
    }

    const id = this.nextRequestId++;
    const request = JSON.stringify({ id, command, payload });
    this.process.stdin.write(`${request}\n`);

    return new Promise((resolve, reject) => {
      this.pending.set(id, { resolve, reject });
    });
  }

  private async startRecording(): Promise<void> {
    try {
      await this.sendRequest('start_recording', {});
      this.isRecording = true;
      this.lastError = '';
      this.updateStatusBar();
      this.render();
    } catch (error) {
      this.lastError = String(error);
      this.render();
      void vscode.window.showErrorMessage(`Failed to start recording: ${String(error)}`);
    }
  }

  private async stopRecording(): Promise<void> {
    try {
      const response = await this.sendRequest('stop_recording', {});
      const result = response as unknown as PromptResult;
      this.isRecording = false;
      this.lastRawText = result.raw_text || '';
      this.lastCompiledPrompt = result.compiled_prompt || result.agent_input?.prompt || '';
      this.lastError = '';
      this.updateStatusBar();
      this.render();

      const config = vscode.workspace.getConfiguration('voicePrompt');
      const autoCopy = config.get<boolean>('autoCopyToClipboard', true);
      if (autoCopy && this.lastCompiledPrompt) {
        await vscode.env.clipboard.writeText(this.lastCompiledPrompt);
        void vscode.window.showInformationMessage('Compiled prompt copied to clipboard.');
      }
    } catch (error) {
      this.isRecording = false;
      this.lastError = String(error);
      this.updateStatusBar();
      this.render();
      void vscode.window.showErrorMessage(`Failed to stop recording: ${String(error)}`);
    }
  }

  private async copyPromptToClipboard(): Promise<void> {
    if (!this.lastCompiledPrompt) {
      void vscode.window.showWarningMessage('No compiled prompt available yet.');
      return;
    }
    await vscode.env.clipboard.writeText(this.lastCompiledPrompt);
    void vscode.window.showInformationMessage('Compiled prompt copied to clipboard.');
  }

  private async insertPromptAtCursor(): Promise<void> {
    const editor = vscode.window.activeTextEditor;
    if (!editor) {
      void vscode.window.showWarningMessage('No active editor found.');
      return;
    }
    const text = this.lastCompiledPrompt;
    if (!text) {
      void vscode.window.showWarningMessage('No compiled prompt available yet.');
      return;
    }
    await editor.edit((editBuilder: vscode.TextEditorEdit) => {
      editBuilder.insert(editor.selection.active, text);
    });
  }

  private updateStatusBar(): void {
    if (this.isRecording) {
      this.statusBar.text = '$(primitive-square) Stop Voice Prompt';
      return;
    }
    if (!this.isBackendReady) {
      this.statusBar.text = '$(sync~spin) Voice Prompt Loading';
      return;
    }
    this.statusBar.text = '$(mic) Start Voice Prompt';
  }

  private render(): void {
    if (!this.panel && !this.sidebarView) {
      return;
    }

    const stateText = this.isRecording
      ? '正在录音中...'
      : this.isBackendReady
        ? '后端已就绪'
        : '后端启动中...';

    const safeRaw = this.escapeHtml(this.lastRawText || '暂无转写内容');
    const safePrompt = this.escapeHtml(this.lastCompiledPrompt || '暂无编译后的 prompt');
    const safeError = this.escapeHtml(this.lastError || '无');
    const toggleLabel = this.isRecording ? '停止录音并编译' : '开始录音';

    const html = `<!DOCTYPE html>
<html lang="zh-CN">
<head>
  <meta charset="UTF-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1.0" />
  <style>
    :root {
      color-scheme: light dark;
      --bg: #0f172a;
      --panel: #111827;
      --panel-border: rgba(148, 163, 184, 0.18);
      --text: #e5eefc;
      --muted: #9fb1cc;
      --accent: #4ade80;
      --accent-2: #38bdf8;
      --warn: #fb7185;
    }
    body {
      margin: 0;
      padding: 20px;
      background: radial-gradient(circle at top, #12233f, var(--bg));
      color: var(--text);
      font-family: "Segoe UI", "PingFang SC", sans-serif;
    }
    .header {
      display: flex;
      align-items: center;
      justify-content: space-between;
      margin-bottom: 16px;
    }
    .status {
      color: ${this.isRecording ? 'var(--accent)' : 'var(--accent-2)'};
      font-weight: 700;
    }
    .actions {
      display: flex;
      gap: 10px;
      margin-bottom: 16px;
    }
    button {
      border: none;
      border-radius: 10px;
      padding: 10px 14px;
      background: linear-gradient(135deg, #2563eb, #0ea5e9);
      color: white;
      cursor: pointer;
      font-size: 13px;
      font-weight: 600;
    }
    button.secondary {
      background: linear-gradient(135deg, #1f2937, #334155);
    }
    .card {
      background: rgba(15, 23, 42, 0.82);
      border: 1px solid var(--panel-border);
      border-radius: 16px;
      padding: 14px;
      margin-bottom: 14px;
      backdrop-filter: blur(10px);
    }
    .label {
      color: var(--muted);
      font-size: 12px;
      margin-bottom: 8px;
      text-transform: uppercase;
      letter-spacing: 0.08em;
    }
    pre {
      white-space: pre-wrap;
      word-break: break-word;
      margin: 0;
      line-height: 1.6;
      font-size: 13px;
    }
    .error {
      color: var(--warn);
    }
  </style>
</head>
<body>
  <div class="header">
    <div>
      <div style="font-size: 20px; font-weight: 700;">Voice Prompt</div>
      <div class="status">${stateText}</div>
    </div>
  </div>
  <div class="actions">
    <button id="toggleRecording">${toggleLabel}</button>
    <button id="copyPrompt" class="secondary">复制 Prompt</button>
    <button id="insertPrompt" class="secondary">插入当前编辑器</button>
  </div>
  <div class="card">
    <div class="label">Raw Transcript</div>
    <pre>${safeRaw}</pre>
  </div>
  <div class="card">
    <div class="label">Compiled Prompt</div>
    <pre>${safePrompt}</pre>
  </div>
  <div class="card">
    <div class="label">Backend / Error</div>
    <pre class="error">${safeError}</pre>
  </div>
  <script>
    const vscode = acquireVsCodeApi();
    document.getElementById('toggleRecording').addEventListener('click', () => {
      vscode.postMessage({ command: 'toggleRecording' });
    });
    document.getElementById('copyPrompt').addEventListener('click', () => {
      vscode.postMessage({ command: 'copyPrompt' });
    });
    document.getElementById('insertPrompt').addEventListener('click', () => {
      vscode.postMessage({ command: 'insertPrompt' });
    });
  </script>
</body>
</html>`;

    if (this.panel) {
      this.panel.webview.html = html;
    }

    if (this.sidebarView) {
      this.sidebarView.webview.html = html;
    }
  }

  private escapeHtml(input: string): string {
    return input
      .replace(/&/g, '&amp;')
      .replace(/</g, '&lt;')
      .replace(/>/g, '&gt;');
  }
}

let controller: VoicePromptController | undefined;

export async function activate(context: vscode.ExtensionContext): Promise<void> {
  controller = new VoicePromptController(context);
  context.subscriptions.push({ dispose: () => void controller?.dispose() });

  context.subscriptions.push(
    vscode.window.registerWebviewViewProvider('voicePrompt.sidebar', {
      resolveWebviewView(webviewView: vscode.WebviewView) {
        controller?.resolveWebviewView(webviewView);
      },
    })
  );

  context.subscriptions.push(vscode.commands.registerCommand('voicePrompt.openPanel', async () => {
    await controller?.openPanel();
  }));

  context.subscriptions.push(vscode.commands.registerCommand('voicePrompt.toggleRecording', async () => {
    await controller?.toggleRecording();
  }));

  context.subscriptions.push(vscode.commands.registerCommand('voicePrompt.copyPrompt', async () => {
    await controller?.copyPromptCommand();
  }));

  await controller.initialize();
}

export function deactivate(): Promise<void> | undefined {
  return controller?.dispose();
}
