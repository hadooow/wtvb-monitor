# 工业无线温振监测系统

面向 WTVB01-BT50 温振传感器和 EW-DTU02 蓝牙转串口网关，目标运行环境为 Windows 11 x64。

## v0.6.0

- 默认连接超时由 20 秒改为网关测试手册建议的 40 秒。
- 优先使用手册的普通连接指令，失败后再尝试扫描地址、MTU23 和配对扩展参数。
- 使用串口指令队列，连接前停止扫描，等待连接结果及结束标记后再继续。
- 保留跨串口读取超时的半行数据；裸 `ERROR` 关联到当前连接。
- 保存滚动日志，首页可一键下载诊断 ZIP。
- 默认登记 CBF0～CBF6；CBF1 为 `C2:37:21:02:DE:EF`，旧数据库启动时也会一次性补入。
- 分别显示串口打开、网关回复、蓝牙连接与有效数据状态。
- 提供 Windows 自动打包、可执行文件模拟采集自检和 Release 工作流。

这些改动修正了代码与网关资料之间的差异，不代表已经确认现场唯一根因。当前没有现场网关/传感器，实际射频连接仍需按《现场测试说明》复测。仓库只有一个原始提交，未取得独立的 0.4 源码，不能完整进行 0.4/0.5 差异对比。

## 下载运行（发布完成后）

1. 打开 https://github.com/hadooow/wtvb-monitor/releases 。
2. 在 v0.6.0 的 Assets 中下载 `WTVB-Monitor-v0.6.0-windows-x64.zip`。GitHub 自动提供的 `Source code` 是源码，不能直接当作 EXE 运行。
3. 右键 ZIP → 全部解压，建议解压到有写入权限的目录，例如 `D:\WTVB-v0.6.0`。
4. 打开解压后的 `WTVB-Monitor` 文件夹，双击 `WTVB-Monitor.exe`。保留整个文件夹及 `_internal` 子目录，无需安装 Python。
5. 程序自动打开 `http://127.0.0.1:8000`；未自动打开时可手动输入。保留程序窗口，关闭窗口会停止采集。
6. 点击“采集设置”，选择真实 EW-DTU02 网关。按设备管理器中实际端口设置 COM 号，默认 COM3；波特率默认 115200，需与网关一致；连接超时建议 40 秒。

如果 Releases 尚没有上面的 ZIP，说明 Windows 构建和发布还未完成，可以先按下面的源码方式运行。当前没有硬件时，选择模拟网关即可验证界面、存储和日志；模拟数据不能用于判断蓝牙是否已经修好。

## 查看日志

日志自动开启，无需额外操作。

- **最方便：** 首页点击“下载诊断日志”，浏览器下载 `WTVB-diagnostics-v0.6.0.zip`。其中含 `diagnostics.json`（版本、配置及状态快照）和 `logs/monitor.log*`。
- **本地查看：** 打开 EXE 同目录的 `logs\monitor.log`，用记事本或 VS Code 查看。源码运行时位于项目根目录的 `logs`。
- **实时查看：** 在程序目录打开 PowerShell，执行：

```powershell
Get-Content .\logs\monitor.log -Tail 100 -Wait
```

日志中的 `TX` 是发给网关的指令，`RX` 是完整原始回复；`CONNECT` 是连接方案；`First valid sensor sample` 表示首次解析出有效温振数据。日期时间使用电脑本地时间。单文件约 5 MiB，最多保留当前文件及 5 份备份，总计约 30 MiB；长期运行会覆盖最旧日志，所以现场失败后及时下载。

`AT_RESPONSE_TIMEOUT` 表示指令回复没有完整结束。为避免把迟到回复分配给下一条指令，程序暂停继续发指令。先下载日志，再到采集设置点击“按已保存设置重新连接网关”。`TIMEOUT` 则是网关明确返回的蓝牙连接超时，会正常轮换方案重试。

## 保留旧数据升级

先退出新旧程序，备份旧版整个文件夹。将旧版 `data` 文件夹复制到新版 EXE 同目录，以保留设备、阈值和历史记录。CBF1 会自动补入；已有同 MAC 的名称和启用状态不会被覆盖。之后主动删除 CBF1，不会在下次启动时重新出现。

建议使用新版自带的 `config/settings.json` 并在界面重新选择端口。若复制旧配置，原有 20 秒超时会被保留，请在采集设置中改为 40 秒。不要同时运行两个实例访问同一串口或同一数据库。

## 源码运行（Win11）

安装 Python 3.12 x64，下载源码并解压，在项目目录打开 PowerShell：

```powershell
py -3.12 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe -m app
```

首次安装依赖需要联网。界面、SQLite 数据保存和日志均在本机运行。网页选择模拟网关后，会以预置设备演示模拟数据；真实模式按 MAC 采集，不向数据库写入假零。

## 本地制作 Windows 发布包

在 Windows 11 x64 源码目录执行：

```powershell
.\.venv\Scripts\python.exe -m pip install -r requirements-dev.txt
.\build_release.bat
```

脚本先运行测试，再用 PyInstaller 打包，复制静态资源及现场配置，在临时副本中自动运行 EXE 验证模拟采集、首页及日志下载。成功后生成 `release\WTVB-Monitor-v0.6.0-windows-x64.zip` 和 SHA256 文件。临时自检数据不会装进发布包。

## GitHub 自动发布（维护者）

将代码推送后，可在 Actions → Windows release → Run workflow 手动构建；成功后在该次运行的 Artifacts 下载结果，这种方式不会创建 Release。

正式发布时，确保版本号与标签相同，然后：

```powershell
git tag v0.6.0
git push origin main
git push origin v0.6.0
```

标签触发 Windows 构建，通过测试与 EXE 自检后自动创建 Release 并上传 ZIP 和校验文件。推送工作流需要仓库相应写入权限。仅上传源码不会自动产生可执行文件。

也可以向 `main` 推送提交说明含 `[release]` 的发布提交。工作流会构建该提交，在全部检查通过后按应用版本号创建标签和 Release。普通 `main` 提交只构建，不发布；已存在的同名 Release 不会被自动覆盖。

## 协议与范围

依据本次提供的《网关测试指导手册 V1.0》第 3.4 节、《AT 指令说明 V1.5》第 1.12、1.13、1.18 节修正网关交互。手册的普通连接示例为：

```text
AT+CONN=C2372102DEEF,,,247,40000,1,40,20,0,600
```

其中 notify 开关为 1，连接操作超时为 40000 ms；不在普通方案中附加配对扩展参数。不会自动恢复出厂、清除绑定或写入传感器寄存器。

传感器官方文档：https://wit-motion.yuque.com/wumwnr/docs/zzn7g2vtt9eeokcb 。本次环境无法读取该页面，故保留原有 `55 61` 测量帧解析；若网关连接成功但始终没有有效数据，请提供该页面 PDF 及诊断 ZIP，以核对现场型号的 GATT/通知和数据协议。
