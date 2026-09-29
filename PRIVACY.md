# 隐私政策 / Privacy Policy — Document OCR

> 适用审核项：**C3**（包内可查）、**C4**（要素完整）、**C5**（URL 可达）、
> **C8**（第三方去向与传输地域）。本文档随 deb 包分发，公网地址如下。

**公网地址 / Public URL**：<https://github.com/RyanYang163/shh14-ocr/blob/main/PRIVACY.md>
**生效日期 / Effective date**：2026-09-23
**适用版本 / Applies to**：1.0.025
**开发者 / Publisher**：shh
**包名 / Package**：`shh14-ocr`
**软件类型 / Type**：TerraMaster TOS 7 Deb 应用（WebUI 内嵌 / iframe）

---

## 1. 收集哪些数据 / Data Collected

**不收集任何个人数据。**

本应用不采集、不上传、不存储任何可以识别个人身份的信息，具体包括：

- 不收集使用统计、埋点、遥测、崩溃上报；
- 不收集设备标识、序列号、MAC 地址、主机名、IP 地址；
- 不收集账号、密码、邮箱或任何凭据；
- 不读取与功能无关的用户文件；
- 不生成用户画像，不做行为分析。

应用在运行期**只在本机**处理以下内容，且不离开你的 NAS：

| 处理内容 | 目的 | 是否离开设备 |
|---|---|---|
| 批量提取 PDF 的文字层与图片元数据，并建立本地检索索引 | 实现本应用功能 | **否** |
| 应用自身日志 | 排障（已对密码 / Token / API Key 做兜底脱敏） | **否** |

## 2. 数据保存期限 / Retention Period

| 数据 | 位置 | 保存期限 |
|---|---|---|
| 运行期数据（配置、任务队列、缓存） | `/Volume*/@apps/shh14-ocr/data/` | 随应用保留；缓存类可随时删除 |
| 应用日志 | 同上 `data/logs/` | 单文件 2 MB，轮转保留 5 个，超出自动删除 |
| 用户主动生成的结果文件 | 同上 `data/output/` | 由用户自行管理，应用不自动删除 |
| 用户业务数据 | 用户共享文件夹 | 应用**不修改、不删除**（本应用默认只读访问用户文件） |

**卸载时**：程序文件与专用用户被删除；`data/` 目录按设计**保留**（其中可能含你生成的结果
文件）。需要清理时手动执行 `sudo rm -rf /Volume*/@apps/shh14-ocr`。

## 3. 第三方共享与数据存放地域 / Third-Party Sharing and Data Location

**不与任何第三方共享数据。** 本应用不含统计 SDK、不含广告 SDK、不含崩溃上报服务。

- **数据存放地域**：全部数据存放在**你自己的 TNAS 设备本地**，不传输到任何境外或境内服务器。
- **出网行为**：核心功能**完全离线**，不产生任何出网请求。核心功能**完全离线**，不产生任何出网请求。本应用可选调用系统上已安装的 tesseract（本机程序，不出网）。仅当管理员**主动**在设置中配置了远程接口（管理员自行配置的远程接口）并启用时，应用才会向该接口发送必要的数据；不配置则始终离线。
- **第三方接收方**：无。

## 4. 数据安全措施 / Security Measures

- 专用非 root 系统用户运行（`User=` / `Group=` 与 `config.ini` 的 `user` 一致）；
- systemd 加固：`NoNewPrivileges`、`ProtectSystem=strict`、`ProtectHome`、`PrivateTmp`、
  `PrivateDevices`、`RestrictSUIDSGID`、`LockPersonality`、`RemoveIPC`、
  `SystemCallArchitectures=native`，可写路径以 `ReadWritePaths` 显式枚举；
- 不写 `/etc`、`/usr`、`/boot` 等系统目录；
- 进程间通信只经 Unix socket（`/var/api/shh14-ocr.sock`，mode `0660`），不监听任何宿主网络端口；
- 路径访问采用白名单 + `realpath` 校验，拒绝目录穿越与逃出白名单的 symlink；
- 日志写入前对密码、Token、API Key、Cookie、`Bearer` 凭据做兜底脱敏；
- 生命周期脚本不做任何网络操作（无 `apt` / `pip` / `curl`）；
- 不在包内放置任何预编译二进制，全部为可审计的源码。

## 5. 用户权利 / User Rights

- **知情权**：本政策与 README 说明应用处理的全部数据与落点。
- **选择权**：可访问目录白名单初始为空，是否授权完全由你决定，可随时增删。
- **访问与导出权**：所有数据都在你的设备上，随时可直接读取；应用内提供导出功能。
- **删除权**：删除 `data/` 目录即清除本应用产生的全部数据。
- **拒绝权**：不使用本应用即可；卸载后应用不再运行。

## 6. 数据删除途径 / Deletion Channel

| 想删除什么 | 怎么做 |
|---|---|
| 应用产生的运行数据 | 在应用内「设置 → 清理数据」执行，或手动删除 `/Volume*/@apps/shh14-ocr/data/` |
| 应用日志 | 随日志轮转自动清理，也可手动删除 `data/logs/` |
| 全部痕迹 | `sudo dpkg --purge shh14-ocr` 后执行 `sudo rm -rf /Volume*/@apps/shh14-ocr` |

## 7. 联系方式 / Contact

- 问题反馈 / Issue：<https://github.com/RyanYang163/shh14-ocr/issues>
- 开发者 / Publisher：shh
- 邮箱 / Email：surpasshither@outlook.com

---

> 本应用遵循「本地优先 / Local-first」原则：文件默认在你的 TNAS 本地处理，
> 不会自动上传到开发者服务器。若管理员配置了远程接口，数据处理方式会在设置页明确显示为「远程 API」；默认始终为「本地」。
