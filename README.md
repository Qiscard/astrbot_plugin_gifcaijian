# GIF工具箱 (`astrbot_plugin_gifcaijian`)

AstrBot 动图 / 图片处理插件：视频转动图、精灵图合成、GIF 变速与分解、**智能网格裁剪**、自动去白边、线稿与表情包做旧。

| 项 | 信息 |
|---|---|
| 版本 | **v1.7.0** |
| 维护 / 二次开发 | **Qiscard** |
| 原作者 | **shskjw** |
| 本仓库 | https://github.com/Qiscard/astrbot_plugin_gifcaijian |
| 原仓库 | https://github.com/shskjw/astrbot_plugin_gifcaijian |

> 在原作者 shskjw 实现基础上重构模块化、增强并发控制与智能裁剪算法，并保留原作者署名。

---

## 安装

```bash
pip install -r requirements.txt
```

| 依赖 | 用途 |
|---|---|
| `Pillow` | 图像处理（必需） |
| `numpy` | 智能裁剪 / 加速运算（强烈建议） |
| `imageio` + `imageio-ffmpeg` | 视频转 GIF/APNG（视频功能必需） |

安装后请**重载插件**或重启 AstrBot。

---

## 指令

发送 **`gif帮助`** 查看运行时帮助。

### 动图

| 指令 | 说明 |
|---|---|
| `视频转gif [参数]` | 回复视频或附链接 → GIF/APNG/WebP |
| `/g加速 [倍数]` / `g加速` | 加快 GIF（默认 2x） |
| `/g减速 [倍数]` / `g减速` | 减慢 GIF |
| `gif分解` | 拆成静态帧（最多回传 20 帧） |
| `合成gif` / `合成1gif` / `合成2gif` | 精灵图网格合成动图 |
| `多图合成gif [间隔秒]` | 多张图合成动图 |

> 旧指令 `加速` / `减速` 会提示改用 `/g加速` `/g减速`。

**视频转gif 示例**

```text
视频转gif 2s-4.5s fps 15 0.5
视频转gif 开始 2 时长 3 fps10
视频转gif 1/3 0.4
```

- 时间：`1s-5.5s` 或 `开始 2 时长 3`
- 帧率：`fps 15` 或抽帧 `1/3`
- 缩放：`0.5`（0.1~1.0）

### 裁剪

| 指令 | 说明 |
|---|---|
| `自动裁切 [阈值] [模式] [降噪N]` | 去白边 / 透明边 |
| `裁剪 [行]x[列] [边距]` | 纯均分网格 |
| `智能裁剪 [行]x[列] [边距] [阈值]` | **内容缝智能裁剪（推荐 AI 九宫格）** |
| `批量去白边 [阈值]` | 多图批量去白边 |

**智能裁剪**

- 解决 AI 生图九宫格常见问题：行距不均、缝极窄、缝中有发丝/特效导致串行。
- 流程：内容掩膜 → 高密度行/列聚类 → 间隙谷值下刀 → 格内去白边。
- 示例：
  - `智能裁剪 3x3`
  - `智能裁剪 3x3 边距5`
  - `智能裁剪 3x3 245`
  - `智能裁剪 3x3 不去白边`

仅需严格等分时使用：`裁剪 3x3`。

**自动裁切示例**

```text
自动裁切
自动裁切 230 white
自动裁切 210 降噪5
```

### 特效

| 指令 | 说明 |
|---|---|
| `图片转线稿` | 素描线稿 |
| `表情包做旧 [次数]` | 电子包浆（1~50，建议 1~20） |

---

## 配置

见 `_conf_schema.json`（可在 AstrBot 插件配置面板修改）：

| 配置 | 默认 | 含义 |
|---|---|---|
| `output_format` | GIF | GIF / APNG / WEBP |
| `default_scale` | 0.3 | 视频转 GIF 默认缩放 |
| `default_fps` | 10 | 默认帧率 |
| `max_gif_duration` | 10 | 最大截取秒数 |
| `max_video_size_mb` | 50 | 视频体积上限 |
| `max_download_size_mb` | 50 | 下载体积上限 |
| `gif_max_colors` | 256 | GIF 调色板 |
| `crop_output_format` | PNG | 自动裁切输出 |
| `max_concurrent_tasks` | 2 | 并发任务上限 |
| `task_timeout_sec` | 120 | 单任务超时（秒） |
| `max_queue_waiting` | 8 | 排队上限 |

---

## 目录结构

```text
astrbot_plugin_gifcaijian/
├── main.py
├── metadata.yaml
├── requirements.txt
├── _conf_schema.json
├── README.md
├── LICENSE
├── 11/                     # 演示图
└── core/
    ├── animation.py
    ├── config_helpers.py
    ├── crop.py
    ├── deps.py
    ├── media_io.py
    ├── processors.py
    └── task_queue.py
```

---

## 使用提示

1. 多数指令需**回复**图片/视频，或与媒体同条发送。  
2. AI 九宫格优先：`智能裁剪 3x3`；简单等分：`裁剪 3x3`。  
3. 视频功能需安装 `imageio` 与 `imageio-ffmpeg`。  
4. 高负载时任务进入队列；队列满或超时会返回明确提示。

---

## 更新说明

### v1.7.0

- **智能裁剪**统一为内容聚类缝检测方案（原感知裁剪）
- 移除旧均分智能裁剪、感知裁剪、对比裁剪、预览裁剪等冗余指令
- 清理调试产物与冗余代码，重写 README

### v1.6.0

- 维护者变更为 Qiscard，保留原作者 shskjw
- `main.py` 模块化拆分至 `core/`
- `/g加速` `/g减速`；任务并发与超时
- 补充 `requirements.txt`

---

## 致谢

感谢原作者 **shskjw** 的初始实现与开源。
