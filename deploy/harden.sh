#!/usr/bin/env bash
# =====================================================================
# Naslai serverini himoyalash skripti (Ubuntu). Bir necha marta ishga
# tushirsa ham xavfsiz — har safar bir xil holatga keltiradi.
#
#   sudo bash deploy/harden.sh
#
# Nima qiladi:
#   1. Firewall (ufw): faqat SSH, HTTP, HTTPS ochiq; Redis/Postgres va
#      boshqa portlar tashqaridan yopiladi. Skaner diapazoni bloklanadi.
#   2. fail2ban: SSH parol tanlash, .env/.git/wp-login skanerlari va
#      Django admin login urinishlari uchun avtomatik IP bloklash.
#   3. nginx: versiya yashiriladi (server_tokens off).
#   4. Xavfsizlik yangilanishlari avtomatik o'rnatiladi.
#
# SSH sozlamalariga TEGMAYDI (parol bilan kirishni o'chirish sizni
# serverdan chiqarib qo'yishi mumkin) — oxirida tavsiya chiqaradi.
# =====================================================================
set -euo pipefail

if [[ $EUID -ne 0 ]]; then
  echo "Root sifatida ishga tushiring: sudo bash deploy/harden.sh" >&2
  exit 1
fi

say() { printf '\n\033[1;36m==> %s\033[0m\n' "$*"; }

SSH_PORT="$(sshd -T 2>/dev/null | awk '/^port /{print $2; exit}')"
SSH_PORT="${SSH_PORT:-22}"
NGINX_ACCESS_LOG="${NGINX_ACCESS_LOG:-/var/log/nginx/access.log}"
BLOCK_RANGES="${BLOCK_RANGES:-79.124.58.0/24}"
# Agar o'z IP manzilingiz bo'lsa — hech qachon bloklanmaydi (bo'sh joy bilan ajrating).
TRUSTED_IPS="${TRUSTED_IPS:-}"

say "Paketlar o'rnatilmoqda (ufw, fail2ban, unattended-upgrades)"
export DEBIAN_FRONTEND=noninteractive
apt-get update -qq
apt-get install -y -qq ufw fail2ban unattended-upgrades >/dev/null

# ---------------------------------------------------------------- 1. ufw
say "Firewall sozlanmoqda (SSH port: ${SSH_PORT})"
ufw --force default deny incoming >/dev/null
ufw --force default allow outgoing >/dev/null
# SSH avval ochiladi — aks holda ulanish uzilib qolishi mumkin.
ufw allow "${SSH_PORT}/tcp" comment 'SSH' >/dev/null
ufw allow 80/tcp comment 'HTTP' >/dev/null
ufw allow 443/tcp comment 'HTTPS' >/dev/null
for range in $BLOCK_RANGES; do
  ufw insert 1 deny from "$range" comment 'scanner' >/dev/null 2>&1 || ufw deny from "$range" >/dev/null
done
ufw --force enable >/dev/null
ufw status verbose | sed 's/^/   /'

# ------------------------------------------------------------ 2. fail2ban
say "fail2ban sozlanmoqda"

cat > /etc/fail2ban/filter.d/naslai-scan.conf <<'FILTER'
# Mavjud bo'lmagan maxfiy fayllarni qidirayotgan skanerlar.
[Definition]
failregex = ^<HOST> -.*"(GET|POST|HEAD) [^"]*(/\.env|/\.git|/wp-login\.php|/wp-admin|/wp-config|/xmlrpc\.php|/phpmyadmin|/pma/|/\.aws|/\.ssh|/config\.php|/server-status|/actuator|/vendor/phpunit|/cgi-bin/|/boaform|/HNAP1|/SDK/webLanguage)[^"]*" (400|401|403|404|405|444)
ignoreregex =
FILTER

cat > /etc/fail2ban/filter.d/naslai-admin.conf <<'FILTER'
# Django admin login xato urinishlari (config/admin_guard.py loglaydi).
[Definition]
failregex = Admin login (xato|bloklangan): ip=<HOST>
ignoreregex =
FILTER

IGNORE_LINE="ignoreip = 127.0.0.1/8 ::1 ${TRUSTED_IPS}"

cat > /etc/fail2ban/jail.d/naslai.local <<JAIL
[DEFAULT]
${IGNORE_LINE}
banaction = ufw
bantime = 1h
findtime = 10m
maxretry = 5

[sshd]
enabled = true
port = ${SSH_PORT}
maxretry = 5
bantime = 6h

[naslai-scan]
enabled = true
port = http,https
filter = naslai-scan
logpath = ${NGINX_ACCESS_LOG}
maxretry = 3
findtime = 10m
bantime = 24h

[naslai-admin]
enabled = true
port = http,https
filter = naslai-admin
backend = systemd
journalmatch = _SYSTEMD_UNIT=naslaii.service
maxretry = 8
findtime = 30m
bantime = 12h

# Qayta-qayta bloklanganlar — bir haftaga.
[recidive]
enabled = true
logpath = /var/log/fail2ban.log
bantime = 7d
findtime = 1d
maxretry = 3
JAIL

if [[ ! -f "$NGINX_ACCESS_LOG" ]]; then
  echo "   ⚠ $NGINX_ACCESS_LOG topilmadi — naslai-scan qoidasi o'chiriladi."
  sed -i '/^\[naslai-scan\]/,/^$/ s/^enabled = true/enabled = false/' /etc/fail2ban/jail.d/naslai.local
fi
if ! systemctl list-unit-files | grep -q '^naslaii.service'; then
  echo "   ⚠ naslaii.service topilmadi — naslai-admin qoidasi o'chiriladi."
  sed -i '/^\[naslai-admin\]/,/^$/ s/^enabled = true/enabled = false/' /etc/fail2ban/jail.d/naslai.local
fi

fail2ban-client -t >/dev/null
systemctl enable fail2ban >/dev/null 2>&1
systemctl restart fail2ban
sleep 2
fail2ban-client status | sed 's/^/   /'

# --------------------------------------------------------------- 3. nginx
if command -v nginx >/dev/null 2>&1; then
  say "nginx: versiya yashirilmoqda"
  if ! grep -rqs "server_tokens off" /etc/nginx/nginx.conf /etc/nginx/conf.d/; then
    echo "server_tokens off;" > /etc/nginx/conf.d/naslai-security.conf
  fi
  if nginx -t >/dev/null 2>&1; then
    systemctl reload nginx
    echo "   nginx qayta yuklandi"
  else
    echo "   ⚠ nginx -t xato berdi — o'zgarish bekor qilinmoqda"
    rm -f /etc/nginx/conf.d/naslai-security.conf
  fi
fi

# ------------------------------------------------- 4. avtomatik yangilanish
say "Xavfsizlik yangilanishlari avtomatik o'rnatiladigan qilinmoqda"
cat > /etc/apt/apt.conf.d/20auto-upgrades <<'APT'
APT::Periodic::Update-Package-Lists "1";
APT::Periodic::Unattended-Upgrade "1";
APT
systemctl enable --now unattended-upgrades >/dev/null 2>&1 || true

# --------------------------------------------------------------- tekshiruv
say "Ochiq portlar (tashqariga faqat 22/80/443 ko'rinishi kerak)"
ss -tlnp | awk 'NR==1 || /LISTEN/' | sed 's/^/   /'
if ss -tlnp | grep -qE '0\.0\.0\.0:6379|\[::\]:6379'; then
  echo "   ⚠ Redis barcha manzillarda tinglayapti. ufw uni yopdi, lekin /etc/redis/redis.conf da 'bind 127.0.0.1 ::1' qo'yish tavsiya etiladi."
fi

say "SSH bo'yicha tavsiya (avtomatik o'zgartirilmadi)"
sshd -T 2>/dev/null | grep -E '^(permitrootlogin|passwordauthentication) ' | sed 's/^/   hozir: /'
cat <<'TIP'
   Eng kuchli himoya — SSH kalit bilan kirish va parolni o'chirish.
   Buni faqat SSH kalit bilan kira olishingizni tekshirgandan keyin qiling,
   aks holda serverga kira olmay qolasiz.

Tayyor. Bloklangan IP'larni ko'rish:   sudo fail2ban-client status naslai-scan
IP'ni blokdan chiqarish:              sudo fail2ban-client unban <IP>
TIP
