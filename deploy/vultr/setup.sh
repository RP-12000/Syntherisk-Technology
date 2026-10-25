#!/usr/bin/env bash
# 在一台新的 Vultr 服务器上把 Syntherisk 整套装好：后端、前端、先例库、逆合成模型。用 root 运行，可以重复运行。
#
# 用法（任选一种，详见 deploy/README.md）：
#   1. 在 Vultr 网页的 View Console 里用 root 登录，输入一行（先存成文件再运行。不要写成 curl … | bash：
#      那样脚本里会读标准输入的命令可能把脚本后面的内容吃掉）：
#        curl -fsSLo /root/setup.sh https://raw.githubusercontent.com/RP-12000/Syntherisk-Technology/master/deploy/vultr/setup.sh && bash /root/setup.sh
#   2. 在 Vultr 建一个 Startup Script（Boot 类型），内容是 #!/bin/bash 加上面那一行，部署新机器时选上它。
#
# 装好以后：
#   后端      /opt/syntherisk/backend（GitHub 上这个仓库的 master），虚拟环境 /opt/syntherisk/venv
#   数据      /var/lib/syntherisk（VAR_DIR：syntherisk.db、files/、aizynth/）
#   前端      /var/www/syntherisk（本仓库 deploy/web 里的构建结果）
#   设置      /etc/syntherisk/syntherisk.env（第一次运行时生成，SECRET_KEY 和 DATA_ENCRYPTION_KEY 随机生成、不打印）
#   服务      syntherisk.service（uvicorn，只监听 127.0.0.1:8000；分析队列在这个进程里，所以只开一个进程）
#   网页      nginx 监听 80 端口：前端页面；/api、/health 转给后端（不缓冲，进度推送要实时）
#   日志      本脚本：/var/log/syntherisk-setup.log；后端：journalctl -u syntherisk
#
# 数据都从公开来源下载，在服务器上导入（第一次运行要一到两个小时，大部分时间在导入 175 万条专利反应）：
#   USPTO 专利反应（Lowe，CC0）       https://doi.org/10.6084/m9.figshare.5104873 的 1976_Sep2016_USPTOgrants_smiles.7z
#   学术文献反应 academic13k（CC BY 4.0）  https://doi.org/10.5281/zenodo.23097616
#   AiZynthFinder 公开模型和 ZINC 在售清单  aizynthfinder 自带的 download_public_data
# 大模型的 API key 不在这里设置（LLM_PROVIDER=offline）；要用的话见 deploy/README.md。
set -euo pipefail

LOG=/var/log/syntherisk-setup.log
if [ -z "${SYNTHERISK_LOGGING:-}" ]; then
  export SYNTHERISK_LOGGING=1
  # 把自己的全部输出同时写进日志文件
  exec > >(tee -a "$LOG") 2>&1
fi
step() { echo; echo "==== $(date -u +%H:%M:%SZ) $*"; }

APP_USER=syntherisk
HOME_DIR=/opt/syntherisk
APP_DIR=$HOME_DIR/backend
VENV=$HOME_DIR/venv
VAR_DIR=/var/lib/syntherisk
WEB_DIR=/var/www/syntherisk
ENV_FILE=/etc/syntherisk/syntherisk.env
REPO=https://github.com/RP-12000/Syntherisk-Technology.git
USPTO_URL=https://ndownloader.figshare.com/files/8664379
USPTO_MD5=b0d94806fe7479fad6db9b894cff858e
ACADEMIC_URL=https://zenodo.org/api/records/23097616/files/reactionSmilesAcademicDOI2026version2.txt/content
ACADEMIC_MD5=861cd7eb79419b168d13c3032d7d36c6

step "系统软件"
export DEBIAN_FRONTEND=noninteractive
# 新开的机器第一次启动时，系统自己的自动更新可能正占着 apt：最多等 15 分钟
apt-get -o DPkg::Lock::Timeout=900 update -y
apt-get -o DPkg::Lock::Timeout=900 install -y git curl nginx ufw build-essential p7zip-full ca-certificates \
  libx11-6 libxext6 libxrender1 zlib1g fonts-wqy-microhei

step "Python 3.12（aizynthfinder 4.4.1 要求低于 3.13）"
if command -v python3.12 >/dev/null 2>&1; then
  apt-get -o DPkg::Lock::Timeout=900 install -y python3.12-venv python3.12-dev || true
  PY=$(command -v python3.12)
else
  if ! command -v uv >/dev/null 2>&1 && [ ! -x /root/.local/bin/uv ]; then
    curl -LsSf https://astral.sh/uv/install.sh | sh
  fi
  UV=$(command -v uv || echo /root/.local/bin/uv)
  UV_PYTHON_INSTALL_DIR=/opt/python "$UV" python install 3.12
  PY=$(UV_PYTHON_INSTALL_DIR=/opt/python "$UV" python find 3.12)
fi
"$PY" --version

step "交换空间（内存不到 16 GB 时加 4 GB，免得一次搜索把进程挤掉）"
if [ ! -f /swapfile ] && [ "$(free -g | awk '/Mem:/{print $2}')" -lt 15 ]; then
  fallocate -l 4G /swapfile && chmod 600 /swapfile && mkswap /swapfile && swapon /swapfile
  grep -q '^/swapfile' /etc/fstab || echo '/swapfile none swap sw 0 0' >> /etc/fstab
fi
free -g

# 用 syntherisk 这个用户跑一条命令（HOME 指到它自己的目录，git、pip 才不会去读 root 的配置和缓存）
as_user() { runuser -u $APP_USER -- env HOME=$HOME_DIR "$@" </dev/null; }

step "用户和目录"
id -u $APP_USER >/dev/null 2>&1 || useradd --system --home-dir $HOME_DIR --shell /usr/sbin/nologin $APP_USER
mkdir -p $HOME_DIR $VAR_DIR/files $VAR_DIR/aizynth $VAR_DIR/download $WEB_DIR "$(dirname $ENV_FILE)"
chown -R $APP_USER:$APP_USER $HOME_DIR $VAR_DIR

step "后端代码（$REPO 的 master）"
if [ -d $APP_DIR/.git ]; then
  as_user git -C $APP_DIR fetch --depth 1 origin master
  as_user git -C $APP_DIR reset --hard FETCH_HEAD
else
  as_user git clone --depth 1 --branch master $REPO $APP_DIR
fi
as_user git -C $APP_DIR log --oneline -1

step "Python 依赖（requirements.txt，版本按 requirements.lock.txt 里测过的；锁文件里只给 Windows 的那几个在 Linux 上不起作用）"
[ -x $VENV/bin/python ] || as_user "$PY" -m venv $VENV
as_user $VENV/bin/pip install --quiet --upgrade pip wheel
as_user $VENV/bin/pip install --quiet -r $APP_DIR/requirements.txt -c $APP_DIR/requirements.lock.txt

step "设置文件 $ENV_FILE（只在第一次生成）"
if [ ! -f $ENV_FILE ]; then
  PUBLIC_IP=$(curl -s -4 --max-time 10 https://api.ipify.org || hostname -I | awk '{print $1}')
  SECRET=$($VENV/bin/python -c 'import secrets; print(secrets.token_urlsafe(48))')
  FERNET=$($VENV/bin/python -c 'from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())')
  umask 027
  cat > $ENV_FILE <<EOF
APP_ENV=production
HOST=127.0.0.1
PORT=8000
VAR_DIR=$VAR_DIR
SECRET_KEY=$SECRET
DATA_ENCRYPTION_KEY=$FERNET
CORS_ORIGINS=http://$PUBLIC_IP
QUEUE_BACKEND=inprocess
WORKER_THREADS=1
M3_ENGINE=auto
AIZYNTH_CONFIG=$VAR_DIR/aizynth/config.yml
ENABLE_PUBCHEM=true
ENABLE_OPSIN=true
ENABLE_LITERATURE_METADATA=false
LLM_PROVIDER=offline
EOF
  unset SECRET FERNET
  chown root:$APP_USER $ENV_FILE
  chmod 640 $ENV_FILE
  umask 022
fi

# 用和后端服务一样的身份和设置跑一条命令（设置文件里有密钥，不经过命令行参数，ps 里看不到）
as_app() {
  systemd-run --quiet --wait --pipe --collect --uid=$APP_USER --gid=$APP_USER \
    -p EnvironmentFile=$ENV_FILE -p WorkingDirectory=$APP_DIR "$@" </dev/null
}

step "AiZynthFinder 模型和 ZINC 在售清单"
if [ ! -f $VAR_DIR/aizynth/zinc_stock.txt ]; then
  as_user $VENV/bin/download_public_data $VAR_DIR/aizynth
  # 在售清单换成纯文本（一行一个 InChIKey）：AiZynthFinder 读纯文本比读 HDF5 少占两三个 GB 内存
  runuser -u $APP_USER -- env HOME=$HOME_DIR $VENV/bin/python - "$VAR_DIR/aizynth" <<'PYEOF'
import sys
from pathlib import Path
import pandas as pd
folder = Path(sys.argv[1])
keys = pd.read_hdf(folder / "zinc_stock.hdf5", key="table")["inchi_key"]
(folder / "zinc_stock.txt.part").write_text("".join(f"{k}\n" for k in keys), encoding="ascii")
(folder / "zinc_stock.txt.part").rename(folder / "zinc_stock.txt")
print(f"{len(keys):,} InChIKeys")
PYEOF
  rm -f $VAR_DIR/aizynth/zinc_stock.hdf5
fi
A=$VAR_DIR/aizynth
cat > $A/config.yml <<EOF
expansion:
  uspto:
    - $A/uspto_model.onnx
    - $A/uspto_templates.csv.gz
  ringbreaker:
    - $A/uspto_ringbreaker_model.onnx
    - $A/uspto_ringbreaker_templates.csv.gz
filter:
  uspto: $A/uspto_filter_model.onnx
stock:
  zinc: $A/zinc_stock.txt
EOF
chown -R $APP_USER:$APP_USER $A

step "前端（deploy/web）"
rm -rf ${WEB_DIR:?}/*
cp -r $APP_DIR/deploy/web/. $WEB_DIR/
chmod -R a+rX $WEB_DIR

step "后台服务和 nginx"
cat > /etc/systemd/system/syntherisk.service <<EOF
[Unit]
Description=Syntherisk backend
After=network-online.target
Wants=network-online.target

[Service]
User=$APP_USER
Group=$APP_USER
WorkingDirectory=$APP_DIR
EnvironmentFile=$ENV_FILE
Environment=PYTHONUNBUFFERED=1
ExecStart=$VENV/bin/python -m uvicorn syntherisk.main:app --host 127.0.0.1 --port 8000 --proxy-headers
Restart=on-failure
RestartSec=5
TimeoutStopSec=30

[Install]
WantedBy=multi-user.target
EOF

cat > /etc/nginx/sites-available/syntherisk <<'EOF'
server {
    listen 80 default_server;
    listen [::]:80 default_server;
    server_name _;
    root /var/www/syntherisk;
    index index.html;
    client_max_body_size 25m;

    location /api/ {
        proxy_pass http://127.0.0.1:8000;
        proxy_http_version 1.1;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
        proxy_read_timeout 600s;
        # 分析进度是服务器推送事件（SSE）：来一条转一条，不攒着
        proxy_buffering off;
        proxy_cache off;
    }
    location = /health {
        proxy_pass http://127.0.0.1:8000;
    }
    location / {
        try_files $uri $uri/ /index.html;
    }
}
EOF
ln -sf /etc/nginx/sites-available/syntherisk /etc/nginx/sites-enabled/syntherisk
rm -f /etc/nginx/sites-enabled/default
nginx -t
systemctl daemon-reload
systemctl enable nginx syntherisk >/dev/null
systemctl reload nginx || systemctl restart nginx

step "防火墙：只开 SSH、80、443"
ufw allow OpenSSH >/dev/null
ufw allow 'Nginx Full' >/dev/null
ufw --force enable >/dev/null
ufw status

step "先例库（只在还没导过时导入；导完以前后端不启动，免得只导了一部分就拿来定证据等级）"
if [ ! -f $VAR_DIR/.precedents-loaded ]; then
  systemctl stop syntherisk || true
  D=$VAR_DIR/download
  if ! echo "$USPTO_MD5  $D/uspto.7z" | md5sum -c --status 2>/dev/null; then
    curl -fL --retry 3 -o $D/uspto.7z $USPTO_URL
    echo "$USPTO_MD5  $D/uspto.7z" | md5sum -c
  fi
  7z x -y -o$D $D/uspto.7z >/dev/null
  RSMI=$(find $D -name "*.rsmi" | head -1)
  if ! echo "$ACADEMIC_MD5  $D/academic13k.txt" | md5sum -c --status 2>/dev/null; then
    curl -fL --retry 3 -o $D/academic13k.txt $ACADEMIC_URL
    echo "$ACADEMIC_MD5  $D/academic13k.txt" | md5sum -c
  fi
  chown -R $APP_USER:$APP_USER $D
  as_app $VENV/bin/python -m syntherisk.cli load-precedents --source uspto --replace --file "$RSMI"
  as_app $VENV/bin/python -m syntherisk.cli load-precedents --source academic13k --replace --file $D/academic13k.txt
  touch $VAR_DIR/.precedents-loaded
  rm -f "$RSMI"
fi

step "启动后端"
systemctl restart syntherisk
for i in $(seq 1 60); do
  if curl -fs http://127.0.0.1:8000/health >/dev/null; then break; fi
  sleep 2
done
curl -fs http://127.0.0.1:8000/health && echo
as_app $VENV/bin/python -m syntherisk.cli status || true

step "期刊论文按 DOI 补年份和题目（联网查 Crossref，在后台跑，不影响使用；日志 journalctl -u syntherisk-fill-literature）"
if [ ! -f $VAR_DIR/.literature-filled ]; then
  systemctl reset-failed syntherisk-fill-literature >/dev/null 2>&1 || true
  systemd-run --quiet --unit=syntherisk-fill-literature --uid=$APP_USER --gid=$APP_USER \
    -p EnvironmentFile=$ENV_FILE -p WorkingDirectory=$APP_DIR \
    /bin/sh -c "$VENV/bin/python -m syntherisk.cli fill-literature --source academic13k && touch $VAR_DIR/.literature-filled" || true
fi

step "完成"
echo "打开 http://$(curl -s -4 --max-time 10 https://api.ipify.org || hostname -I | awk '{print $1}') 就是网站。后端日志：journalctl -u syntherisk -f"
