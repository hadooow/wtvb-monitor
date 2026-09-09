# 用户 v0.4 发布包静态对照

来源为本次用户上传的 WTVB-Monitor-Windows11-v0.4-dashboard.zip。仅提取 PyInstaller 内嵌 PYZ，用 Python 3.13 marshal/dis 检查字节码；没有执行 EXE 或其模块代码。

- v0.4 EXE SHA256：a2b45d971a51c3d1cd64e77b64994ca41b30e0a9631c7ebab05742abf1232da8。
- 源码 ZIP 内另带 EXE SHA256：004791d7b2cf2f656857ca172b3748bde3ed4401f4977fb2712c6ee685aa4c67。两者不同；后者具有 CONNECTION_PROFILES 轮换逻辑，不能代表 v0.4。
- 两个 EXE 均打包 Python 3.13。v0.4 自带配置为串口 COM5、115200、连接超时 40 秒；实际使用端口应以现场设备管理器为准。

## v0.4 SerialGateway.connect

字节码读取 self.addresses.get(mac)。存在扫描地址时，生成以下字符串（timeout_ms 取配置秒数乘 1000）：

```text
AT+CONN={mac},{address[0]},{address[1]},247,{timeout_ms},1,40,20,0,600,1,1,0
```

无地址时：

```text
AT+CONN={mac},,,247,{timeout_ms},1,40,20,0,600,1,1,0
```

v0.4 start 顺序为 AT、AT+CNNI=、AT+SCAN=1；其 send 直接写串口，没有新版的逐命令响应等待。v0.6.3 仅优先恢复连接参数组合，保留新版队列与回复匹配，不宣称完整还原 v0.4。

## 与 v0.6.2 的差异

v0.6.2 首选普通/自动地址；虽有普通/扫描地址和配对/自动地址，但没有配对/扫描地址。新日志在 MTU23 普通连接阶段超时暂停，没有验证 v0.4 的完整组合。v0.6.3 将该组合放在首位，原五个兼容方案继续保留。

用户确认 v0.4 曾在家连接成功，但在车间也失败。所以上述差异是需要补齐的兼容项，并非车间故障的已证实唯一根因。此前约 57 秒才返回结果、超过软件 45 秒等待窗口的问题尚未解决；没有简单放宽所有超时来掩盖它。本次不更改硬件参数之外的等待策略。
