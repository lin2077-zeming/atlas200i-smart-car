由于我们使用的是 **openKylin** 操作系统，而不是 Raspberry Pi OS。这意味着我们**不能**使用 `raspi-config` 工具，也不能使用 Raspberry Pi OS 默认集成的 RealVNC。需要使用 Linux 通用的 VNC 解决方案。

对于 openKylin (一个基于 Linux 的发行版)，最常用和最适合无头模式的 VNC 服务器是 **TigerVNC** 

请通过ssh终端执行以下步骤

## 安装TigerVNC服务器
 
1.首先，更新软件包列表，然后安装 `tigervnc-standalone-server`。openKylin 使用 `apt` 作为包管理器

```bash
sudo apt update
sudo apt install tigervnc-standalone-server
```

2.设置VNC密码
安装完成后，需要运行一次 `vncserver` 命令来设置一个专用于 VNC 连接的密码

在终端输入
```bash
vncserver
```

然后系统会提示设置密码： `You will require a password to access your desktops.` 我们输入一个 6-8 位的密码

系统会询问是否设置一个 "view-only" (仅查看) 密码，一般选 `n` (否) 即可

第一次运行时，它会自动创建一个默认配置文件，并启动一个 VNC 会话（可能在 `:1` 端口上）。我们先把它停掉，以便进行下一步配置
```bash
vncserver -kill :1
```

## (关键) 配置 VNC 以启动 openKylin 桌面

VNC 需要知道在用户连接时要启动哪个桌面环境。这通过修改 `~/.vnc/xstartup` 文件来完成

1.使用`vim`编辑这个文件

```bash
vim ~/.vnc/xstartup
```

2.你会见到一个空文件夹

![unnamed.png](https://gitee.com/magic486/note/raw/master/image/unnamed.png)

按下i键，左下角会出现插入的字样，此时可以编辑该文件

可以复制该代码，在终端里鼠标右键即可将脚本粘贴进去

```bash
#!/bin/sh

# VNC 远程桌面
unset SESSION_MANAGER
unset DBUS_SESSION_BUS_ADDRESS

# 加载 X 资源
[ -r $HOME/.Xresources ] && xrdb $HOME/.Xresources

# 设置灰色背景
xsetroot -solid grey

# 启动 VNC 的图标（可选，但推荐）
vncconfig -iconic &

# 关键：启动 openKylin (UKUI) 桌面环境
exec ukui-session
```

如果点右键复制之后，左下角变成了插入可视，意味着 `vim` **拦截**了你的鼠标点击，并进入了“可视选择模式”，而不是执行了“粘贴”操作，这是因为 `vim` 默认启用了鼠标支持，它把你的右键点击当成了 `vim` 内部的命令。

解决：
退出当前模式，回到普通模式，依次输入`:set mouse=`。这个命令告诉vim：不管鼠标点击。

3.保存文件并退出
回到普通模式，输入`:wq`，即可将文件保存并推出

4.给予脚本权限
```bash
chmod +x ~/.vnc/xstartup
```
5.启动VNC服务器
在ssh终端输入
```bash
# (以防万一有残留，先kill一次)
vncserver -kill :1

# 启动vnc服务，指定分辨率
vncserver :1 -geometry 1280x720 -depth 24 -localhost no
```
6.确定树莓派VNC的服务状态
在ssh终端输入
```bash
ss -tlnp | grep 5901
```
![image.png](https://gitee.com/magic486/note/raw/master/image/20251020100107878.png)

这表示 VNC 在监听所有 IP 地址，这是正确的
如果你看到的是 `127.0.0.1:5901`，那表示 VNC **只**监听本地连接，这是**错误**的。你需要停止它 (`vncserver -kill :1`)，然后确保你启动时**没有**加 `-localhost` 参数

## 在电脑上连接 VNC

1.首先下载安装VNC客户端，这里下载**TigerVNC Viewer** (官方配套)

2.打开VNC Viewer，在地址栏中，输入树莓派的 **IP 地址** 和 **端口号**，格式为： `[树莓派的IP地址]:5901`

3.点击连接。系统会提示你输入在之前第二步中设置的 VNC 密码。

4.如果一切顺利，你应该能看到 openKylin 的桌面了。

![image.png](https://gitee.com/magic486/note/raw/master/image/20251020102313612.png)


## 设置为开机自启动

1.创建一个 `systemd` 服务文件。**注意：** 把 `<USER>` 替换为你的用户名 (例如 `pi` 或 `kylin)
```bash
sudo vim /etc/systemd/system/vncserver@.service
```
vim文件的修改操作和前面一样

2.将以下内容粘贴进去。**请务必修改 `<USER>` 为你的实际用户名！**
```bash
[Unit]
Description=Start TigerVNC server at startup
After=syslog.target network.target

[Service]
Type=forking
User=<USER>
Group=<USER>
WorkingDirectory=/home/<USER>

PIDFile=/home/<USER>/.vnc/%H:%i.pid

ExecStartPre=-/usr/bin/vncserver -kill :%i > /dev/null 2>&1
ExecStart=/usr/bin/vncserver -depth 24 -geometry 1280x720 -localhost no :%i
ExecStop=/usr/bin/vncserver -kill :%i

[Install]
WantedBy=multi-user.target
```

3.保存并退出

4.让 `systemd` 重新加载配置
```bash
sudo systemctl daemon-reload
```

5.启用这个服务（我们使用屏幕 `:1`）
```bash
# 这将使 vncserver@1.service 在开机时启动
sudo systemctl enable vncserver@1.service
```

现在，VNC 服务会在树莓派每次启动时自动运行，你可以随时通过 VNC Viewer 连接了，到此，我们已经完成了所有步骤。

## 可能的报错
### 1.
![1.png](https://gitee.com/magic486/note/raw/master/image/1.png)
这个错误**不是**“连接超时”或“连接被拒绝”（说明树莓派那边没开），而是你的 **Windows 系统**阻止了 **TigerVNC Viewer 这个软件**“向外”发起网络连接

1.关闭第三方杀毒/安全软件，如火绒等

2.Windows Defender 防火墙，打开“**Windows Defender 防火墙**”，点击“**允许应用或功能通过 Windows Defender 防火墙**”，依次点击“**更改设置**”，“允许其他应用”，添加“**TigerVNC Viewer**”
![image.png](https://gitee.com/magic486/note/raw/master/image/20251020101712615.png)
### 2.
![image.png](https://gitee.com/magic486/note/raw/master/image/20251020102443847.png)
出现这个错误就是树莓派拒绝了来自计算机的请求，说明前面的配置出现了问题，得回去查看，是否有步骤出错





