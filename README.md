# 雪原启动器（XueYuan Launcher）

一个用 Python 写的 Minecraft Java 版第三方启动器（类似 PCL / HMCL）。

当前版本：**v1.5（雪原启动器）**。

## v1.5 更新

- 正式改名「雪原启动器」：窗口标题、界面标题、exe 名称都换了
  （发布文件夹保持英文 `XueYuanLauncher`，见下方打包说明）。
- **Forge 改用官方安装器**（`forge-installer.jar --installClient`），装完校验依赖库，
  修掉了"装好了、一启动退出码 1"的问题。
- **CurseForge 整合包自动下载模组**（不需要 API Key）：cfwidget 查文件名 +
  CurseForge 官方 CDN；`mediafilez` 被限流时自动切 `edge` 域名；
  按 modid 去重（重复移到 `mods-disabled/`）；文件被作者删除时用同版本最新文件替代。
- 安装进度条真正动起来（修了 v1.3 就存在的元素 id 拼错问题），并加了顶部全局进度条、
  百分比和当前文件名。
- 离线用户名合法性校验（只允许字母/数字/下划线），中文名会在进世界前拦下并提示。
- 内存按物理内存 65% 设上限，避免 `-Xmx` 超过物理内存导致卡顿/内置服务器掉线。
- 打包脚本重写为 `build.bat` + `build.py`（纯 ASCII + CRLF，避免 cmd 解析中文出错）。
- 安装路径含中文时弹窗说明并退出，不再 Qt 崩溃闪退。

## v1.4 修复清单

**界面交互（原本完全失效，也是最影响使用的问题）**

- QtWebEngine 默认不实现网页的 `confirm / prompt / alert`：
  v1.3 里点「安装」触发的确认框永远返回"否"，所以点了没反应；
  「浏览」目录按钮也弹不出输入框。现在由主窗口接管这三个对话框，
  使用系统原生的确认框 / 输入框。
- QtWebChannel 里带返回值的接口是**异步**的（返回 Promise），
  v1.3 的 `JSON.parse(bridge.get_init())` 实际上抛异常被吞掉，
  所以首页 Java / 内存 / 已装版本列表 / 设置页在启动后一直是空的。
  现在前端统一用 `await` 调用，初始化一定成功。
- 「整合包导入」在 v1.3 里拿到的是网页的假路径（`File.path` 在
  QtWebEngine 里不存在），必然报"文件不存在"。现在改用系统文件对话框。
- 关闭窗口后重新打开会记住大小 / 位置 / 是否最大化；无边框窗口右下角可以拖拽缩放。

**启动游戏**

- v1.3 生成的启动命令其实忽略了「设置里的 Java 路径」和自动检测结果
  （`get_minecraft_command` 没拿到 `executablePath`），选了 Java 也没用。已修复。
- 按版本 json 的 `javaVersion` 校验主版本：版本要 Java 21 就先自动下载官方运行时；
  1.12 / 1.16 这类老版本会优先挑 Java 8，避免"用 Java 21 跑 1.12 直接崩"。
- 老版本（1.6 及以前）的 `--assetsDir` 自动指向 `resources/` 或
  `assets/virtual/<index>`，声音 / 语言文件不再丢失。
- 打开 `enableLoggingConfig`，游戏日志会正常出现在启动器日志抽屉里。
- 游戏输出额外落一份文件到 `.minecraft/logs/xueyuan-<版本>.log`，
  即使 log4j 没写成 `latest.log`，关掉启动器也能回看。
- 离线用户名按游戏内部规则校验（字母 / 数字 / 下划线，≤16）：
  中文 / 空格 / 符号会让游戏进世界时报 `Invalid characters in username` 并断开，
  现在登录时和启动前都会拦下，并自动弹出登录框让人改名。
- 内存分配按物理内存的 65% 设上限（例如 11.8GB 内存 → 最多 7680MB），
  避免 `-Xmx` 超过物理内存把整机拖垮、内置服务器卡到掉线。
- 停止游戏先温和结束，8 秒没退出再强杀。

**下载 / 安装**

- 所有请求带 UA、连接/读取超时、失败自动重试；不再"卡住没反应"。
- 断点续传：先写 `.part`，sha1 校验通过才改名；中断后重试接着下。
- 校验失败会自动删掉重下，不会把坏文件留给游戏。
- 依赖库按平台规则过滤（含 os.version / arch 判断），
  正确跳过其它平台的专属文件，也正确解压 Windows natives。
- 资源文件并发下载后，按索引的 `virtual` / `map_to_resources` 标记
  补齐 `assets/virtual/<index>` 和 `resources/`（老版本必需）。
- 依赖库下载失败会明确报错（列出失败文件 + 建议换源），不再"假装装完"。
- 安装时自动下载对应版本的 Mojang 官方 Java 运行时。
- 镜像源可用性实测：BMCLAPI 的清单 / 资源 / 依赖库改写均正常。

**登录**

- 外置登录（LittleSkin 等）真正走 Yggdrasil `/authserver/authenticate`：
  用账号密码换 accessToken，拿真实角色 UUID，并用正确的
  `-Dauthlibinjector.yggdrasil.prefetched`（Base64 会话 JSON）注入。
  v1.3 只是把 token 写成 "1" 且 UUID 为空，进服务器会被当成离线账号。
- 启动前自动调用 `/authserver/refresh` 续期；失败也不阻塞启动。
- 微软登录的 refresh_token 持久化，每次启动自动续期。
- 密码只用于换令牌，不会写进配置文件。

**模组**

- 加载器安装统一走 `minecraft-launcher-lib` 8 的 `mod_loader` 接口，
  Fabric / Forge / NeoForge / Quilt 都可用。
- **NeoForge 国内可用**：`maven.neoforged.net` 在国内经常直接超时（实测连不上），
  现在选择 BMCLAPI 源时，版本列表和安装器都走 BMCLAPI 的 maven 镜像
  （`bmclapi2.bangbang93.com/maven`），NeoForge 安装由启动器直接执行
  `installer --install-client` 完成；Forge 的 metadata / 安装器地址同样支持镜像。
- Forge / NeoForge 的版本列表按版本号语义排序（maven metadata 是字符串排序，
  v1.3 直接取会拿到很旧的版本），现在默认装最新的构建。
- 装加载器前先确认母版本已安装；安装器本身需要 Java，现在会自动准备。
- **Forge 改用官方安装器**：Forge 1.13+ 需要在本机生成 srg / extra / patched client 几个 jar，
  之前交给库自己拼，网络抖一下就漏文件，表现为"装好了、一启动退出码 1"。
  现在直接执行 `forge-installer.jar --installClient`（支持 BMCLAPI maven 镜像），
  装完还会校验依赖库是否齐全，缺了会自动再修一次。
- **CurseForge 整合包自动装模组**：manifest 里只有 projectID/fileID，没有文件名，
  现在用 cfwidget 的公开接口解析文件名，再走 CurseForge 官方 CDN 直链，
  **不需要 API Key**；下载完按 modid 去重（重复的移到 `mods-disabled/`）。
- 整合包导入时如果版本/加载器/模组安装失败，会明确报错并列出缺失模组链接，
  不再"显示装完了但一启动就退出"。
- 整合包导入后会自动安装对应的 MC 版本与加载器，导完就能启动；
  zip 解压做了路径校验（防目录穿越）。

**其它**

- Java 自动检测兼容 Mojang 的两种运行时目录结构
  （`runtime/<组件>/<平台>/<组件>/bin/java.exe` 和 `runtime/<组件>/bin/java.exe`），
  已经装过官方运行时的机器可以直接识别。
- 配置损坏会自动备份成 `config.json.broken` 并用默认值启动；保存改为原子写。
- 旧版本写下的空游戏目录会被纠正为默认 `.minecraft`（原来会把游戏文件装到启动器目录里）。
- 程序目录不可写时，配置自动回退到 `%APPDATA%\XueYuanLauncher`。
- 前端 JS 报错会写进 `error.log`，排查问题不用靠猜。
- 依赖版本锁定：`minecraft-launcher-lib>=8.0,<9.0`、`PyQt5 / PyQtWebEngine <5.16`。

## 功能

- **版本下载与安装**：官方源 / BMCLAPI 镜像 / MCBBS 镜像可切换，断点续传 + SHA-1 校验
- **三种登录方式**：离线模式 / 微软正版 OAuth / 外置登录（Authlib-Injector，支持 LittleSkin 等皮肤站）
- **启动游戏**：Java 自动检测与匹配、自动下载 Mojang 官方 Java 运行时、JVM/游戏参数自动组装、日志实时输出
- **模组加载器**：Fabric / Forge / NeoForge / Quilt 一键安装
- **整合包导入**：Modrinth（.mrpack）、MultiMC/HMCL（.zip）、CurseForge（.zip，自动下载模组，无需 API Key）
- **深色沉浸界面**：首页 / 下载 / 模组 / 设置 四个页面

## 在 Windows 上运行（开发模式）

1. 安装 Python 3.10+（安装时勾选 "Add to PATH"）
2. 双击 `build.bat`（内部调用 `build.py`），自动装依赖并打包出 `dist\XueYuanLauncher\雪原启动器.exe`
3. 或者手动运行：
   ```
   pip install -r requirements.txt
   python main.py
   ```

> 没有装 Java 也能用：下载版本时启动器会自动装好 Mojang 官方 Java 运行时。

## 打包给玩家

跑完 `build.bat` 后，把 `dist\XueYuanLauncher\` 整个文件夹压缩发给玩家即可，
**对方不需要装 Python**（第一次启动需要联网下载游戏和 Java）。

> **注意：解压到的文件夹路径不能含中文。** Qt 5.15 的网页引擎在中文路径下会崩溃，所以发布文件夹固定叫 `XueYuanLauncher`（exe 才叫 `雪原启动器.exe`）。玩家如果把它放进中文目录，启动器会弹窗提示移动位置，而不是无声闪退。

## 使用说明

### 登录
- **离线模式**：填个名字直接进（最常用）
- **微软正版**：点登录 → 点「微软正版」→ 点授权链接（会在浏览器打开）→ 授权完成后
  复制地址栏完整网址，粘贴回启动器 → 完成登录
- **外置登录**：填皮肤站账号 + 密码 + API 地址（如 `https://littleskin.cn/api/yggdrasil`），
  首次启动会自动下载 authlib-injector

### 下载版本
下载页选择版本 → 点安装 → 确认。国内网络慢时，在设置里把下载源切到 BMCLAPI 镜像
（安装过程中可以在首页右下角「打开日志」看进度）。

### 模组
模组页选择已装版本 → 点加载器（Fabric/Forge/NeoForge/Quilt）→ 安装完成后会生成一个新版本，
回首页选中它启动即可；mods 文件夹在游戏目录的 `mods/` 里，把模组 jar 丢进去即可。

### 整合包
- `.mrpack`（Modrinth）：直接选文件导入，自动装依赖与模组
- `.zip`（MultiMC/HMCL 格式）：自动解析版本/加载器，合并数据并安装对应版本
- `.zip`（CurseForge 格式）：合并 overrides、安装对应版本与加载器，并按 manifest 自动从 CurseForge 下载模组（不需要 API Key）

## 常见问题

**找不到 Java？** 不用自己装：1.17+ 版本会自动下载官方运行时，1.12/1.16 也会自动准备
Java 8。想手动指定就在设置里选 java.exe。

**下载很慢 / 失败？** 设置里把下载源切到 BMCLAPI 镜像；失败会明确提示是哪个文件、
哪个地址出错，已下载的文件不会重复下载。

**装 NeoForge 提示没有可用版本？** NeoForge 1.20.1 的安装器只发布在官方 maven
（maven.neoforged.net），国内经常连不上；1.20.1 建议用 Forge，1.20.2 及以上
（以及 2026 年起的 26.x 版本）可通过 BMCLAPI 镜像正常安装 NeoForge。
Forge / Fabric / Quilt 不受影响。

**进单人世界后立刻「无法连接至服务器 / 连接中断」？**
这是离线用户名不合法导致的。游戏内部只允许 **英文字母 / 数字 / 下划线，最多 16 位**，
名字里带中文、空格或符号时，客户端能启动，但一进世界内置服务器就会断开，
日志里会写 `Internal Exception: java.lang.IllegalStateException: Invalid characters in username`。
点右上角「登录」把名字改成英文（例如 `Steve`、`XueYuan`）再启动即可。
现在启动器会在登录时和启动前各校验一次，并自动弹出登录框。

**内存分配被自动调小了？** 启动器会按物理内存的 65% 给 `-Xmx` 设上限
（例如 11.8GB 内存的机器最多 7680MB），分超过物理内存只会让整机变卡、
内置服务器更容易卡死。想恢复默认就点「保存设置」，滑块会显示当前上限。

**启动后黑屏退出？** 打开日志看最后几行报错，常见原因：显卡驱动过旧 / 手动指定的 Java
版本不对（用「自动检测」会重新匹配）/ 内存分配过大。游戏日志同时会写到
`.minecraft/logs/xueyuan-<版本>.log`，启动器关掉之后也能回看。

**界面第一页数据是空的？** 确认用的是 v1.4（v1.3 的初始化接口调用方式有问题，
首页和设置页会显示不出来）。

## 项目结构

```
XueYuan_Launcher/
├── main.py            # 入口（日志重定向、异常兜底）
├── main_window.py     # 无边框窗口 + WebEngine + alert/confirm/prompt 接管
├── bridge.py          # 前后端桥接（原生对话框、线程任务、忙碌锁）
├── core/
│   ├── config.py      # 配置持久化（原子写、空值纠正、目录回退）
│   ├── downloader.py  # 下载引擎（多源、超时重试、断点续传、natives/资源）
│   ├── mirrors.py     # 国内镜像适配（Forge / NeoForge 的 maven 与安装器）
│   ├── auth.py        # 三种登录（微软 OAuth、Yggdrasil 外置登录）
│   ├── launcher.py    # Java 匹配 / 运行时下载 / 启动与进程管理
│   ├── curseforge.py  # CurseForge 整合包模组下载（无需 API Key）
│   └── mods.py        # 加载器安装与整合包导入
├── ui/index.html      # 深色界面
├── build.bat          # 一键打包（双击即可，内部调用 build.py）
├── build.py           # 打包逻辑（输出 dist\XueYuanLauncher\雪原启动器.exe）
└── requirements.txt
```

## 合规说明

启动器本体只负责"管理 + 启动"，不内置任何破解、私服充值功能。
游戏文件下载走官方或公开镜像源。
