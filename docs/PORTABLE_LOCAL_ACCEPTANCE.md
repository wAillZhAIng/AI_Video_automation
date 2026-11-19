# 本地模型部署验收

按 [本地推理节点教程](local-inference.md) 在支持的 NVIDIA Windows 主机上准备 ComfyUI、Qwen、H3 和 4step LoRA，并核对两个步骤。

1. `GET /video_factory/runtime` 返回 `ready: true`，在设置页保存并测试 Context-IR 档案。
2. 从 Word 导入一个短镜头。选择 Fast 4，核对实际运行回执中的工作流和四步采样，下载并完整解码最终 MP4。再按需要验收 Quality 20 和 Quality 30。

检查应用平台与数据卷的结果见 [发布验收](RELEASE_VALIDATION.md)。
