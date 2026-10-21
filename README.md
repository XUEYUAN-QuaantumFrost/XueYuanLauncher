# XueYuan Launcher

一个用 Python 写的 Minecraft Java 版第三方启动器（类似 PCL / HMCL）。

## 功能

- **版本下载与安装**：官方源 / BMCLAPI 镜像 / MCBBS 镜像可切换，断点续传 + SHA-1 校验
- **三种登录方式**：离线模式 / 微软正版 OAuth / 外置登录（Authlib-Injector，支持 LittleSkin 等皮肤站）
- **启动游戏**：自动检测 Java、自动下载 Mojang 官方 Java 运行时、JVM/游戏参数自动组装、日志实时输出
- **模组加载器**：Fabric / Forge / NeoForge / Quilt 一键安装
- **整合包导入**：Modrinth（.mrpack）、MultiMC/HMCL（.zip）、CurseForge（.zip，模组下载需 API Key）
- **深色沉浸界面**：首页 / 下载 / 模组 / 设置 四个页面

## 在 Windows 上运行（开发模式）

1. 安装 Python 3.10+（安装时勾选 "Add to PATH"）
2. 双击 `build.bat`，脚本会自动装依赖并打包出 `dist\XueYuanLauncher.exe`
3. 或者手动运行：
   ```
   pip install -r requirements.txt
   python main.py
   ```

## 打包给玩家

跑完 `build.bat` 后，把 `dist\XueYuanLauncher.exe` 发给玩家即可，**对方不需要装 Python**。

## 使用说明

### 登录
- **离线模式**：填个名字直接进（最常用）
- **微软正版**：点登录 → 在弹层里点"获取授权链接" → 浏览器打开授权 → 授权完成后复制地址栏完整网址，粘贴回启动器 → 完成
- **外置登录**：填皮肤站账号 + 外置登录 API 地址（如 `https://littleskin.cn/api/yggdrasil`），首次启动会自动下载 authlib-injector

### 下载版本
下载页选择版本 → 点安装。国内网络慢时，在设置里把下载源切到 BMCLAPI 镜像。

### 模组
模组页选择已装版本 → 点加载器（Fabric/Forge/NeoForge/Quilt）→ 安装完成后，mods 文件夹就在游戏目录的 `mods/` 里，把模组 jar 丢进去即可。

### 整合包
- `.mrpack`（Modrinth）：直接选文件导入，自动装依赖
- `.zip`（MultiMC/HMCL 格式）：自动解析版本/加载器并合并数据
- `.zip`（CurseForge 格式）：合并 overrides 数据；模组自动下载需要 CurseForge API Key，请手动下载模组放入 mods/

## 常见问题

**找不到 Java？** 装一个 Java 17/21（推荐 Adoptium Temurin），或在设置里手动指定 java.exe 路径。1.17+ 版本也会尝试自动下载 Mojang 官方 Java 运行时。

**下载很慢 / 失败？** 设置里把下载源切到 BMCLAPI 或 MCBBS 镜像。

**启动后黑屏退出？** 打开日志看报错，常见原因：显卡驱动过旧 / Java 版本不对 / 内存分配过大。

## 项目结构

```
mc_launcher/
├── main.py            # 入口
├── main_window.py     # 无边框窗口 + WebEngine
├── bridge.py          # 前后端桥接
├── core/
│   ├── config.py      # 配置持久化
│   ├── downloader.py  # 下载引擎（多源）
│   ├── auth.py        # 三种登录
│   ├── launcher.py    # Java 检测与启动
│   └── mods.py        # 加载器与整合包
├── ui/index.html      # 深色界面
├── build.bat          # 一键打包
└── requirements.txt
```

## 合规说明

启动器本体只负责"管理 + 启动"，不内置任何破解、私服充值功能。游戏文件下载走官方或公开镜像源。
