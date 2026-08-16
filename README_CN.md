# MiniMax H3 单画布长视频人物替换工作流

这是一个基于 MiniMax H3 Ref2VA 与 Motion Context 的 ComfyUI 长视频人物替换方案。
最终版本把输入、动态分段、递归生成、上下文续接、片段保存、检查点恢复、合并、精确裁剪和原音轨恢复集中在一张可见画布中。

## 推荐下载

| 文件 | 用途 |
|---|---|
| `assets/workflows/h3-native-loop-user-final-no-audio-compatible-ui.json` | 用户最终配置；124 帧/段、无音频兼容、活动 LightX2V LoRA、画布成片预览 |
| `assets/workflows/h3-native-loop-user-final-no-audio-compatible-low-vram-ui.json` | 用户最终低显存配置；107 帧/段，其余链路与普通版一致 |
| `assets/workflows/h3-native-loop-final-stable-ui.json` | 推荐的 12 GB 低显存稳定版；107 帧/段，Turbo LoRA 已断开 |
| `assets/workflows/h3-native-loop-final-turbo-experimental-ui.json` | 保留最终画布中的 Turbo 路线，仅供兼容模型实验 |
| `assets/workflows/h3-native-loop-long-video-character-swap-ui.json` | 已验证的基础单画布版本 |
| `RELEASE_NOTES.md` | 最终版本行为与本地验证记录 |
| `THIRD_PARTY_NOTICES.md` | 参考项目、第三方依赖和许可说明 |

Git 源码历史不包含模型、源视频、人物参考图或生成成片；用户提供的演示视频仅作为 Release 附件发布。

## 演示视频

- [生成效果（25.01 秒，1440x2560，60 fps）](https://github.com/ldq30000-tech/minimax-h3-chained-character-swap/releases/download/v1.0.0/generated-character-swap-result.mp4)
- [模板/参考视频（25.61 秒，720x1280，30 fps）](https://github.com/ldq30000-tech/minimax-h3-chained-character-swap/releases/download/v1.0.0/template-reference-video.mp4)

两段视频用于展示工作流输入与输出效果，但时长不同，不是严格等长的逐帧前后对比素材。视频媒体不因附加到 Release 而自动适用本仓库的 MIT 许可。

## 相对参考实现的扩展与优势

本项目明确参考并保留
[MacroSony/minimax-h3-chained-character-swap](https://github.com/MacroSony/minimax-h3-chained-character-swap)
的历史、MIT 许可、22 帧 Motion Context 配方、临时色度噪声渐退、QA 方法和安全停止原则。
最终单画布版本在此基础上增加：

1. **一个工作流完成完整链路。** 昂贵的 Ref2VA、Motion Context、采样、裁剪、保存和循环节点全部在画布上可见，便于定位错误。
2. **自动适配视频长度。** 输入完整视频后自动按 24 fps 统计唯一帧数，并按 H3 的 `17k+5` 长度网格规划片段。12 GB 稳定版默认把每轮限制在 107 帧；无法整除时通过推理末尾补帧重新平衡，禁止尾段合并成 124/141/158 帧，交付前仍精确裁回源长度。
3. **按段流式读取，降低系统内存峰值。** 全局只读取视频元数据与音频；普通用户版每轮最多解码 124 帧，低显存用户版和稳定版最多 107 帧，不再把整段长视频展开成一个 float32 图片张量。576x1024、24 fps、20 步、seed 和 22 帧 Motion Context 均保持不变。
4. **只在推理输入补帧。** 尾部不足时只复制推理参考帧，最终节点严格裁回真实源帧数，不把重复补帧交付出去。
5. **连续而不重播时间线。** 每段读取连续源时间戳，后续段使用前一段干净输出的 22 帧尾部建立 Motion Context，然后删除重复上下文。
6. **原声音轨自动恢复。** 中间生成音频仅用于链路；最终 MP4 重新封装原视频声音并保持源时长。源视频没有可解码音轨时，工作流自动生成同长度的 44.1 kHz 单声道静音，不再中止。最终精确裁剪节点会在画布中直接显示可播放的视频预览和保存路径。
7. **可诊断、可恢复。** 每段保存 MP4、音频与 safetensors 检查点；Review Gate 可见但默认关闭；完整片段可以不重新采样而恢复合并。源视频内容也进入检查点指纹，更换视频不会误用旧片段。
8. **低显存辅助链。** 最终画布保留 ReservedVRAM 与 KJNodes 的 H3 SageAttention patch。12 GB 环境已经完成过实例运行，但不代表最低显存保证。
9. **保守稳定版与用户配置分开。** 稳定版继续断开 Turbo LoRA；两份用户最终版则按本机修改保留活动 LightX2V LoRA 与 20 步采样，便于复现实际配置，不把它冒充为所有模型环境都兼容的默认方案。

这些改进提升的是自动化、可观察性、时间线完整性和恢复能力；它们不构成逐帧动作严格控制，也不能取消人工质量检查。

## 环境与前提

- Windows 或 Linux，Python 3.10+。
- 支持当前 ComfyUI/PyTorch 的 NVIDIA CUDA 环境。
- 使用较新的 ComfyUI；其原生 `VIDEO` 输入必须支持流式元数据、源文件访问和 `as_trimmed()` 分段读取。
- FFmpeg 与 FFprobe 可在 `PATH` 中调用。
- 足够的 GPU 显存、系统内存和模型存储。上游 RH 节点以 24 GB 级单卡为主要目标；更低显存依赖强卸载并会显著变慢。
- 输入必须是用户有权使用的源视频、角色参考图和模型。

历史验证记录：RTX 5070 Ti Laptop 12 GB、576x1024、24 fps、20 步、旧 124 帧配置共 6 段，完整生成约 2 小时 29 分钟。当前稳定版改用 107 帧上限以避免显存和系统内存换页；它会增加少量分段，实际提速取决于显卡、卸载策略和素材长度。

## 必需自定义节点

在 `ComfyUI/custom_nodes` 中分别安装：

```bash
git clone https://github.com/ldq30000-tech/minimax-h3-chained-character-swap.git
git clone https://github.com/HM-RunningHub/ComfyUI_RH_MinMaxH3.git
git clone https://github.com/ethanfel/ComfyUI-MiniMaxH3-Contex-Loop.git
git clone https://github.com/kijai/ComfyUI-KJNodes.git
git clone https://github.com/Windecay/ComfyUI-ReservedVRAM.git
```

使用 ComfyUI 自己的 Python 环境安装每个仓库的 `requirements.txt`，然后重启 ComfyUI。
若使用其他兼容 Motion Context 实现，需要确保它提供工作流中同名的 `MiniMaxH3Chain*`、引用、裁剪、保存和合并节点。

## 模型文件

稳定版默认引用以下名称，放在对应的 ComfyUI 模型目录；实际目录以 RH MiniMax H3 节点文档为准：

```text
minimax/minimax_h3_ref2va_pruned_int8_convrot.safetensors
minimax/qwen3vl_32b_minimax_h3_nvfp4_awq.safetensors
minimax/minimax_h3_video_vae_fp16.safetensors
minimax/minimax_h3_audio_vae_fp32.safetensors
minimax/minimax_h3_ref2v_lightx2v_turbo_4step_v0.1_resized_avg_rank_20_bf16.safetensors
```

推荐采样配置：`res_multistep`、`beta`、20 步、denoise 1.0。

稳定版画布中展示的两个 Turbo LoRA 均为禁用状态。不要把它们连接到
`minimax_h3_ref2va_pruned_int8_convrot.safetensors`。只有换成兼容的非 pruned H3 基础模型后，才能按照对应 LoRA 文档启用并调整步数。

两份 `user-final` 工作流不同：它们按用户当前修改保留节点 `1972` 的活动
LightX2V 路线，并继续使用 `res_multistep`、`beta`、20 步。这是用户实测配置，
不是对其他 H3 基础模型、LoRA 版本或显卡环境的兼容性保证。导入后先确认本机
模型文件名和节点版本一致；需要保守配置时改用 `final-stable`。

## 输入文件

导入所选工作流后，在左侧选择：

1. `@character_front`：人物正面图。
2. `@character_side`：人物侧面图。
3. `@character_back`：人物背面图。
4. `@character_face`：高分辨率脸部近照。
5. `source_video`：完整原视频，最好带原始声音。

工作流内的占位文件名只用于显示，必须重新选择自己的文件。近景镜头缺少脸部特写参考时，换脸失败通常不是 seed 能解决的问题。

## 使用方法

1. 要复现当前修改时，导入对应的 `h3-native-loop-user-final-*.json`；需要保守默认时导入 `h3-native-loop-final-stable-ui.json`。
2. 选择四张角色图和一个源视频。
3. 检查模型名称、输出分辨率、提示词、seed 和输出目录。
4. 保持 Review Gate 关闭即可一次排队自动跑完整链路；需要逐段验收时再开启。
5. 点击 Queue。不要在同一 GPU 上同时排其他大型模型任务。
6. 最终文件由绿色 `H3FinalTrimToSource` 节点输出，位于：

```text
ComfyUI/output/h3_chains/<run_name>/final/character_swap_full_exact*.mp4
```

中间的 `character_swap_assembled*.mp4` 可能包含推理尾部补帧，不是最终交付文件。
各流式工作流使用自己的新 `h3_native_loop_*` 运行名，不会自动续用旧版整片解码工作流的检查点。
将本仓库安装到 `ComfyUI/custom_nodes` 并重启后，最终节点会直接显示可播放成片；
ComfyUI 重启后也会按该节点的文件名前缀恢复最近一次匹配的最终视频。

## 重跑局部片段

`Loop Start` 支持连续范围，例如 `scene_range = 1:2`。由于每段 Motion Context 依赖前一段，修改前两段后应从第 3 段继续生成到结尾，不能把不连续的新旧片段当作无缝链路。更换 seed 后保持模型、提示词、参考图和分段设置不变，否则旧检查点不会通过一致性校验。

## 限制

- 适合慢速到中速动作，不保证快速格斗或突然变向。
- Ref2VA 与相位检测不是严格逐帧姿态控制。
- 同一配置可能出现 seed 不替换人物的情况，需要候选 seed 与人工检查。
- 硬切镜头应在提示词 `detailed_description` 内按时间写成多个 Shot。
- 必须检查身份、手部、背景、锐度、动作相位、22 帧上下文出口和真实播放接缝。

详细规则参见 `references/RECIPE.md`、`references/GOTCHAS.md`、`references/LIMITATIONS.md` 与 `THIRD_PARTY_NOTICES.md`。
