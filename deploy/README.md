# 部署到 Vultr

`deploy/vultr/setup.sh` 在一台新的 Vultr 服务器上把整套装好：后端、前端、先例库（USPTO 专利 + academic13k 期刊反应）、
AiZynthFinder 模型和 ZINC 在售清单。数据都从公开来源下载，在服务器上导入。可以重复运行：再运行一次就是更新到 GitHub 上最新的 master，
数据库、账号和密钥都留着。

## 1. 准备服务器

- 系统选 **Ubuntu 24.04 x64**（别的系统脚本也会自己装 Python 3.12，但 24.04 最省事）。
- 配置至少 **4 核 8 GB**，16 GB 更稳：一次逆合成搜索要占两三个 GB 内存，后端加先例库索引还要一两个 GB。
  内存不到 16 GB 时脚本会加 4 GB 交换空间。
- 地点：用户在加拿大的话选 Toronto。
- 防火墙：Vultr 的 Firewall Group 要么不选，要么放开 SSH 22、HTTP 80、HTTPS 443。脚本自己还会在系统里开 ufw，只放这三个端口。

## 2. 运行安装脚本（二选一）

**已经有服务器：** 在 Vultr 网页上打开这台机器，点右上角的 **View Console**，用 `root` 和 Overview 页上的密码登录，输入这一行回车：

```bash
curl -fsSLo /root/setup.sh https://raw.githubusercontent.com/RP-12000/Syntherisk-Technology/master/deploy/vultr/setup.sh && bash /root/setup.sh
```

**新建服务器：** 先在 Vultr 的 Orchestration → Startup Scripts 新建一个脚本（Type 选 Boot），内容是：

```bash
#!/bin/bash
curl -fsSLo /root/setup.sh https://raw.githubusercontent.com/RP-12000/Syntherisk-Technology/master/deploy/vultr/setup.sh && bash /root/setup.sh
```

然后 Deploy 新机器时在 Startup Script 一栏选上它。机器开起来就自动开始装。

第一次要一到两个小时，大部分时间在导入 175 万条专利反应。看进度：在 View Console 里输入

```bash
tail -f /var/log/syntherisk-setup.log
```

最后出现“完成”和网址就装好了，在浏览器里打开 `http://服务器IP`。期刊论文的年份、题目在那之后还会在后台补一阵（不影响使用）。

## 3. 装好以后

| 要做的事 | 命令（在服务器上用 root） |
|---|---|
| 看后端日志 | `journalctl -u syntherisk -f` |
| 重启后端 | `systemctl restart syntherisk` |
| 更新到最新版本 | 再运行一次第 2 步的那一行 |
| 看补年份的进度 | `journalctl -u syntherisk-fill-literature` |

**打开大模型功能（一句话要求、说明文字、问答）：** 用 `nano /etc/syntherisk/syntherisk.env` 把 `LLM_PROVIDER=offline` 改成
`LLM_PROVIDER=anthropic`，再加一行 `LLM_API_KEY=你的 key`（要指定型号的话加 `LLM_MODEL=…`），保存后 `systemctl restart syntherisk`。
这个文件里还有脚本随机生成的 `SECRET_KEY` 和 `DATA_ENCRYPTION_KEY`：不要改，也不要删。改了 `DATA_ENCRYPTION_KEY`，库里加密的数据就再也读不出来了。

**HTTPS：** 要先有一个域名指向这台服务器，然后：

```bash
apt-get install -y certbot python3-certbot-nginx
certbot --nginx -d 你的域名
```

再把 `/etc/syntherisk/syntherisk.env` 里的 `CORS_ORIGINS` 改成 `https://你的域名`，重启后端。

## 4. 东西都放在哪

| 位置 | 是什么 |
|---|---|
| `/opt/syntherisk/backend` | 后端代码（这个仓库的 master） |
| `/opt/syntherisk/venv` | Python 3.12 虚拟环境 |
| `/var/lib/syntherisk` | 数据：`syntherisk.db`（账号、分析、先例库都在里面）、`files/`（报告和附件）、`aizynth/`（模型和在售清单） |
| `/var/www/syntherisk` | 前端页面 |
| `/etc/syntherisk/syntherisk.env` | 设置和密钥 |
| `/etc/nginx/sites-available/syntherisk` | nginx：前端页面，`/api` 转给后端 |

要备份的只有 `/var/lib/syntherisk` 和 `/etc/syntherisk/syntherisk.env`。

## 5. 前端

`deploy/web` 是前端仓库（syntherisk-app）`npm run build` 的产物，脚本把它拷到 `/var/www/syntherisk`。
前端改了以后：在前端仓库里 `npm run build`，把 `dist/` 里的东西拷到这里替换掉 `deploy/web` 的内容，提交推送，再在服务器上运行一次第 2 步。
