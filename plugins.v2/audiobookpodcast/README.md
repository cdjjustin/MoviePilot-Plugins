# AudiobookPodcast — 有声书播客插件

将本地有声书目录生成 **iOS 播客（Apple Podcasts）兼容的 RSS 2.0 订阅源**，无需任何中间服务器，直接由 MoviePilot 提供文件服务。

---

## 功能特性

| 功能 | 说明 |
|------|------|
| 目录自动扫描 | 每个子目录视为一本书/一档播客节目 |
| 多层目录支持 | 支持 CD1/CD2 等子文件夹结构 |
| 封面自动识别 | 检测目录中的 cover/folder/front/artwork 图片 |
| 音频时长 | 借助 mutagen 读取，写入 `<itunes:duration>` |
| 流式传输 | 支持 HTTP Range 请求，iOS 播客可拖拽进度条 |
| 路径安全 | 路径归一化 + `relative_to` 检查，防目录遍历 |
| API 鉴权 | 使用 MoviePilot API Token（apikey 模式） |

---

## 目录结构约定

```
<audiobook_path>/
├── 三体/
│   ├── cover.jpg          ← 封面（可选）
│   ├── 001-第一章.mp3
│   └── 002-第二章.mp3
├── 活着/
│   ├── CD1/
│   │   └── 01.mp3
│   └── CD2/
│       └── 01.mp3
└── 散装.mp3               ← 根目录音频归入"杂项"播客
```

---

## 配置说明

| 配置项 | 必填 | 说明 |
|--------|------|------|
| 有声书根目录 | ✅ | 本地路径，如 `/mnt/audiobooks` |
| 文件稳定等待（秒） | ❌ | 过滤最近修改的音频（默认 60），避免下载未完成导致偶发播放失败；0 表示不过滤 |
| MoviePilot 外部访问地址 | ✅ | iOS 设备可访问的地址，如 `http://192.168.1.100:3001` |
| 播客作者 | 可选 | 显示在播客 App 中的作者名 |
| 默认封面图 URL | 可选 | 书目录无本地封面时使用的网络图片 |

---

## 订阅方式

### iOS 播客 App

1. 打开"播客" → 搜索栏右上角"…" → **通过 URL 收听**
2. 输入订阅地址：

```
http://<server_url>/api/v1/plugin/AudiobookPodcast/feed?book=<书名>&apikey=<API密钥>
```

> API 密钥在 MoviePilot 后台 → 设置 → 安全 中查看。  
> 插件详情页会直接列出每本书的完整订阅地址，可一键复制。

### 其他支持 RSS 的播客客户端

同上，任何支持自定义 RSS 源的播客 App（如 Pocket Casts、Overcast、AntennaPod）均可使用该地址。

---

## API 接口

所有接口路径前缀：`/api/v1/plugin/AudiobookPodcast`，需要在 Query 中传 `apikey=<API密钥>`。

| 方法 | 路径 | 说明 |
|------|------|------|
| GET | `/books` | 列出所有有声书及其订阅地址（JSON） |
| GET | `/feed?book=<书名>` | 返回指定有声书的 RSS XML |
| GET | `/audio?book=<书名>&file=<文件路径>` | 流式返回音频/封面文件 |

---

## 支持的音频格式

`.mp3` `.m4a` `.m4b` `.mp4` `.aac` `.ogg` `.opus` `.flac` `.wav` `.wma` `.aiff`

---

## 开发与部署

```
plugins.v2/
└── audiobookpodcast/
    ├── __init__.py         # 插件主类 AudiobookPodcast
    ├── requirements.txt    # mutagen>=1.47.0
    └── README.md           # 本文件
package.v2.json             # 插件市场索引
```

将本仓库地址配置到 MoviePilot 插件市场的第三方源即可安装。
