# Windows 脚本目录一键批量制作

双击项目根目录的 `一键批量产出.cmd`，程序会递归扫描 `D:\脚本` 中的 Word 文档，并把每份 Word 所在产品目录中的 PNG/JPG/JPEG/WebP 图片作为共享参考图。它先输出文档与图片 SHA-256、产品分组和本机服务预检；输入 `START ALL` 后，才按顺序导入 Word、同步产品图库图片、启动视频生成并等待结果。

默认规格为 `resolution=4k`、`mode=fast`、`processing=ai_super_resolution`、`aspect_ratio=16:9`。Fast 使用应用的本机 H3 Context-IR 路由选择 4-step 工作流；4K 后期超分复用实例中已经安装并验收的 Real-ESRGAN x4plus。Real-ESRGAN 是逐帧图像模型，不是时序超分；程序只在服务端精确报告 4K/16:9 AI 超分已验证时提交，不会因为缺少 SeedVR2 而阻断，也不会静默改为普通缩放。其他画幅、尺寸若没有各自的验收能力，会在创建项目之前阻断。

## 连接当前 Vast 实例

启动器通过 SSH 本地转发连接远端自动化 API 的 `127.0.0.1:18766`，不需要公开新的 Web/API 端口。Windows 必须安装 OpenSSH Client，并且已配置可登录该 Vast 实例的 SSH 密钥。把以下两行写入项目 `.local-runtime\folder-production-ssh.txt`：

```text
root@VAST_IP_ADDRESS
VAST_SSH_PORT
```

例如第一行填写实例 SSH 用户和地址，第二行填写 Vast 控制台显示的 SSH 映射端口。该文件已被 Git 忽略，不要提交。启动器会创建临时 localhost 隧道、检查 `/api/health`，完成后关闭自己启动的 SSH 进程；连接诊断写入 `.local-runtime\folder-production-ssh.*.log`。若明确设置了 `VIDEO_FACTORY_API_URL`，则使用该地址且不自动创建隧道。没有 SSH 配置时，才尝试 `.local-runtime\folder-production-api.txt` 或本机 `http://127.0.0.1:8765`。

## 实例本机模型

`AI_Video_Creation` 与 `AI_Video_automation` 的运行配置使用同一台实例上的 ComfyUI（`127.0.0.1:18188`）、H3 Context-IR（`127.0.0.1:8788`）、Qwen3-VL 4B（`127.0.0.1:8772/v1`）、语音识别（`127.0.0.1:8778`）和 FFmpeg。两套应用各自保留项目数据库与服务档案；模型权重留在实例的共享 ComfyUI/模型路径，不复制或重复下载。H3 专用 Qwen 编码器不作为视觉对话模型使用；视觉理解使用完整的 Qwen3-VL Instruct 服务。

经实例报告，RealESRGAN_x4plus 4K/16:9 是已验收规格；本机模型目录还包含 `realesr-general-x4v3`。一键默认选择已验收的 x4plus 档案。超分模型的文件分辨率不代表时序细节或主观画质已经合格，成片仍需人工检查。

## 文件夹规则

- 一个产品文件夹可以放多份 Word 和多张产品图片；该文件夹内每份 Word 共用这些图片。
- 多级目录各自形成产品组；同一图片只按 SHA-256 上传一次。
- 文档明确写出产品名称或品牌时采用原文；其余情况使用相对目录名，不推测品牌、规格或产品属性。
- 损坏的 Word 或图片按单个文件隔离并报告，不影响其他文件。每组最多 8 张产品图，超出时阻断并显示原因，不静默丢图。
- Word 原文件不会改写；上传的是原始 DOCX 字节和同目录图片。

## 断点续跑与费用控制

本地 `data\folder_production_state.json` 保存源文件哈希、图片哈希、质量参数、去敏 API 主机和项目/run ID。相同输入已提交、运行中、完成或结果未知时会跳过，避免重复派单。网络写请求超时会记为“结果未知”，需要先在应用中核对项目状态；不会自动重试可能已经提交的任务。只有明确失败于预检、且尚未创建项目的条目才可安全重验。

启动器先只读扫描和预检，再要求输入 `START ALL`。所有任务都使用实例本机模型；云端计费与付费回退保持关闭。GPU 实例仍按 Vast 当前租用费率计时。

命令行只读预检：

```powershell
D:\人物一致性\.venv\Scripts\python.exe D:\人物一致性\scripts\one_click_folder_production.py --root D:\脚本 --api http://127.0.0.1:18766 --all --dry-run --limit 500
```
