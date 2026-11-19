# Linux 安装教程

适用于可运行 Linux Docker 容器的 x86-64 和 ARM64 主机。安装 [Docker Engine](https://docs.docker.com/engine/install/) 及 [Compose 插件](https://docs.docker.com/compose/install/linux/)，准备好当前用户的 Docker 权限或 rootless 上下文。

```
docker version
docker compose version
git clone https://github.com/wAillZhAIng/AI_Video_automation.git
cd AI_Video_automation
docker compose up -d
docker compose ps
```

打开 [项目网页](http://127.0.0.1:8088)。也可以运行 `bash ./Linux管理.sh`，选启动、关闭或清理。桌面启动完成后使用默认浏览器，无图形会话时输出访问地址。

## 配置模型

- 云端服务在 **系统设置 → 服务档案** 中按用途选择协议，填写 HTTPS 地址、模型 ID 和密钥，再测试连接。
- 自有节点使用 Docker 容器能访问的私网地址。例如在独立 NVIDIA Windows 推理主机启动 [Context-IR 节点](local-inference.md)，在视频服务档案填写该主机的 `8788` 地址。
- 在同一台 Linux 主机部署本项目有对应适配器的服务时，使用 Compose 提供的 `host.docker.internal`，服务需监听容器可访问的宿主地址。按 Docker 网段限制访问。给网页配置的 `127.0.0.1` 会解析到后端容器自身。

应用与推理对 CUDA、Metal 和具体模型的要求各自独立。随仓的 H3 精确工作流部署教程以 Windows NVIDIA 节点为基线。

## 维护

```
bash ./Linux管理.sh stop
bash ./scripts/backup-docker.sh
docker compose logs --tail=100 backend frontend
```

一键清理会在确认后删除此 Compose 项目的三个数据卷及对应容器。从空白状态启用应用请再次启动。备份与恢复见 [Docker 维护教程](DOCKER_RELEASE_GUIDE_ZH.md)，制作步骤见 [Word 到 MP4 教程](USER_GUIDE_ZH.md)。
