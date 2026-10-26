#!/usr/bin/env bash
# TikTok Studio — установка на сервер (VPS) с Ubuntu 22.04/24.04 или Debian 12.
#
#   curl -fsSL https://raw.githubusercontent.com/gytfel/Tiktok/main/deploy/install.sh | sudo bash
#
# Со своим доменом вместо IP-адреса (A-запись домена должна указывать на сервер):
#   curl -fsSL https://raw.githubusercontent.com/gytfel/Tiktok/main/deploy/install.sh | sudo bash -s -- --domain studio.example.com
#
# Если порты 80/443 заняты другой программой (например, Jupyter или веб-панель из шаблона
# сервера), установщик назовёт её и остановится; --free-ports остановит и отключит её.
#
# Повторный запуск (или «sudo tiktok-studio update») обновляет студию; видео, загрузки,
# ключи нейросетей и код доступа сохраняются в /var/lib/tiktok-studio.
#
# Для проверки скрипта без настоящего сервера:
#   TS_SOURCE_DIR=<папка с кодом>  взять код из папки, а не с GitHub
#   TS_SKIP_APT=1 TS_SKIP_CADDY=1 TS_NO_SYSTEMD=1 TS_PUBLIC_IP=<адрес> TS_CADDYFILE=<файл>
#   TS_CHECK_PORTS=1  проверять порты 80/443 даже с TS_SKIP_CADDY
set -euo pipefail

REPO="${TS_REPO:-gytfel/Tiktok}"
BRANCH="${TS_BRANCH:-main}"
APP_ROOT=/opt/tiktok-studio
APP_DIR="$APP_ROOT/app"
VENV="$APP_ROOT/venv"
DATA_DIR="${TS_DATA_DIR:-/var/lib/tiktok-studio}"
SERVER_ENV="$DATA_DIR/server.env"
SERVICE_USER=tiktokstudio
PORT=8765
CADDYFILE="${TS_CADDYFILE:-/etc/caddy/Caddyfile}"
MARKER="# TikTok Studio"
DOMAIN=""
FREE_PORTS=""

say()  { printf '\n\033[1;36m==> %s\033[0m\n' "$*"; }
warn() { printf '\033[1;33m[!] %s\033[0m\n' "$*"; }
die()  { printf '\n\033[1;31mОшибка: %s\033[0m\n' "$*" >&2; exit 1; }

usage() {
  cat <<'EOF'
Установка TikTok Studio на сервер.
  --domain ИМЯ   использовать свой домен (по умолчанию — IP-адрес сервера)
  --free-ports   остановить и отключить программы, которые заняли порты 80 и 443
  -h, --help     эта справка
EOF
}

while [ $# -gt 0 ]; do
  case "$1" in
    --domain) DOMAIN="${2:-}"; shift 2 ;;
    --domain=*) DOMAIN="${1#--domain=}"; shift ;;
    --free-ports) FREE_PORTS=1; shift ;;
    -h|--help) usage; exit 0 ;;
    *) die "неизвестный параметр: $1 (см. --help)" ;;
  esac
done

[ "$(id -u)" -eq 0 ] || die "запустите установку от имени администратора: ... | sudo bash"

if [ -r /etc/os-release ]; then
  # shellcheck source=/dev/null
  . /etc/os-release
  case "${ID:-}" in
    ubuntu|debian) ;;
    *) warn "Установщик проверен на Ubuntu и Debian, а у вас ${PRETTY_NAME:-неизвестная система}. Пробую продолжить." ;;
  esac
fi

# Домен из прошлой установки, если сейчас не указан.
if [ -z "$DOMAIN" ] && [ -r "$SERVER_ENV" ]; then
  DOMAIN="$(sed -n 's/^STUDIO_DOMAIN=//p' "$SERVER_ENV" | head -1)"
fi

if [ -z "${TS_NO_SYSTEMD:-}" ] && [ ! -d /run/systemd/system ]; then
  die "на этой машине нет systemd — похоже, это контейнер или облачная среда (например, Jupyter), а не обычный VPS.
Студии нужен обычный сервер: в панели хостинга создайте (или переустановите) VPS с чистой Ubuntu 24.04."
fi

# ------------------------------------------------------------------ порты 80 и 443

# Порты проверяет та же программа, что потом ставится как «tiktok-studio» (ports, free-ports).
if [ -z "${TS_SKIP_CADDY:-}" ] || [ -n "${TS_CHECK_PORTS:-}" ]; then
  ports_tool="$(mktemp)"
  if [ -n "${TS_SOURCE_DIR:-}" ]; then
    cp "$TS_SOURCE_DIR/deploy/tiktok-studio" "$ports_tool"
  else
    curl -fsSL "https://raw.githubusercontent.com/$REPO/$BRANCH/deploy/tiktok-studio" -o "$ports_tool" \
      || die "не удалось скачать файлы студии с GitHub — проверьте интернет на сервере."
  fi
  conflicts="$(bash "$ports_tool" ports || true)"
  if [ -n "$conflicts" ] && [ -n "$FREE_PORTS" ]; then
    say "Освобождаю порты 80 и 443…"
    bash "$ports_tool" free-ports 2>/dev/null | sed 's/^/  /' || true
    conflicts="$(bash "$ports_tool" ports || true)"
    if [ -n "$conflicts" ]; then
      rm -f "$ports_tool"
      die "не получилось освободить порты 80 и 443 — программа на них запускается снова:
$conflicts
В скобках — кто ею управляет (контейнер, служба или цепочка процессов, которая её перезапускает).
Самый надёжный выход — в панели хостинга переустановить систему сервера на чистую Ubuntu 24.04
(без приложений и шаблонов) и запустить установку заново."
    fi
  fi
  rm -f "$ports_tool"
  if [ -n "$conflicts" ]; then
    die "студии нужны порты 80 и 443 (через них работает HTTPS), но их занимает другая программа:
$conflicts
Поэтому по адресу сервера открывается она, а не студия.
Если эта программа вам не нужна (например, Jupyter или веб-панель из шаблона сервера),
запустите установку с параметром --free-ports — установщик остановит и отключит её:
  curl -fsSL https://raw.githubusercontent.com/$REPO/$BRANCH/deploy/install.sh | sudo bash -s -- --free-ports
Если нужна — поставьте студию на отдельный сервер с чистой Ubuntu 24.04."
  fi
fi

free_mb=$(df -Pm / | awk 'NR==2 {print $4}')
if [ "${free_mb:-0}" -lt 1500 ]; then
  die "на диске сервера свободно всего ${free_mb} МБ, нужно хотя бы 1.5 ГБ. Выберите тариф с диском побольше или освободите место."
fi

# ------------------------------------------------------------------ пакеты

caddy_ok() {
  command -v caddy >/dev/null 2>&1 || return 1
  local v
  v="$(caddy version 2>/dev/null | grep -oE '[0-9]+\.[0-9]+\.[0-9]+' | head -1)"
  # Сертификаты Let's Encrypt на IP-адрес (профиль shortlived) работают в Caddy новее 2.10.0.
  [ -n "$v" ] && [ "$(printf '%s\n2.10.2\n' "$v" | sort -V | head -1)" = "2.10.2" ]
}

if [ -z "${TS_SKIP_APT:-}" ]; then
  say "Устанавливаю системные пакеты (Python, ffmpeg)…"
  export DEBIAN_FRONTEND=noninteractive
  apt-get update -q
  apt-get install -y -q python3 python3-venv python3-pip ffmpeg curl ca-certificates unzip gnupg fontconfig

  if [ -z "${TS_SKIP_CADDY:-}" ] && ! caddy_ok; then
    say "Устанавливаю веб-сервер Caddy (HTTPS с бесплатным сертификатом)…"
    curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/gpg.key' \
      | gpg --dearmor --yes -o /usr/share/keyrings/caddy-stable-archive-keyring.gpg
    curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/debian.deb.txt' > /etc/apt/sources.list.d/caddy-stable.list
    chmod o+r /usr/share/keyrings/caddy-stable-archive-keyring.gpg /etc/apt/sources.list.d/caddy-stable.list
    apt-get update -q
    apt-get install -y -q caddy
  fi
fi

python3 -c 'import sys; sys.exit(sys.version_info < (3, 10))' || die "нужен Python 3.10 или новее"

# ------------------------------------------------------------------ подкачка

mem_kb=$(awk '/MemTotal/ {print $2}' /proc/meminfo)
if [ "${mem_kb:-0}" -lt 3000000 ] && [ -z "$(swapon --show --noheadings 2>/dev/null)" ] && [ "$free_mb" -gt 4000 ]; then
  say "Памяти меньше 3 ГБ — добавляю файл подкачки 2 ГБ, чтобы сборка видео не падала…"
  if { fallocate -l 2G /swapfile 2>/dev/null || dd if=/dev/zero of=/swapfile bs=1M count=2048 status=none; } \
     && chmod 600 /swapfile && mkswap /swapfile >/dev/null && swapon /swapfile; then
    grep -q '^/swapfile ' /etc/fstab || echo '/swapfile none swap sw 0 0' >> /etc/fstab
  else
    rm -f /swapfile
    warn "Не удалось включить подкачку — продолжаю без неё."
  fi
fi

# ------------------------------------------------------------------ пользователь и код

id -u "$SERVICE_USER" >/dev/null 2>&1 \
  || useradd --system --home-dir "$DATA_DIR" --shell /usr/sbin/nologin "$SERVICE_USER"
mkdir -p "$DATA_DIR" "$APP_ROOT"
chown -R "$SERVICE_USER:$SERVICE_USER" "$DATA_DIR"
# В settings.json лежат ключи нейросетей — данные студии видны только ей самой.
chmod 700 "$DATA_DIR"
[ -f "$DATA_DIR/settings.json" ] && chmod 600 "$DATA_DIR/settings.json"

say "Скачиваю студию…"
tmp="$(mktemp -d)"
trap 'rm -rf "$tmp"' EXIT
mkdir -p "$tmp/src"
if [ -n "${TS_SOURCE_DIR:-}" ]; then
  tar -C "$TS_SOURCE_DIR" --exclude=.git --exclude=workspace --exclude=.venv --exclude=__pycache__ \
    --exclude=.pytest_cache -cf - . | tar -C "$tmp/src" -xf -
else
  curl -fsSL "https://github.com/$REPO/archive/refs/heads/$BRANCH.zip" -o "$tmp/src.zip" \
    || die "не удалось скачать студию с GitHub — проверьте интернет на сервере"
  unzip -q "$tmp/src.zip" -d "$tmp/zip"
  top="$(find "$tmp/zip" -mindepth 1 -maxdepth 1 -type d | head -1)"
  cp -a "$top/." "$tmp/src/"
fi
[ -f "$tmp/src/run.py" ] || die "в скачанном архиве нет run.py"
rm -rf "$APP_DIR.new" "$APP_DIR.old"
mv "$tmp/src" "$APP_DIR.new"
[ -d "$APP_DIR" ] && mv "$APP_DIR" "$APP_DIR.old"
mv "$APP_DIR.new" "$APP_DIR"
rm -rf "$APP_DIR.old"
chmod -R a+rX "$APP_DIR"

say "Устанавливаю зависимости Python (несколько минут)…"
if [ ! -x "$VENV/bin/python" ] || ! "$VENV/bin/python" -m pip --version >/dev/null 2>&1; then
  rm -rf "$VENV"
  python3 -m venv "$VENV"
fi
"$VENV/bin/python" -m pip install -q --no-cache-dir --disable-pip-version-check -r "$APP_DIR/requirements.txt"
# yt-dlp (видео-образцы по ссылке) часто чинят под изменения сайтов — при каждом обновлении берём свежий.
"$VENV/bin/python" -m pip install -q --no-cache-dir --disable-pip-version-check -U "yt-dlp[default,curl-cffi]" \
  || warn "Не удалось обновить yt-dlp — видео по ссылке могут не скачиваться, файлом загружать можно."
chmod -R a+rX "$VENV"

# Студия проверена с ffmpeg 6 и 7. В Ubuntu 22.04 из пакетов ставится 4.4 —
# тогда берём сборку ffmpeg 7 из пакета imageio-ffmpeg (она уже в окружении).
FFMPEG_BIN=""
ffmpeg_major="$(ffmpeg -version 2>/dev/null | sed -n 's/^ffmpeg version n\{0,1\}\([0-9]\{1,\}\)\..*/\1/p' | head -1)"
if [ -z "$ffmpeg_major" ] || [ "$ffmpeg_major" -lt 5 ]; then
  FFMPEG_BIN="$("$VENV/bin/python" -c 'import imageio_ffmpeg; print(imageio_ffmpeg.get_ffmpeg_exe())' 2>/dev/null || true)"
  if [ -n "$FFMPEG_BIN" ] && [ -x "$FFMPEG_BIN" ]; then
    say "Системный ffmpeg ${ffmpeg_major:+версии $ffmpeg_major }не подходит — использую встроенный: $FFMPEG_BIN"
  else
    FFMPEG_BIN=""
  fi
fi

run_as() {
  (cd "$APP_DIR" && umask 077 && runuser -u "$SERVICE_USER" -- env HOME="$DATA_DIR" TIKTOK_STUDIO_WORKSPACE="$DATA_DIR" \
    TIKTOK_STUDIO_FFMPEG="$FFMPEG_BIN" "$@")
}

if ! run_as "$VENV/bin/python" -c 'import sys; from studio.ffmpeg_utils import capabilities as c; sys.exit(0 if c()["ok"] else 1)'; then
  warn "В ffmpeg на сервере не хватает компонентов (libass/libx264) — сборка видео может не работать."
fi
# Код доступа создаётся один раз и хранится в $DATA_DIR/settings.json (его покажет «tiktok-studio info»).
run_as "$VENV/bin/python" -c 'from studio import phone; phone.access_key()'
# Голоса Piper — озвучка без Microsoft: адресам серверов Microsoft часто не отдаёт звук.
say "Скачиваю русские голоса Piper (запасная озвучка, ~130 МБ)…"
run_as "$VENV/bin/python" -c 'from studio import tts; [tts.piper_model(c) for c in ("ru_RU-dmitri-medium", "ru_RU-irina-medium")]' \
  || warn "Голоса Piper не скачались — студия скачает их при первой озвучке."

# ------------------------------------------------------------------ адрес

detect_ip() {
  local u ip
  for u in https://api.ipify.org https://ipv4.icanhazip.com https://ifconfig.me/ip; do
    ip="$(curl -4 -fsS --max-time 8 "$u" 2>/dev/null | tr -d '[:space:]')" || continue
    if [[ "$ip" =~ ^[0-9]+\.[0-9]+\.[0-9]+\.[0-9]+$ ]]; then echo "$ip"; return 0; fi
  done
  ip -4 route get 1.1.1.1 2>/dev/null | awk '{for (i = 1; i <= NF; i++) if ($i == "src") print $(i + 1)}' | head -1
}

if [ -n "$DOMAIN" ]; then
  HOST="$DOMAIN"
else
  HOST="${TS_PUBLIC_IP:-$(detect_ip)}"
  [ -n "$HOST" ] || die "не удалось узнать IP-адрес сервера. Укажите домен: --domain имя.ru"
  if [[ "$HOST" =~ ^(10\.|192\.168\.|172\.(1[6-9]|2[0-9]|3[01])\.) ]]; then
    warn "У сервера нет публичного IPv4 ($HOST). В панели хостинга подключите публичный IP или укажите домен через --domain."
  fi
fi
URL="https://$HOST"
cat > "$SERVER_ENV" <<EOF
STUDIO_URL=$URL
STUDIO_DOMAIN=$DOMAIN
EOF
chmod 644 "$SERVER_ENV"

# ------------------------------------------------------------------ служба

install -m 755 "$APP_DIR/deploy/tiktok-studio" /usr/local/bin/tiktok-studio

if [ -z "${TS_NO_SYSTEMD:-}" ]; then
  say "Настраиваю автозапуск…"
  cat > /etc/systemd/system/tiktok-studio.service <<EOF
[Unit]
Description=TikTok Studio
After=network-online.target
Wants=network-online.target

[Service]
User=$SERVICE_USER
Group=$SERVICE_USER
WorkingDirectory=$APP_DIR
Environment=TIKTOK_STUDIO_WORKSPACE=$DATA_DIR
Environment=HOME=$DATA_DIR
Environment=PYTHONUNBUFFERED=1
${FFMPEG_BIN:+Environment=TIKTOK_STUDIO_FFMPEG=$FFMPEG_BIN
}UMask=0077
ExecStart=$VENV/bin/python run.py --server --host 127.0.0.1 --port $PORT
Restart=always
RestartSec=3
Nice=5

[Install]
WantedBy=multi-user.target
EOF
  systemctl daemon-reload
  systemctl enable --quiet tiktok-studio
  systemctl restart tiktok-studio
fi

# ------------------------------------------------------------------ HTTPS (Caddy)

write_caddyfile() {
  if [ -f "$CADDYFILE" ] && ! grep -q "$MARKER" "$CADDYFILE"; then
    cp "$CADDYFILE" "$CADDYFILE.bak-$(date +%Y%m%d%H%M%S)"
    warn "Прежний $CADDYFILE сохранён рядом с суффиксом .bak-…"
  fi
  mkdir -p "$(dirname "$CADDYFILE")"
  if [ -n "$DOMAIN" ]; then
    cat > "$CADDYFILE" <<EOF
$MARKER — файл создан установщиком и перезаписывается при обновлении.
$DOMAIN {
	encode gzip
	reverse_proxy 127.0.0.1:$PORT
}
EOF
  else
    cat > "$CADDYFILE" <<EOF
$MARKER — файл создан установщиком и перезаписывается при обновлении.
# Сертификат Let's Encrypt прямо на IP-адрес: выдаётся только с коротким профилем
# (6 дней), Caddy продлевает его сам.
https://$HOST {
	tls {
		issuer acme {
			profile shortlived
		}
	}
	encode gzip
	reverse_proxy 127.0.0.1:$PORT
}
EOF
  fi
}

if [ -z "${TS_SKIP_CADDY:-}" ]; then
  say "Настраиваю HTTPS для $HOST…"
  write_caddyfile
  caddy validate --config "$CADDYFILE" --adapter caddyfile >/dev/null || die "Caddy не принял настройки: $CADDYFILE"
  if command -v ufw >/dev/null 2>&1 && ufw status 2>/dev/null | grep -q "Status: active"; then
    ufw allow 80/tcp >/dev/null && ufw allow 443/tcp >/dev/null
  fi
  if [ -z "${TS_NO_SYSTEMD:-}" ]; then
    systemctl enable --quiet caddy
    if ! systemctl restart caddy; then
      journalctl -u caddy -n 15 --no-pager 2>/dev/null || true
      die "веб-сервер Caddy не запустился (журнал выше). Подробная проверка: sudo tiktok-studio doctor"
    fi
  fi
else
  write_caddyfile
fi

# ------------------------------------------------------------------ проверка

if [ -z "${TS_NO_SYSTEMD:-}" ]; then
  say "Проверяю, что студия запустилась…"
  ok=""
  for _ in $(seq 1 60); do
    code="$(curl -s -o /dev/null -w '%{http_code}' "http://127.0.0.1:$PORT/" || true)"
    if [ "$code" = "401" ]; then ok=1; break; fi
    sleep 1
  done
  [ -n "$ok" ] || die "студия не отвечает. Журнал: sudo tiktok-studio logs"

  if [ -z "${TS_SKIP_CADDY:-}" ]; then
    say "Получаю сертификат HTTPS (до 2 минут)…"
    https_ok=""
    for _ in $(seq 1 24); do
      # Ответ «Нужен код доступа» бывает только у студии — так видно, что по ссылке именно она.
      if curl -s --max-time 8 "$URL/api/status" 2>/dev/null | grep -q "Нужен код доступа"; then https_ok=1; break; fi
      sleep 5
    done
    if [ -z "$https_ok" ]; then
      code="$(curl -sk -o /dev/null -w '%{http_code}' --max-time 8 "$URL/api/status" || true)"
      if curl -sk --max-time 8 "$URL/api/status" 2>/dev/null | grep -q "Нужен код доступа"; then
        warn "Студия отвечает, но сертификат HTTPS пока не получен. Обычно причина — закрытые порты:"
        warn "в панели хостинга в разделе «Firewall»/«Сетевые правила» разрешите входящие TCP 80 и 443."
      elif [ "$code" != "000" ]; then
        warn "По адресу $URL отвечает другая программа, а не студия. Похоже, порты 80/443 этого адреса"
        warn "заняты на стороне хостинга (например, шаблоном с Jupyter). Переустановите сервер на чистую Ubuntu 24.04."
      else
        warn "Адрес $URL не отвечает. В панели хостинга в разделе «Firewall»/«Сетевые правила»"
        warn "разрешите входящие TCP 80 и 443."
      fi
      warn "Затем: sudo tiktok-studio restart. Проверка всего сразу: sudo tiktok-studio doctor"
    fi
  fi
fi

/usr/local/bin/tiktok-studio info
