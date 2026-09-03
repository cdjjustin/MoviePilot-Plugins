# AudiobookOrganizer — 有声书刮削整理插件

从豆瓣和喜马拉雅刮削有声书元数据，批量整理本地文件：重命名、目录结构、ID3 标签、封面下载。

## 功能特性

| 功能 | 说明 |
|------|------|
| 目录扫描 | 子目录 = 一本书，支持 CD1/CD2 多层结构 |
| 元数据刮削 | 豆瓣（书籍信息）+ 喜马拉雅（分集/朗读者） |
| 批量预览 | 整理前预览所有变更，确认后执行 |
| 整理方式 | 硬链接（默认，不影响做种）/ 复制 / 移动 |
| 文件重命名 | 可配置命名模板 |
| 标签写入 | 复制/移动模式写入；硬链接模式跳过（保护做种哈希） |
| 目录监控 | 定时扫描，高置信度自动整理或通知 |
| 本地降级 | 刮削失败时按目录名/文件名整理（可开关） |

## 目录结构约定

### 整理前（源目录）

```
<source_path>/
├── 三体 128kbps/
│   ├── 001-第一章.mp3
│   └── 002-第二章.mp3
└── 盗墓笔记/
    ├── 039.第二季.第002集.xxx.mp3
    └── ...
```

### 整理后（目标目录，硬链接模式）

源目录文件**保持不动**（继续做种），目标目录通过硬链接呈现标准结构：

```
<target_path>/                    <source_path>/（做种目录，不变）
└── 刘慈欣/                       └── 三体 128kbps/
    └── 三体/                         ├── 001-第一章.mp3  ← 硬链接
        ├── cover.jpg                 └── 002-第二章.mp3  ← 硬链接
        ├── S01E01 - 第一章.mp3  ←─┘
        └── S01E02 - 第二章.mp3  ←─┘
```

> 硬链接与源文件共享同一份数据，**不会写入音频标签**（避免改变文件哈希导致做种失败）。
> 如需带标签的独立副本，请使用「复制」模式。

## 配置说明

| 配置项 | 必填 | 说明 |
|--------|------|------|
| 有声书源目录 | ✅ | 待整理的原始目录（做种目录） |
| 整理输出目录 | ✅（硬链接模式） | 整理后的标准目录，供播客插件使用 |
| 整理方式 | ❌ | 硬链接（默认）/ 复制 / 移动 |
| 命名模板 | ❌ | 默认 `{author}/{title}/S{season:02d}E{episode:02d} - {episode_title}{ext}` |
| 数据源优先级 | ❌ | 喜马拉雅优先 / 豆瓣优先 / 仅手动 |
| 豆瓣/喜马拉雅 Cookie | ❌ | 防反爬，可选 |
| 目录监控 | ❌ | 定时扫描 + 通知或自动整理 |
| 无刮削时本地整理 | ❌ | 默认开启；刮削失败时按本地目录/文件名整理 |

## API 接口

路径前缀：`/api/v1/plugin/AudiobookOrganizer`，需 Bearer 认证。

| 方法 | 路径 | 说明 |
|------|------|------|
| GET | `/scan` | 扫描源目录 |
| GET | `/search?keyword=` | 搜索元数据 |
| POST | `/preview` | 预览整理计划 |
| POST | `/apply` | 执行整理 |
| GET | `/history` | 操作历史 |

### 预览请求示例

```json
{
  "book_id": "abc123",
  "source": "ximalaya",
  "source_id": "12345678"
}
```

### 执行整理示例

```json
{
  "plan_ids": ["plan_id_1", "plan_id_2"]
}
```

## 与有声书播客插件联动

整理完成后，将 `AudiobookPodcast` 插件的「有声书根目录」指向本插件的**整理输出目录**，即可自动生成 iOS 播客 RSS 订阅源。

## 开发与部署

### MoviePilot V3（推荐）

```
plugins.v3/
└── audiobookorganizer/
    ├── __init__.py
    ├── models.py
    ├── scanner.py
    ├── namer.py
    ├── tagger.py
    ├── organizer.py
    ├── scrapers/
    ├── pyproject.toml
    └── README.md
package.v3.json
```

将本仓库地址配置到 MoviePilot **V3** 插件市场的第三方源后刷新市场，即可看到并安装「有声书刮削整理」。

### V3 市场不显示时排查

1. **确认 `PLUGIN_MARKET` 包含本仓库**（设定 → 系统 / `app.env`）：
   ```
   https://github.com/cdjjustin/MoviePilot-Plugins
   ```
   多个地址用英文逗号分隔。本仓库不在官方 Wiki 默认清单里，必须手写进去。
2. **强制刷新**插件市场（不要只点普通同步）。合并 `package.v3.json` 之前若已同步过，宿主可能把该文件的 404 缓存约 30 分钟。
3. **网络与 Token**：能访问 `raw.githubusercontent.com`；建议配置 `GITHUB_TOKEN`，国内建议配置 `GITHUB_PROXY`。
4. 在市场搜索框搜：`有声书刮削` 或 `AudiobookOrganizer`。
5. 浏览器直接验证索引是否可读：
   `https://raw.githubusercontent.com/cdjjustin/MoviePilot-Plugins/main/package.v3.json`

### MoviePilot V2

仍可通过 `plugins.v2/` + `package.v2.json` 安装旧版（v1.0.2）。V3 宿主请使用本目录的 V3 实现。
