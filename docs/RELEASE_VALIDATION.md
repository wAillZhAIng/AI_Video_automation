# 发布验收

仓库提供空白部署所需的源码、工作流、公共 Word 模板及管理入口。应用容器与独立模型服务通过 HTTP 连接。

## 2026 年 11 月 19 日发布验证

| 项目 | 验证结果 |
| --- | --- |
| 空卷构建与启动 | Windows Docker Desktop 的 Linux AMD64 引擎完成源码构建，两个非 root 服务通过健康检查，项目列表为空。 |
| 真实生产 | 通过容器设置连接独立的 H3 节点。结构化 Word 自动启动制作，执行 Context-IR 和 Fast 4 四步采样，直接返回 4 秒、1280×720、24 fps 的 H.264/AAC MP4。经 HTTP 下载后，视频与音频均完整解码。 |
| 一键管理 | 实际双击 Windows 入口并验证三个按钮。启动打开默认浏览器，关闭保留三个命名卷，取消清理保留数据，确认清理只移除验收项目的容器和三卷。 |
| 前端 | 240 项测试通过，TypeScript 检查与 Vite 生产构建通过。 |
| 源码边界 | 公开 Word 文件保留正文、布局和制作规则标识，作者属性已净化。验收时产生的数据库、密钥、资料、成片和日志均在公开源码之外。 |

## 持续集成与平台范围

[GitHub Actions](https://github.com/wAillZhAIng/AI_Video_automation/actions) 检查前后端、Windows/macOS/Linux 入口、AMD64/ARM64 镜像、空卷首次启动和持久化。打开与下载版本对应的工作流查看实际结果。

应用 Docker 兼容性与推理硬件分别验收。Windows NVIDIA Context-IR 节点的具体条件见 [本地模型部署教程](local-inference.md)。Apple Metal 或 Linux GPU 的原生 H3 生成仍需独立验证。
