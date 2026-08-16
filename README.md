# WeChatAlbumReader 微信公众号专辑自动收集工具

> [!WARNING]
> **本工具由 AI 生成**
> 本工具全程使用 Trae Work 生成，是人工智能生成的代码，使用前请自行审查代码逻辑。

## 功能简介

定时抓取微信公众号专辑中的最新文章，自动保存为本地 HTML 文件，方便离线阅读和归档。

## 快速开始

### 一键打包为 EXE

```bash
# 双击运行即可
build_exe.bat
```

PyInstaller 会自动打包，生成的 EXE 位于 `dist\WeChatAlbumReader.exe`（约 13 MB）。

### 在目标电脑上使用

1. 将 `dist\WeChatAlbumReader.exe` 复制到任意文件夹
2. 双击运行，**无需安装 Python**
3. 程序自动打开浏览器，访问 `http://localhost:5000`
4. 首次使用：通过 Web 界面 `/config` 页面添加专辑参数（Biz、Album ID、名称）
5. 配置完成后，程序自动开始抓取文章

## 文件结构

运行后会在同目录下自动创建：

```
项目目录/
├── articles.db       # 文章数据库
├── articles/         # 文章 HTML 文件
└── config.json       # 配置信息（专辑列表）
```

## 注意事项

- **首次启动**：可能需要几秒钟时间
- **安全提示**：Windows Defender 可能提示风险，添加信任即可
- **端口占用**：端口固定为 5000，请确保该端口未被占用
- **停止运行**：关闭命令行窗口即可停止程序

## 技术栈

- **后端**：Python + Flask
- **打包**：PyInstaller
- **生成工具**：Trae Work

## 许可

本项目仅供学习研究使用。