# WeChatAlbumReader

## 本工具全程使用Trae Work生成，是人工智能生成的代码。

# 打包说明

## 一键打包为 EXE

双击 `build_exe.bat` 即可自动使用 PyInstaller 打包，生成的 EXE 在 `dist\WeChatAlbumReader.exe`（约 13 MB）。

## 在目标电脑上使用

1. 将 `dist\WeChatAlbumReader.exe` 复制到任意文件夹
2. 直接双击运行，**不需要安装 Python**
3. 程序会自动打开浏览器访问 http://localhost:5000
4. 首次使用：通过 Web 界面的 `/config` 页面添加专辑参数（Biz、Album ID、名称）
5. 配置完成后，程序会自动抓取文章

## 文件结构

运行后会在同目录下自动创建：
- `articles.db` — 文章数据库
- `articles/` — 文章 HTML 文件
- `config.json` — 配置信息（专辑列表）

## 注意事项

- 第一次启动可能需要几秒钟
- Windows Defender 可能会提示风险，添加信任即可
- 端口固定为 5000，确保端口未被占用
- 关闭命令行窗口即可停止程序