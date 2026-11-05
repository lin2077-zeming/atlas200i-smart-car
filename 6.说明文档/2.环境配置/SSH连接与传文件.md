# SSH 连接与文件传输

> **本文替代《SSH连接参考.pdf》。**
> 前作是针对 openKylin 写的，两个前提在 Atlas 200I DK A2（Ubuntu 22.04）上**都不成立**：
> ① 板子**默认已装好并启用了 openssh-server**，不需要像 openKylin 那样手动 `apt install`；
> ② 板子**自带静态 IP 192.168.137.100**，不需要用 Advanced IP Scanner 去扫。
>
> 本文覆盖：连通方式（3 种）→ SSH 客户端选择（含 FinalShell 的已知坑）→ 传文件（8 种方法）→ 免密 → 大文件实战 → 故障排查。

---

## 🔴 先读这一条：板子 IP 现在是 DHCP，会变

本文正文里大量使用 **`192.168.137.100`** —— 那是**网线直连 PC 时的静态地址**（PC 的
ICS 网关 `192.168.137.1`）。**只要插着网线，这个地址就一直有效。**

但板子现在还接了 **WiFi**（SSID `A102`），此时它的地址由路由器 DHCP 分配，
**会变**（2026-09-30 时为 `192.168.31.128`）。这种情况本文正文的 IP 就不适用了。

**找不到板子时的顺序：**

| 顺序 | 做法 | 命令 |
|---|---|---|
| ① | **PC 上跑现成脚本**（最省事）| 双击 `工具脚本\find_car.cmd` |
| ② | **板子上查自己**（最可靠，板子有屏幕）| `bash find_ip.sh` |
| ③ | 插网线走固定地址 | `ssh root@192.168.137.100` |
| ④ | 手动扫网段 | 见《快捷命令速查.md》**§0.1** |

`find_car` 按 **MAC 前缀**认板子（`ac:45:ef`=WiFi 网卡 / `a0:70:b7`=华为板载网卡），
比盲扫端口可靠。

> ⚠️ **别被 ARP 缓存骗了**：扫一次没找到 **≠** 板子没联网（踩过这个坑）。
> 加 `-Sweep` 重扫，或直接在板子上 `bash find_ip.sh`。

**让 IP 固定下来（推荐一次做好）**：在路由器管理页
（本项目是 `http://192.168.31.1`）做 **DHCP 静态 IP 绑定**：
`ac:45:ef:a6:36:c3` ↔ `192.168.31.128`。绑好后就不用再找了。

---

## 〇、结论速览

| 你的场景 | 直接看这一节 | 用什么 |
|---|---|---|
| 第一次连上板子 | §3.1 | 网线直连 + PC 设静态 IP |
| 想边开发边让板子能联网 | §3.2 | 网线接路由器 |
| 手头只有 Type-C 线 | §3.3 | Type-C 直连（192.168.0.2） |
| 还想继续用 FinalShell | §4.1 | **注意算法协商坑** |
| 传几百 MB～几 GB 的模型文件 | §7 | 千兆直连 + MobaXterm/rsync，**或直接上 NFS** |
| 传一堆零散小文件 | §5.4 | Git Bash 里 `tar` 管道 |
| 只想拖拽上传 | §5.5 / §4.4 | JupyterLab 网页 / VS Code 拖拽 |
| 人在宿舍、板子在实验室 | §9.2 | Tailscale + scp |
| 连不上，报错了 | §10 | 故障排查对照表 |

---

## 一、先搞清楚板子的网络长什么样

Atlas 200I DK A2 有三个可以当"网口"用的东西，出厂默认值如下：

| 接口 | 默认 IP | 说明 | 建议用途 |
|---|---|---|---|
| **eth1**（RJ45） | **192.168.137.100**（静态） | 出厂写死在 netplan 里 | **接 PC / 接笔记本，做主调试链路** |
| eth0（RJ45） | DHCP，默认无 IP | 需要路由器分配 | **接路由器，让板子上网** |
| usb0（Type-C） | **192.168.0.2**（静态） | 一根 Type-C 线直连 PC 时出现 | 应急 / 只带一根线时 |
| （Tailscale） | 100.x.x.x | 装完才有 | 异地访问 |

**登录凭据（出厂默认，务必尽快改掉）**

| 项 | 值 |
|---|---|
| 用户名 | `root`（或用 `HwHiAiUser`） |
| 密码 | `Mind@123` |
| SSH 端口 | 22 |

> **注意**：官方文档同时给出 `root` 和 `HwHiAiUser` 两个账号（密码相同）。**传文件、改系统配置建议直接用 `root`**，避免到处 `sudo` 时遇到目录权限问题。

**登进去之后，第一件事是确认自己到底在哪个口上：**

```bash
ip -br addr
# 期望看到类似：
# eth0    UP    192.168.1.27/24        ← 接路由器才有
# eth1    UP    192.168.137.100/24     ← 出厂默认
# lo      UNKNOWN  127.0.0.1/8
```

**分不清哪个 RJ45 是 eth1？** 两个办法：
1. 网线先插一个口，`ping 192.168.137.100`；不通就换另一个口（板子有 2 个网口，只有一个带默认静态 IP）。
2. 直接用 §3.2 的"接路由器"方案——不管插哪个口，从路由器后台都能看到板子被分配到的 IP。

---

## 二、SSH 服务本身要不要装？

**不用装。** Atlas 200I DK A2 官方 Ubuntu 镜像开箱即带 `openssh-server` 并已开机自启（官方快速入门就是用 MobaXterm 直接 SSH 登录的）。

只有当你把服务搞坏了，才需要登进板子本地终端（HDMI + 键鼠）修复：

```bash
sudo apt update && sudo apt install -y openssh-server
sudo systemctl enable --now ssh
sudo systemctl status ssh          # 看到 active (running) 即可
ss -tlnp | grep :22                # 确认 22 端口在监听
```

> **和前作的关键差异**：openKylin 需要"本地开终端装 SSH + 改密码"这一整套；Ubuntu 版板子全都不需要。这条经验**不要照抄**。

---

## 三、把 SSH 连通（三种拓扑，按推荐度排序）

### 3.1 方案 A：网线直连 PC（官方推荐，延迟最低最稳）

**拓扑**

```
笔记本 [以太网口] ──RJ45 网线── [eth1] Atlas 200I DK A2
       192.168.137.101                  192.168.137.100
```

**步骤**

1. 网线一头插 PC 的**以太网口**，一头插板子的 **eth1**。插好后**网口指示灯绿灯常亮、黄灯闪烁**即为连线正常。
2. 板子接 12V 电源开机（首次开机要等约 3 分钟才能远程登录，期间**不要断电**；之后每次约 1 分钟）。
3. 在 **PC** 上把以太网口设成同网段静态 IP：

   图形界面（Win10/11）：`设置 → 网络和 Internet → 高级网络设置 → 以太网 → 编辑` →
   `IP 分配 = 手动`，填：

   | 项 | 值 |
   |---|---|
   | IP 地址 | `192.168.137.101` |
   | 子网掩码 | `255.255.255.0`（或前缀长度 24） |
   | 网关 | **留空**（直连模式不需要） |
   | DNS | 留空 |

   命令行（管理员 PowerShell，二选一）：
   ```powershell
   New-NetIPAddress -InterfaceAlias "以太网" -IPAddress 192.168.137.101 -PrefixLength 24
   ```

4. 验证连通性：
   ```powershell
   ping 192.168.137.100
   ```
   通 → 进第 5 步。不通 → 见 §10。

5. 登录：
   ```bash
   ssh root@192.168.137.100
   ```

**优点**：跑到满速千兆（实测约 940 Mbps），传模型最快，不依赖任何外网设备。
**代价**：PC 的以太网口被独占，PC 自身的上网走 WLAN，互不影响。
**顺带**：这个 `192.168.137.x` 网段其实是 **Windows「Internet 连接共享（ICS）」的专用网段**——把 PC 的 WLAN 共享给"以太网"，PC 以太网口会自动变成 `192.168.137.1`，板子就能跟着上网了（配 Tailscale、apt 装包都要用）。这是官方的推荐思路，见 §3.4。

### 3.2 方案 B：网线接路由器（长期开发推荐）

**拓扑**

```
        ┌── 路由器 LAN 口 ── [eth0] Atlas 200I DK A2（DHCP 拿 IP，能上外网）
笔记本 ─┤
(WiFi)  └── 同一个路由器
```

**步骤**

1. 网线接板子 **eth0** ↔ 路由器 **LAN 口**。
2. 路由器后台把 LAN 设为自动、**开启 DHCP 服务**。
3. 在路由器后台的"已连接设备"/DHCP 列表里找板子的 IP（也可在板子本地终端 `ip -br addr` 查 eth0）。
4. `ssh root@<查到的IP>`。

**优点**：板子能上外网（apt 装包、pip 装 whl、跑 Tailscale 都靠它）；PC 无所谓有线无线都能连；IP 稳定后写进 FinalShell/MobaXterm 保存即可。
**缺点**：速率受路由器影响；如果板子和 PC 不在同一网段需要检查路由。

> **这是本项目最推荐的常态方案**：日常开发走路由器，需要传大文件时再临时拉一根网线走 §3.1 的直连。

### 3.3 方案 C：一根 Type-C 数据线（最省事）

用 Type-C 数据线连接板子的 Type-C 口和 PC，板子的 `usb0` 会以 `192.168.0.2` 出现：

```bash
ssh root@192.168.0.2
```

**注意事项**
- 必须用**数据线**，不能是只供电的充电线。
- Windows 端需要能识别这个虚拟网卡（RNDIS / CDC-NCM 驱动）。若没自动识别，需手动给新出现的网卡配置 `192.168.0.1/24`。
- 这条链路是 USB 网络模拟，**速率和稳定性都不如 RJ45**，适合应急和控制，不适合传大的模型文件。

### 3.4 三种拓扑对比

| | A 网线直连 | B 接路由器 | C Type-C 直连 |
|---|---|---|---|
| 板子默认 IP | 192.168.137.100 | DHCP 分配 | 192.168.0.2 |
| PC 需要配 IP | **要**（192.168.137.101） | 不用 | 有时要 |
| 板子能上外网 | ✗（除非开 ICS 共享） | **✓** | ✗ |
| 实际速率 | **~940 Mbps** | 看路由器 | 几百 Mbps 且不稳 |
| 适合传大文件 | **✓✓✓** | ✓ | ✗ |
| 适合日常开发 | ✗（占网口） | **✓✓✓** | ✓ |
| 需要的硬件 | 网线 + 有线网口 | 网线 + 路由器 | Type-C 数据线 |

---

## 四、SSH 客户端怎么选

先说结论：**FinalShell 能继续用，但如果连接时报 `Algorithm negotiation fail`，按 §4.1.2 处理即可。** 如果你不想折腾，直接换 MobaXterm（官方文档就是用它演示的，且内置 rsync，传大文件更省心）。

| 客户端 | 本机状态 | 传文件能力 | 推荐场景 |
|---|---|---|---|
| **FinalShell** | ✅ 已安装 | 内置 SFTP 面板，可拖拽 | 延续习惯，日常够用 |
| **MobaXterm** | 需下载 | **内置 SFTP + rsync**，官方推荐 | **传大文件首选** |
| 命令行 `ssh`/`scp`/`sftp` | ✅ Windows OpenSSH + Git Bash 都有 | 最强最可控 | 脚本化、批量 |
| **VS Code Remote-SSH** | VS Code 已装，**扩展未装** | 左侧资源管理器直接拖拽 | 边写代码边传文件 |
| WinSCP / FileZilla | 未安装 | 图形化 SFTP | 需要图形进度条时 |

### 4.1 FinalShell（沿用旧工具）

#### 4.1.1 配置步骤

1. 打开 FinalShell → 左上角 **"新建连接" → "SSH连接(Linux)"**。
2. 填写：

   | 字段 | 值 |
   |---|---|
   | 名称 | `Atlas 200I DK A2` |
   | 主机 | `192.168.137.100`（或路由器分配的 IP） |
   | 端口 | `22` |
   | 用户名 | `root` |
   | 密码 | `Mind@123`（改过就填新的） |
   | 编码 | **UTF-8**（很重要，见下） |

3. 点"确定"→ 双击连接。

#### 4.1.2 ⚠️ 已知问题一：`Algorithm negotiation fail`

**这是 FinalShell 换到 Ubuntu 22.04 后最容易碰到的坑，也是"能不能继续用 FinalShell"的关键。**

**原因**：Ubuntu 22.04 自带 OpenSSH 8.9，默认**已禁用 `ssh-rsa`（RSA/SHA1）签名算法**。而老版本 FinalShell 用的 Java SSH 库只支持 `ssh-rsa`，握手时双方谈不拢 → 报 `Algorithm negotiation fail`。

**解决（按推荐顺序）**

**解法 1：升级 FinalShell（首选，最干净）**
从 FinalShell 官网下载最新版覆盖安装。新版支持 `rsa-sha2-256/512`、`curve25519-sha256`、`ed25519`，直接就能连。**不要动板子配置。**

**解法 2：板子侧临时放宽算法（不升级客户端时用）**

登进板子（HDMI 本地终端，或如果还能连上就 SSH）：

```bash
sudo cp -a /etc/ssh/sshd_config /etc/ssh/sshd_config.bak
echo 'PubkeyAcceptedAlgorithms=+ssh-rsa' | sudo tee -a /etc/ssh/sshd_config
echo 'HostKeyAlgorithms=+ssh-rsa'       | sudo tee -a /etc/ssh/sshd_config
sudo systemctl restart ssh
```

仍连不上就再追加（更老的客户端只支持 SHA1 KEX）：

```bash
echo 'KexAlgorithms +diffie-hellman-group14-sha1,diffie-hellman-group1-sha1' | sudo tee -a /etc/ssh/sshd_config
echo 'MACs +hmac-sha1,hmac-sha1-96' | sudo tee -a /etc/ssh/sshd_config
sudo systemctl restart ssh
```

> **安全提醒**：这等于让板子的 SSH 接受已被淘汰的 SHA-1 算法。**只在校园内网、临时调试阶段使用**；项目演示/结题前建议恢复：
> ```bash
> sudo cp -a /etc/ssh/sshd_config.bak /etc/ssh/sshd_config && sudo systemctl restart ssh
> ```
> 并改用 SSH 公钥登录（§6）。

**解法 3：先用命令行验证是不是客户端的问题**

如果 `ssh root@192.168.137.100` 从 Windows 命令行能连上，而 FinalShell 连不上，那就**确定是客户端算法问题**，不要去改板子：

```bash
ssh -o PreferredAuthentications=password root@192.168.137.100
```

#### 4.1.3 已知问题二：中文乱码

FinalShell 的终端/文件名编码如果不是 UTF-8，看板子上的中文路径会变乱码，上传带中文名的文件也会失败。

- 会话属性里把编码设为 **UTF-8**。
- 文件名建议**统一用英文/数字**，尤其是模型文件（`Qwen2.5-0.5B-Instruct-Q4_K_M.gguf` 这类）。这同时也能避开跨系统编码问题。

#### 4.1.4 已知问题三：SFTP 面板传大文件中断

FinalShell 的 SFTP 对断点续传支持一般，传几百 MB 以上容易中断且要从头来。**大文件请走 §5.3（rsync）或 §5.6（NFS）**。

### 4.2 MobaXterm（官方推荐，传文件最省心）

官方文档就是用它演示 SSH 登录和上传下载的。它的优势是**左侧自带 SFTP 面板**，右上角有"上传/下载"按钮，点开选文件即可；而且**内置了 rsync**（MobaXterm 自带 Cygwin 环境），可以直接跑增量同步。

下载地址：MobaXterm 官网（Portable 版免安装，官方文档里用的就是 `MobaXterm_Personal_22.2.exe`）。

配置：`Session → SSH`，填 IP、勾选 `Specify username` 填 `root`，首次连接会弹信任提示点 `Accept`，然后输密码。

### 4.3 命令行（本机已确认可用）

本机环境已经验证过：

| 工具 | 位置 | 版本 |
|---|---|---|
| Windows OpenSSH | `C:\Windows\System32\OpenSSH\` | 含 `ssh.exe` / `scp.exe` / `sftp.exe` |
| Git Bash 自带 | `/usr/bin/` | OpenSSH_10.3p1，含 `ssh` / `scp` / `sftp` / **`ssh-copy-id`** / `ssh-keygen` |
| `rsync` | ❌ 未安装 | Windows 无原生 rsync，用 MobaXterm 里的，或见 §5.3 |

**在 PowerShell / CMD 里直接用即可：**

```powershell
ssh root@192.168.137.100
scp .\model.gguf root@192.168.137.100:/data/models/
```

### 4.4 VS Code Remote-SSH（写代码 + 传文件一体）

VS Code 已装，但 **Remote-SSH 扩展还没装**。装法：

1. 扩展市场搜 `Remote - SSH`（ID：`ms-vscode-remote.remote-ssh`）安装。
2. `F1` → `Remote-SSH: Connect to Host` → 输入 `root@192.168.137.100`。
3. 连上后**左侧资源管理器可以直接从 Windows 拖文件进板子的目录**，也会自动装好 aarch64 的 VS Code Server。

**注意**：VS Code Server 会占板子的 CPU 和内存（板子只有 4 核 @1.0GHz），**建议只在写代码/改配置时开**，跑 ASR/LLM 推理时断开，给推理让出算力。

---

## 五、传文件：8 种方法，按场景选

### 5.1 `scp` —— 最通用

```powershell
# PC → 板子：单个文件
scp .\qwen2.5-0.5b-instruct-q4_k_m.gguf root@192.168.137.100:/data/models/

# PC → 板子：整个目录
scp -r .\my_project\ root@192.168.137.100:/home/HwHiAiUser/

# 板子 → PC：拉回来
scp root@192.168.137.100:/home/HwHiAiUser/car_logs.tar.gz .\

# 非 22 端口用 -P（大写）
scp -P 2222 .\a.zip root@192.168.137.100:/tmp/
```

**要点**
- 路径里有空格/中文要加引号。
- 传**文本/代码**可以加 `-C` 开压缩；传 **`.gguf` / `.om` / `.zip` 这类已压缩的二进制，别加 `-C`**，纯浪费 CPU。
- `scp` **没有进度条**。要进度就用 `rsync --progress`（§5.3）或图形客户端。
- Windows 自带的 `scp` 在较新版本已改为走 SFTP 协议，兼容性比老版好。

### 5.2 `sftp` —— 交互式，适合边看边传

```bash
sftp root@192.168.137.100
# 连上后是一个交互 shell：
sftp> ls /data/models
sftp> cd /data/models
sftp> put .\model.gguf            # 上传
sftp> get /data/logs/app.log      # 下载
sftp> lcd D:\Downloads            # 切换本地目录
sftp> mkdir newdir
sftp> bye
```

### 5.3 `rsync` —— 大文件 / 断点续传 / 增量同步（推荐用于模型）

**这是传大模型最省心的方式**：断点续传、增量只传变化部分、有进度条、可限速。

Windows 上没有原生 rsync，用以下任一方式：

**方式 1：MobaXterm 的本地终端**（最省事）
打开 MobaXterm 的 Local terminal，直接跑：

```bash
rsync -avP --partial /drives/d/models/model.gguf root@192.168.137.100:/data/models/
```

**方式 2：在板子上跑 rsync，从 PC 拉**（PC 端需要能被板子访问到）
如果 PC 上有 Linux 虚拟机（你的 VMware Ubuntu 就在），就在虚拟机上跑 rsync 推到板子，这是最标准的用法。

**方式 3：装 cwRsync / 用 WSL**（不折腾就不必）

常用参数：

| 参数 | 作用 |
|---|---|
| `-a` | 归档模式（保留权限/时间戳） |
| `-v` | 显示详情 |
| `-P` | `--partial --progress`，断点续传 + 进度条 |
| `-z` | 压缩传输（只对文本有效） |
| `--exclude='*.tmp'` | 排除文件 |
| `--bwlimit=5000` | 限速 5 MB/s（避免占满带宽） |

### 5.4 `tar` 流式管道 —— Windows 上批量传小文件的利器

传几十个小文件时，`scp -r` 会为每个文件单独握手，非常慢。**打包成一条流传过去**快得多，而且**本机 Git Bash 可直接用**（已内置 `ssh` / `tar`）：

```bash
# PC(Git Bash) → 板子：把 D:\models\cfg 整个目录（不打中间文件）传过去
tar czf - -C "/d/models/cfg" . | ssh root@192.168.137.100 \
    "mkdir -p /data/models/cfg && tar xzf - -C /data/models/cfg"

# 反向：把板子的日志目录拉回 PC
ssh root@192.168.137.100 "tar czf - -C /home/HwHiAiUser logs" | tar xzf - -C "/d/backup"
```

**要点**：路径要写成 Git Bash 能懂的形式（`/d/models` 而不是 `D:\models`）；Windows 传过来的路径用引号包住。

### 5.5 JupyterLab 网页上传 —— 零安装，拖拽即用

**官方镜像自带 JupyterLab**，这是最直观的方式，特别适合传几个小文件、看图片、跑 notebook。

1. 在 PC 浏览器打开：`http://192.168.137.100:8888/lab`
2. 首次可能要求输入密码（用 `root` + 板子密码，或镜像初始密码 `Mind@123`）
3. 左侧文件浏览器有 **↑ 上传按钮**，也可以直接把文件**拖进文件列表**

**局限**：网页上传对单文件体积有限制（默认上限在百 MB 量级），传几百 MB 以上会失败；要放宽需改 Jupyter 配置 `ServerApp.max_body_size`。**大文件不要走这条路。**

### 5.6 NFS 共享目录 —— 沿用你已有的方案（推荐做长期配置）

你已经有《../6.历史资料/旧版目录/5.NFS共享文件系统配置/5.NFS共享文件系统配置.md》这一整套（Windows PC ↔ VMware Ubuntu ↔ 板子三方共享）。**这套在 Atlas 上完全可复用**，它是"频繁双向改文件"场景最优解——改完即生效，不用反复传。

唯一需要改的是客户端安装。Ubuntu 22.04（板子）侧：

```bash
sudo apt update && sudo apt install -y nfs-common
sudo mkdir -p /mnt/nfs_share
sudo mount -t nfs -o nolock,vers=3 192.168.31.20:/mnt/hgfs/VMshared /mnt/nfs_share
```

**必须注意的两个坑**
1. **netplan 的网段冲突**：NFS 服务器（你的 Ubuntu 虚拟机）和板子必须在**同一网段**。板子 eth1 出厂是 `192.168.137.100`，和常见的 `192.168.31.x` 不通。要么改板子 eth1 的 IP（§8.2），要么按《../6.历史资料/旧版目录/5.NFS共享文件系统配置/5.NFS共享文件系统配置.md》里那样做三设备桥接打通。
2. **开机自动挂载**：写进 `/etc/fstab` 时要加 `noauto,x-systemd.automount`，否则服务器不在线时板子**开机会卡住等超时**：
   ```fstab
   192.168.31.20:/mnt/hgfs/VMshared  /mnt/nfs_share  nfs  nolock,vers=3,noauto,x-systemd.automount,_netdev  0 0
   ```

**优势**：模型文件只存一份（放 PC 或虚拟机），板子直接读，不占板子 SD 卡空间（板子卡通常只有 32GB）。

### 5.7 临时 HTTP 服务器 —— 板子从 PC 下文件

适合"PC 上有文件，想让板子一条命令拉走"：

```bash
# PC（Git Bash）在文件所在目录执行
"C:/Users/LinZeming/.workbuddy/binaries/python/versions/3.13.12/python.exe" -m http.server 8000
# 或任意有 python 的环境：python -m http.server 8000
```

```bash
# 板子上执行
cd /data/models
wget http://192.168.137.101:8000/model.gguf
# 或 curl -O http://192.168.137.101:8000/model.gguf
```

**注意**：Windows 防火墙可能拦截入站 8000 端口，第一次运行会弹窗，选"允许"；或临时只对专用网络放行。

反过来（板子→PC）也同理，但更少见。

### 5.8 U 盘 / 拔 TF 卡 —— 最后的兜底

板子支持 USB 存储，官方文档专门写了 U 盘接入方法：

```bash
lsusb
fdisk -l                      # 看 U 盘设备名，比如 /dev/sda1
sudo mkdir -p /mnt/usb
sudo mount /dev/sda1 /mnt/usb
cp /mnt/usb/model.gguf /data/models/
sudo umount /mnt/usb
```

> **注意**：**不要把系统 TF 卡拔下来插到 Windows 上拷文件**——Windows 可能自动挂载并写入 `System Volume Information` 之类的元数据，也可能提示格式化，有搞坏分区的风险。要拔就先在板子上 `sudo poweroff` 安全下电。

### 5.9 八种方法对照

| 方法 | 大文件（>500MB） | 断点续传 | 需要装东西 | 图形界面 | 最适合 |
|---|---|---|---|---|---|
| §5.1 `scp` | 一般 | ✗ | ✗ | ✗ | 单个中小文件，脚本化 |
| §5.2 `sftp` | 一般 | 部分 | ✗ | ✗ | 交互式挑文件 |
| §5.3 `rsync` | **✓✓✓** | **✓** | 要（MobaXterm） | 部分 | **模型文件、反复同步** |
| §5.4 `tar` 管道 | ✓ | ✗ | ✗ | ✗ | **Windows 上批量小文件** |
| §5.5 JupyterLab | ✗ | ✗ | ✗ | **✓** | 零安装、随手传、看图片 |
| §5.6 NFS | **✓✓✓** | — | 要 | ✓ | **长期开发、双向频繁改** |
| §5.7 HTTP | ✓ | 部分 | ✗ | ✗ | PC 上的文件让板子来拉 |
| §5.8 U 盘 | ✓✓ | — | ✗ | ✗ | 完全连不上时的兜底 |

---

## 六、免密登录（配一次，长期省事）

每次传文件都输密码很烦。配公钥后，`scp` / `rsync` / VS Code / Gate 都能免密。

**在 PC 的 Git Bash 里操作**（已内置 `ssh-keygen` 和 `ssh-copy-id`）：

```bash
# 1) 生成密钥（已有 ~/.ssh/id_ed25519 就跳过，别重复覆盖）
ssh-keygen -t ed25519 -C "linzeming-atlas"

# 2) 把公钥装到板子上（这一步要输一次密码）
ssh-copy-id root@192.168.137.100

# 3) 验证：应该不再问密码
ssh root@192.168.137.100 "hostname; uptime"
```

**如果 `ssh-copy-id` 不好使，手动装：**

```bash
cat ~/.ssh/id_ed25519.pub | ssh root@192.168.137.100 \
    "mkdir -p ~/.ssh && chmod 700 ~/.ssh && cat >> ~/.ssh/authorized_keys && chmod 600 ~/.ssh/authorized_keys"
```

**装了公钥反而连不上？** 九成是权限问题，登进去修：

```bash
chmod 700 ~/.ssh
chmod 600 ~/.ssh/authorized_keys
chown -R root:root ~/.ssh
```

> **重要**：**Windows 上也别把私钥随便放**，并且**不要把私钥和密码写进任何提交到版本库的脚本**（前作的经验结论，继续遵守）。

---

## 七、大文件传输实战（模型文件场景）

### 7.1 铁律：**在 PC 上下模型，再传到板子**

板子直连下载（尤其是从 huggingface / github）经常慢到没法用。正确姿势：

1. PC 上先下好（国内可用镜像源，如 `hf-mirror.com` 替换 `huggingface.co`）。
2. 校验完整性（比对官方给的 SHA256 或文件大小）。
3. 走 §3.1 千兆直连 + §5.3 rsync 或 §5.6 NFS 传过去。

### 7.2 速率预期

| 链路 | 实测/理论速率 | 传 1 GB 大约需要 |
|---|---|---|
| 网线直连 eth1（千兆） | ~940 Mbps | **10–15 秒** |
| 接路由器（千兆 LAN） | 看路由器，一般 ~500–900 Mbps | 15–30 秒 |
| Type-C 直连 | 几百 Mbps 且波动 | 30 秒–数分钟 |
| TF 卡导数据（不推荐） | 20–90 MB/s | 15–60 秒 + 拔插风险 |

### 7.3 传到哪个目录？别传错地方

| 目录 | 用途 | 说明 |
|---|---|---|
| `/data/` | **推荐**。模型、数据集、日志 | 建议挂到 NVMe SSD（见 §8.3）；没 NVMe 就是 SD 卡上的普通目录 |
| `/home/HwHiAiUser/` | 代码、临时文件 | 官方样例代码在 `/home/HwHiAiUser/samples/` |
| `/tmp/` | 临时中转 | **重启就没了**，别长期放模型 |
| `/root/` | root 的家目录 | 用 root 登录时的默认落点 |

> **SD 卡系统分区不要塞满**：系统盘通常是 32GB SD 卡，塞几个 GB 的模型就满了，会导致系统异常。**优先 `/data`。**

### 7.4 传输后校验

大文件传完必须验一次，避免拿到半个文件白跑一轮推理：

```bash
# 两端都算一遍，比对
sha256sum /data/models/model.gguf

# 或至少比大小
ls -l /data/models/model.gguf
```

---

## 八、几项常用的一次性配置

### 8.1 改板子的主机名（便于在路由器/Tailscale 里认出来）

```bash
sudo hostnamectl set-hostname atlas-car
sudo sed -i 's/192.168.137.100/192.168.137.100 atlas-car/' /etc/hosts
```

之后 PC 的 `~/.ssh/config` 里加个别名，就能 `ssh atlas`：

```
Host atlas
    HostName 192.168.137.100
    User root
    ServerAliveInterval 30
    ServerAliveCountMax 3
```

> `ServerAliveInterval` 很有用：传到一半网卡休眠/断线时，连接不会被静默掐断。

### 8.2 改板子 eth1 的静态 IP（当需要和路由器/VM 同网段时）

```bash
ls /etc/netplan/                       # 文件名类似 01-netcfg.yaml
sudo cp /etc/netplan/01-netcfg.yaml /etc/netplan/01-netcfg.yaml.bak
sudo vi /etc/netplan/01-netcfg.yaml
```

把 eth1 的 `addresses` 改成目标 IP（例如 `192.168.31.60/24`），**YAML 缩进必须用空格、不能有 Tab**。保存后：

```bash
sudo netplan try      # 推荐：60 秒内不确认会自动回滚，防止改错失联
# 确认没问题再：
sudo netplan apply
```

> **改 IP 有失联风险**。如果是唯一一条连接，**先用 `netplan try`**，或在 `reboot` 前准备好 HDMI + 键鼠本地终端兜底。

### 8.3 挂载 NVMe SSD 到 `/data`（强烈建议）

板子只有 32GB 级 TF 卡，模型一多就爆。装 NVMe 后：

```bash
lsblk                                   # 找到 NVMe 设备，比如 /dev/nvme0n1
sudo mkfs.ext4 /dev/nvme0n1             # 首次使用才需要格式化，注意确认设备名别搞错
sudo mkdir -p /data
echo '/dev/nvme0n1  /data  ext4  defaults,noatime  0  2' | sudo tee -a /etc/fstab
sudo mount -a && df -h /data
```

### 8.4 Tailscale 远程访问（沿用旧方案）

你有现成的《内网穿透.md》，结论在 Atlas 上**完全适用**，直接照做：

```bash
# 板子上
curl -fsSL https://tailscale.com/install.sh | sh
sudo tailscale up --ssh
sudo tailscale ip -4                   # 记下 100.x.x.x
```

PC 上 Tailscale 已装且已在运行（1.102.3）。之后不论板子在哪：

```bash
ssh root@100.x.x.x
scp .\model.gguf root@100.x.x.x:/data/models/
```

**注意**：Tailscale 依赖板子能上外网。所以**先用 §3.2（接路由器）或 §3.1+ICS 把板子弄上网**，再装 Tailscale。

---

## 九、故障排查对照表

| 现象 | 可能原因 | 处理 |
|---|---|---|
| `ping 192.168.137.100` 不通 | PC 没设静态 IP | 回 §3.1 第 3 步 |
| | 网线插错 RJ45 口 | 换到另一个网口再试 |
| | PC 防火墙拦了 ICMP | 临时允许，或跳过 ping 直接试 `ssh` |
| | 板子还没启动完 | 首次开机等满 3 分钟 |
| `Connection refused` | sshd 没起 | 本地终端 `sudo systemctl enable --now ssh` |
| | 端口不是 22 | `ss -tlnp \| grep ssh` 看实际端口 |
| `Connection timed out` | IP 不在同网段 | 核对两边 IP 与掩码 |
| `Permission denied (publickey,password)` | 用户名不对 | 试 `root` 或 `HwHiAiUser` |
| | 密码错 | 默认 `Mind@123`，改过就用新的 |
| | root 密码登录被禁 | 本地终端检查 `/etc/ssh/sshd_config` 里 `PermitRootLogin` |
| **`Algorithm negotiation fail`** | **FinalShell 版本过老**（最常见） | **升级 FinalShell**，或按 §4.1.2 解法 2 放宽板子算法 |
| `WARNING: REMOTE HOST IDENTIFICATION HAS CHANGED` | 板子重装/换卡，主机指纹变了 | `ssh-keygen -R 192.168.137.100` 后重连 |
| 连上但**中文乱码** | 客户端编码不是 UTF-8 | 会话编码改 UTF-8；文件名尽量用英文 |
| `scp` 传一半卡住/断开 | 网卡休眠、链路抖动 | `~/.ssh/config` 里加 `ServerAliveInterval 30`；改用 `rsync -P` |
| 传大文件报 `No space left on device` | 塞到 SD 卡系统分区了 | 换到 `/data`；或挂 NVMe（§8.3） |
| JupyterLab 上传失败 | 超过单文件体积上限 | 大文件改用 scp/rsync |
| 板子上不了外网 | eth1 是静态无网关 | 走 §3.2 接路由器，或在 PC 上开 ICS 共享 |
| 板子能上网但**网关优先级不对** | 多网口默认路由冲突 | `route -n` 查，删掉多余的 default gw，只留一条 |
| NFS 挂载卡住/开机卡死 | 服务器不在线且写进了 fstab | fstab 加 `noauto,x-systemd.automount,_netdev` |

---

## 十、一键自检脚本

项目根目录已经放好了可直接运行的 **`check_board.sh`**，在 **PC 的 Git Bash** 里跑，一次把常见问题都排掉。

```bash
cd "C:/Users/LinZeming/Desktop/大创小车项目/昇腾开发板-大创小车"
bash check_board.sh                    # 默认 192.168.137.100
bash check_board.sh 192.168.1.27       # 接路由器时，填实际 IP
```

脚本内容如下（便于按需改动）：

```bash
#!/usr/bin/env bash
# 用法：bash check_board.sh [板子IP]
IP="${1:-192.168.137.100}"
USER="root"

echo "=== 1. 本机有线网卡 IP ==="
ipconfig | grep -A4 -E "以太网|Ethernet" | grep -E "IPv4|适配器|adapter"

echo
echo "=== 2. ping 板子 ==="
ping -n 2 "$IP" || echo "!! ping 不通：检查 PC 静态IP(192.168.137.101) 和网线"

echo
echo "=== 3. SSH 端口是否可达 ==="
timeout 5 bash -c "cat < /dev/null > /dev/tcp/$IP/22" \
  && echo "22 端口可达" \
  || echo "!! 22 不可达：sshd 未启动 / 端口不同 / 被防火墙拦"

echo
echo "=== 4. SSH 登录 + 板子信息 ==="
ssh -o ConnectTimeout=8 "$USER@$IP" '
  echo "主机名 : $(hostname)"
  echo "系统   : $(. /etc/os-release; echo $PRETTY_NAME)"
  echo "内核   : $(uname -r)"
  echo "架构   : $(uname -m)"
  echo "--- 网口 ---"; ip -br addr | grep -v "^lo"
  echo "--- 磁盘 ---"; df -h / /data 2>/dev/null
  echo "--- 内存 ---"; free -h | head -2
  echo "--- NPU  ---"; npu-smi info 2>/dev/null | head -12 || echo "npu-smi 无输出，检查驱动"
'

echo
echo "=== 5. 传输测速（上传 10MB 随机文件并计时）==="
head -c 10485760 /dev/urandom > /tmp/_spd.bin
time scp -q /tmp/_spd.bin "$USER@$IP:/tmp/_spd.bin" && echo "上传完成"
ssh "$USER@$IP" "rm -f /tmp/_spd.bin"; rm -f /tmp/_spd.bin
```

**判读标准**
- 第 1–3 步全绿 → 链路没问题，问题在客户端软件（多半就是 §4.1.2）。
- 第 4 步看到 `npu-smi` 有芯片信息 → 板子状态正常。
- 第 5 步 10MB 上传在 **1 秒内** → 千兆链路正常；超过 5 秒 → 你走的是 Type-C 或 WiFi，传大文件前先换到有线直连。

---

## 十一、最终推荐组合

综合成本、稳定性、和你现有工具链，建议这样配：

| 场景 | 方案 |
|---|---|
| **日常连通** | 网线接路由器（§3.2）+ 板子装 Tailscale（§8.4），写进 PC 的 `~/.ssh/config` |
| **传输大文件** | 临时拉一根网线走直连（§3.1）+ MobaXterm 的 rsync（§5.3），或直接 NFS（§5.6） |
| **传零散小文件** | Git Bash 的 `tar` 管道（§5.4），比 `scp -r` 快得多 |
| **随手拖一个文件** | JupyterLab 网页上传（§5.5） |
| **写代码** | VS Code Remote-SSH（§4.4），用完断开给推理让算力 |
| **终端** | FinalShell 升级到最新版继续用；连不上就按 §4.1.2 排查 |
| **兜底** | HDMI + 键鼠本地终端 / U 盘 / Type-C 直连 |

---

## 附：常用命令速查

```bash
# 登录
ssh root@192.168.137.100

# 上传 / 下载
scp .\a.gguf  root@192.168.137.100:/data/models/
scp -r .\proj\  root@192.168.137.100:/home/HwHiAiUser/
scp root@192.168.137.100:/data/logs.tar.gz  .\

# 免密
ssh-keygen -t ed25519 -C "linzeming-atlas"
ssh-copy-id root@192.168.137.100

# 清理旧指纹
ssh-keygen -R 192.168.137.100

# 增量同步（MobaXterm 本地终端或 Linux 端）
rsync -avP --partial /path/model.gguf root@192.168.137.100:/data/models/

# 批量小文件（Git Bash）
tar czf - -C "/d/models/cfg" . | ssh root@192.168.137.100 "mkdir -p /data/models/cfg && tar xzf - -C /data/models/cfg"

# 板子信息
ip -br addr
df -h
free -h
npu-smi info
systemctl status ssh
ss -tlnp | grep :22
```

---

## 相关文档

| 文档 | 关系 |
|---|---|
| 《SSH连接参考.pdf》 | **已被本文替代**（针对 openKylin，两个前提在 Atlas 上不成立） |
| 《内网穿透.md》 | 完全沿用，见本文 §8.4 |
| 《../6.历史资料/旧版目录/5.NFS共享文件系统配置/5.NFS共享文件系统配置.md》 | 完全沿用，见本文 §5.6（注意 netplan 网段冲突） |
| 《VNC远程桌面.md》 | 沿用，但 `~/.vnc/xstartup` 里的 `exec ukui-session` 需改成 `exec gnome-session` |
| 《../1.总览与架构/GUI开发研究报告.md》 | 其 FinalShell 配置与连接步骤可继续参考；**"必须退出 conda 环境"的结论仍适用** |
| 《../1.总览与架构/项目总览导读.md》 | 本文对应的平台方案总纲（§5.2 系统烧录、§5.3 远程调试链路） |
