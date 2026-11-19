using System;
using System.Diagnostics;
using System.Drawing;
using System.IO;
using System.Security.Cryptography;
using System.Text;
using System.Threading;
using System.Threading.Tasks;
using System.Windows.Forms;

internal static class Program
{
    [STAThread]
    private static void Main()
    {
        string root = Path.GetFullPath(AppDomain.CurrentDomain.BaseDirectory);
        string instance;
        using (var sha = SHA256.Create())
        {
            instance = BitConverter.ToString(sha.ComputeHash(Encoding.UTF8.GetBytes(root.ToUpperInvariant())));
        }
        bool first;
        using (var single = new Mutex(true, @"Local\VideoFactoryManager-" + instance, out first))
        {
            if (!first)
            {
                MessageBox.Show("本项目的管理窗口已打开。", "AI 视频自动化 · 管理程序",
                    MessageBoxButtons.OK, MessageBoxIcon.Information);
                return;
            }
            Application.EnableVisualStyles();
            Application.SetCompatibleTextRenderingDefault(false);
            Application.Run(new ManagerWindow(root));
        }
    }
}

internal sealed class ManagerWindow : Form
{
    private static readonly Color Ink = Color.FromArgb(24, 39, 54);
    private static readonly Color Muted = Color.FromArgb(75, 91, 105);
    private readonly string root;
    private readonly Button startButton;
    private readonly Button stopButton;
    private readonly Button cleanButton;
    private readonly Label status;
    private readonly TextBox output;
    private Operation active;
    private string pending;
    private bool closePending;

    private sealed class Operation
    {
        internal readonly string Action;
        internal volatile bool Cancel;

        internal Operation(string action)
        {
            Action = action;
        }
    }

    internal ManagerWindow(string directory)
    {
        root = directory;
        Name = "VideoFactoryManager";
        Text = "AI 视频自动化 · 管理程序";
        Font = new Font("Microsoft YaHei UI", 10F);
        ForeColor = Ink;
        BackColor = Color.FromArgb(245, 248, 251);
        AutoScaleMode = AutoScaleMode.Font;
        StartPosition = FormStartPosition.CenterScreen;
        ClientSize = new Size(695, 438);
        MinimumSize = new Size(580, 390);

        var layout = new TableLayoutPanel
        {
            Dock = DockStyle.Fill, Padding = new Padding(24), ColumnCount = 1, RowCount = 6,
        };
        layout.ColumnStyles.Add(new ColumnStyle(SizeType.Percent, 100F));
        layout.RowStyles.Add(new RowStyle(SizeType.AutoSize));
        layout.RowStyles.Add(new RowStyle(SizeType.AutoSize));
        layout.RowStyles.Add(new RowStyle(SizeType.Absolute, 63F));
        layout.RowStyles.Add(new RowStyle(SizeType.AutoSize));
        layout.RowStyles.Add(new RowStyle(SizeType.Percent, 100F));
        layout.RowStyles.Add(new RowStyle(SizeType.AutoSize));
        Controls.Add(layout);

        var title = new Label
        {
            Text = "AI 视频自动化", AutoSize = true, Margin = new Padding(0),
            Font = new Font(Font.FontFamily, 23F, FontStyle.Bold),
        };
        layout.Controls.Add(title, 0, 0);
        var location = new Label
        {
            AutoSize = true, Text = "Docker Compose  ·  " + root.TrimEnd('\\'),
            ForeColor = Muted, Margin = new Padding(1, 3, 0, 4),
        };
        layout.Controls.Add(location, 0, 1);

        var actions = new TableLayoutPanel
        {
            Dock = DockStyle.Fill, ColumnCount = 3, RowCount = 1, Margin = new Padding(0, 12, 0, 9),
        };
        for (int i = 0; i < 3; i++)
        {
            actions.ColumnStyles.Add(new ColumnStyle(SizeType.Percent, 100F / 3F));
        }
        startButton = MakeButton("一键启动", "StartProject", "start", true);
        stopButton = MakeButton("一键关闭", "StopProject", "stop", false);
        cleanButton = MakeButton("一键清理", "CleanProject", "clean", false);
        cleanButton.Margin = new Padding(0);
        actions.Controls.Add(startButton, 0, 0);
        actions.Controls.Add(stopButton, 1, 0);
        actions.Controls.Add(cleanButton, 2, 0);
        layout.Controls.Add(actions, 0, 2);

        status = new Label
        {
            Name = "OperationStatus", AutoSize = true, Text = "等待操作",
            Margin = new Padding(1, 5, 0, 7), Font = new Font(Font, FontStyle.Bold),
        };
        layout.Controls.Add(status, 0, 3);
        output = new TextBox
        {
            Name = "OperationLog", AccessibleName = "管理操作日志", Multiline = true,
            ScrollBars = ScrollBars.Both, WordWrap = false, ReadOnly = true,
            Dock = DockStyle.Fill, BackColor = Color.White, ForeColor = Ink, Margin = new Padding(0),
        };
        layout.Controls.Add(output, 0, 4);
        var help = new Label
        {
            Text = "关闭会中断正在运行的任务。清理会永久移除此 Compose 项目的数据卷。",
            Dock = DockStyle.Fill, AutoSize = true, ForeColor = Muted, Margin = new Padding(0, 9, 0, 0),
        };
        layout.Controls.Add(help, 0, 5);
        Append("Docker 引擎就绪后即可启动。网页通过健康检查后会自动打开。");
        ActiveControl = startButton;
    }

    private Button MakeButton(string text, string name, string action, bool primary)
    {
        var button = new Button
        {
            Text = text, Name = name, AccessibleName = text, Dock = DockStyle.Fill,
            FlatStyle = FlatStyle.Flat, Margin = new Padding(0, 0, 10, 0),
            BackColor = primary ? Color.FromArgb(25, 77, 154) : Color.White,
            ForeColor = primary ? Color.White : Ink, UseVisualStyleBackColor = false,
        };
        button.FlatAppearance.BorderColor = primary ? button.BackColor : Color.FromArgb(179, 194, 210);
        button.Click += (sender, args) =>
        {
            if (action != "clean" || ConfirmReset()) RequestOperation(action);
        };
        return button;
    }

    private bool ConfirmReset()
    {
        string message =
            "清理会关闭此 Compose 项目，并永久删除项目历史、服务档案及密钥、上传与生成文件、成片和运行日志。"
            + Environment.NewLine + Environment.NewLine
            + "再次启动会创建空的数据卷。若需要旧记录，请先备份。"
            + Environment.NewLine + Environment.NewLine + "确认清理此目录所配置的 Compose 项目？";
        return MessageBox.Show(this, message, "确认恢复全新状态",
            MessageBoxButtons.YesNo, MessageBoxIcon.Warning, MessageBoxDefaultButton.Button2) == DialogResult.Yes;
    }

    private void RequestOperation(string action)
    {
        if (active != null)
        {
            if (active.Action == "start" && action != "start" && pending == null)
            {
                pending = action;
                active.Cancel = true;
                status.Text = "正在取消启动…";
                UpdateActions();
            }
            return;
        }
        var operation = new Operation(action);
        active = operation;
        status.Text = action == "start" ? "正在启动…" : action == "stop" ? "正在关闭…" : "正在恢复全新状态…";
        Append("");
        Append(status.Text);
        UpdateActions();
        var thread = new Thread(() => RunOperation(operation)) { IsBackground = true };
        thread.Start();
    }

    private void UpdateActions()
    {
        bool idle = active == null;
        bool canCancel = !idle && active.Action == "start" && pending == null;
        startButton.Enabled = idle;
        stopButton.Enabled = idle || canCancel;
        cleanButton.Enabled = idle || canCancel;
    }

    private void RunOperation(Operation operation)
    {
        int exitCode = -1;
        try
        {
            string script = Path.Combine(root, @"scripts\manage.ps1");
            if (!File.Exists(script))
                throw new FileNotFoundException("管理脚本缺失。请完整解压仓库，再双击 Windows管理.cmd。", script);
            if (!operation.Cancel)
            {
                string powershell = Path.Combine(Environment.SystemDirectory, @"WindowsPowerShell\v1.0\powershell.exe");
                string arguments = "-NoLogo -NoProfile -NonInteractive -ExecutionPolicy Bypass -File \"" + script
                    + "\" -Action " + operation.Action + (operation.Action == "clean" ? " -Yes" : " -EmitBrowserUrl");
                var info = new ProcessStartInfo(powershell, arguments)
                {
                    WorkingDirectory = root, UseShellExecute = false, CreateNoWindow = true,
                    RedirectStandardOutput = true, RedirectStandardError = true,
                    StandardOutputEncoding = Encoding.UTF8, StandardErrorEncoding = Encoding.UTF8,
                };
                using (var process = new Process { StartInfo = info })
                {
                    var stdoutFinished = new TaskCompletionSource<bool>();
                    var stderrFinished = new TaskCompletionSource<bool>();
                    process.OutputDataReceived += (sender, args) =>
                    {
                        if (args.Data == null) stdoutFinished.TrySetResult(true);
                        else if (operation.Action == "start" && args.Data.StartsWith("VF_OPEN_BROWSER ", StringComparison.Ordinal))
                            QueueBrowser(operation, args.Data.Substring("VF_OPEN_BROWSER ".Length));
                        else Publish(args.Data);
                    };
                    process.ErrorDataReceived += (sender, args) =>
                    {
                        if (args.Data == null) stderrFinished.TrySetResult(true);
                        else Publish(args.Data);
                    };
                    process.Start();
                    process.BeginOutputReadLine();
                    process.BeginErrorReadLine();
                    var clock = Stopwatch.StartNew();
                    int limitSeconds = operation.Action == "start" ? 1500 : 75;
                    while (!process.WaitForExit(120))
                    {
                        if (operation.Cancel || clock.Elapsed.TotalSeconds >= limitSeconds)
                        {
                            StopOperationTree(process);
                            if (!process.WaitForExit(3500))
                                throw new TimeoutException("Windows 尚未释放管理进程，请检查该进程状态。");
                            if (!operation.Cancel)
                                Publish("操作已达到等待时限。可以点击一键关闭回收本项目进程。");
                            break;
                        }
                    }
                    exitCode = process.ExitCode;
                    Task.WaitAll(new Task[] { stdoutFinished.Task, stderrFinished.Task }, 1250);
                }
            }
        }
        catch (Exception error)
        {
            Publish("操作未完成。" + error.Message);
        }
        OnUi(() => Finish(operation, exitCode));
    }

    private static void StopOperationTree(Process process)
    {
        if (process.HasExited) return;
        string taskkill = Path.Combine(Environment.SystemDirectory, "taskkill.exe");
        using (Process killer = Process.Start(new ProcessStartInfo(taskkill, "/PID " + process.Id + " /T /F")
        {
            UseShellExecute = false, CreateNoWindow = true,
        }))
        {
            if (killer != null) killer.WaitForExit(5000);
        }
        if (!process.HasExited) process.Kill();
    }

    private void QueueBrowser(Operation operation, string url)
    {
        Uri uri;
        if (!Uri.TryCreate(url, UriKind.Absolute, out uri) || uri.Scheme != Uri.UriSchemeHttp)
        {
            Publish("未能识别网页地址。请查看上一行的启动输出。");
            return;
        }
        ThreadPool.QueueUserWorkItem(unused =>
        {
            if (operation.Cancel) return;
            try
            {
                using (Process browser = Process.Start(new ProcessStartInfo(uri.AbsoluteUri) { UseShellExecute = true }))
                {
                    Publish("浏览器已打开 " + uri.AbsoluteUri);
                }
            }
            catch (Exception error)
            {
                Publish("自动打开浏览器失败。请手动访问 " + uri.AbsoluteUri + " " + error.Message);
            }
        });
    }

    private void Finish(Operation operation, int exitCode)
    {
        active = null;
        if (closePending)
        {
            Close();
            return;
        }
        if (pending != null)
        {
            string next = pending;
            pending = null;
            RequestOperation(next);
            return;
        }
        if (exitCode == 0)
            status.Text = operation.Action == "start" ? "服务已就绪" : operation.Action == "stop" ? "已关闭" : "已恢复全新状态";
        else
            status.Text = "操作未完成，请查看日志";
        UpdateActions();
    }

    private void OnUi(Action update)
    {
        if (IsDisposed || !IsHandleCreated) return;
        try { BeginInvoke(update); }
        catch (InvalidOperationException) { /* The window was closed. */ }
    }

    private void Publish(string message)
    {
        OnUi(() => Append(message));
    }

    private void Append(string message)
    {
        if (output.TextLength > 60000) output.Clear();
        output.AppendText(message + Environment.NewLine);
    }

    protected override void OnFormClosing(FormClosingEventArgs args)
    {
        if (active != null && args.CloseReason == CloseReason.UserClosing)
        {
            closePending = true;
            pending = null;
            active.Cancel = true;
            status.Text = "正在结束管理操作…";
            args.Cancel = true;
        }
        base.OnFormClosing(args);
    }
}
