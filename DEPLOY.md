# Serverga joylash — Ubuntu / DigitalOcean

Bosqichlar tartibi muhim. Ayniqsa DNS va HTTPS — ularni oxiriga qoldirsangiz,
cookie va CORS bilan ovora bo'lasiz.

Quyida `NASLAI_IP` — sizning droplet IP manzilingiz.

---

## 0. Avval DNS. Shu bugun qiling.

Domen ro'yxatdan o'tkazgan joyingizda ikkita A yozuv qo'shing:

```
naslai.uz        A    NASLAI_IP
www.naslai.uz    A    NASLAI_IP
```

DNS tarqalishi 10 daqiqadan bir necha soatgacha ketadi. Shuning uchun eng
birinchi shu. Tekshirish:

```bash
dig +short naslai.uz
```

IP chiqsa — davom eting.

---

## 1. Serverga kirish va asosiy himoya

```bash
ssh root@NASLAI_IP
```

Tizimni yangilang:

```bash
apt update && apt upgrade -y
```

Ishchi foydalanuvchi yarating (root bilan ishlamang):

```bash
adduser nurulloh && usermod -aG sudo nurulloh
```

Faervol. **`OpenSSH` ni birinchi ruxsat bering**, aks holda o'zingizni
serverdan qulflab qo'yasiz:

```bash
ufw allow OpenSSH && ufw allow 'Nginx Full' && ufw --force enable
```

Bundan keyin `nurulloh` sifatida kiring: `ssh nurulloh@NASLAI_IP`

---

## 2. Kerakli paketlar

```bash
sudo apt install -y python3-venv python3-pip nginx git redis-server
```

Redis faqat serverning o'zidan ochiq bo'lsin. Ubuntu'da bu standart
sozlama, lekin tekshirib qo'ying — tashqariga ochiq Redis eng ko'p
buziladigan xizmatlardan biri:

```bash
sudo ss -lntp | grep 6379
```

Chiqishda `127.0.0.1:6379` bo'lishi kerak. Agar `0.0.0.0:6379` bo'lsa,
`/etc/redis/redis.conf` da `bind 127.0.0.1 -::1` qiling va
`sudo systemctl restart redis-server` bajaring.

---

## 3. Papkalar

Kod va ma'lumot alohida turadi. Bu shart: deploy paytida kod ustiga
yoziladi, baza va rasmlar esa o'sha joyda qolishi kerak.

```bash
sudo mkdir -p /srv/naslai /var/lib/naslai/media /var/lib/naslai/static /etc/naslai
```

```bash
sudo chown -R nurulloh:www-data /srv/naslai /var/lib/naslai
```

---

## 4. Kodni olib kelish

```bash
git clone https://github.com/<username>/naslai_drf.git /srv/naslai
```

Virtual muhit va kutubxonalar:

```bash
cd /srv/naslai && python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
```

Gunicorn alohida — u `requirements.txt` da yo'q, chunki Windows'ga
o'rnatilmaydi va lokal o'rnatishingizni sindirardi:

```bash
/srv/naslai/.venv/bin/pip install gunicorn
```

---

## 5. Maxfiy sozlamalar

Avval kalit yarating va chiqqan qiymatni nusxalang:

```bash
/srv/naslai/.venv/bin/python -c "import secrets; print(secrets.token_urlsafe(64))"
```

Faylni yarating:

```bash
sudo nano /etc/naslai/naslai.env
```

Ichiga:

```ini
DJANGO_SECRET_KEY=<yuqorida chiqqan qiymat>
DJANGO_DEBUG=0
NASLAI_SITE_DOMAIN=naslai.uz
DJANGO_ALLOWED_HOSTS=naslai.uz,www.naslai.uz

NASLAI_DB_PATH=/var/lib/naslai/db.sqlite3
NASLAI_MEDIA_ROOT=/var/lib/naslai/media
DJANGO_STATIC_ROOT=/var/lib/naslai/static

# Fon navbati. BO'SH QOLDIRMANG: bo'sh bo'lsa generatsiya to'g'ridan-to'g'ri
# HTTP so'rovi ichida bajariladi va gunicorn jarayonlarini uch daqiqagacha
# band qiladi.
CELERY_BROKER_URL=redis://127.0.0.1:6379/0
STUCK_JOB_MINUTES=30

# Avval mock bilan tekshiring — bir tiyin ham sarflanmaydi.
# Hammasi ishlaganiga ishonch hosil qilgach openai ga o'zgartiring.
GENERATION_PROVIDER=mock
AUDIT_PROVIDER=mock
OPENAI_API_KEY=

ADMIN_EMAILS=nurulloh166@gmail.com
GOOGLE_CLIENT_ID=
TELEGRAM_BOT_TOKEN=
TELEGRAM_BOT_NAME=

PAYMENTS_ENABLED=0
```

Huquqlar — faylni faqat root va servis o'qisin:

```bash
sudo chown root:nurulloh /etc/naslai/naslai.env && sudo chmod 640 /etc/naslai/naslai.env
```

---

## 6. Baza va statik fayllar

```bash
cd /srv/naslai && set -a && . /etc/naslai/naslai.env && set +a && .venv/bin/python manage.py migrate
```

```bash
cd /srv/naslai && set -a && . /etc/naslai/naslai.env && set +a && .venv/bin/python manage.py collectstatic --noinput
```

Admin foydalanuvchi:

```bash
cd /srv/naslai && set -a && . /etc/naslai/naslai.env && set +a && .venv/bin/python manage.py createsuperuser
```

---

## 7. Gunicorn servisi

```bash
sudo nano /etc/systemd/system/naslai.service
```

```ini
[Unit]
Description=Naslai backend
After=network.target

[Service]
User=nurulloh
Group=www-data
WorkingDirectory=/srv/naslai
EnvironmentFile=/etc/naslai/naslai.env
# --timeout 300: haqiqiy generatsiya uch daqiqagacha davom etadi.
# Pasaytirsangiz, systemd to'langan so'rovni o'rtasida uzib qo'yadi.
ExecStart=/srv/naslai/.venv/bin/gunicorn config.wsgi:application \
          --bind 127.0.0.1:8787 --workers 3 --timeout 300
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl daemon-reload && sudo systemctl enable --now naslai
```

Ishlayaptimi:

```bash
sudo systemctl status naslai --no-pager
```

```bash
curl -s -H "Host: naslai.uz" http://127.0.0.1:8787/api/auth/session
```

`{"authenticated":false,"account":null}` chiqishi kerak.

---

## 7b. Celery worker — generatsiya navbati

Gunicorn faqat so'rovlarga javob beradi, rasm chizishni worker bajaradi.
Ikkalasi ham bir xil `.env` faylni o'qiydi.

```bash
sudo nano /etc/systemd/system/naslai-worker.service
```

```ini
[Unit]
Description=Naslai generation worker
After=network.target redis-server.service
Requires=redis-server.service

[Service]
User=nurulloh
Group=www-data
WorkingDirectory=/srv/naslai
EnvironmentFile=/etc/naslai/naslai.env
# --concurrency: bir vaqtda nechta generatsiya. Har bir jarayon Django va
#   Pillow'ni xotiraga yuklaydi (~150 MB), shuning uchun oshirib
#   yubormang. 1 GB droplet uchun 2 dan oshmasin.
# --max-tasks-per-child: Pillow xotirani sekin oqizadi, jarayon vaqti-vaqti
#   bilan yangilanib tursin.
ExecStart=/srv/naslai/.venv/bin/celery -A config worker \
          --loglevel=info --concurrency=2 --max-tasks-per-child=20
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl daemon-reload && sudo systemctl enable --now naslai-worker
```

Worker vazifani ko'ryaptimi:

```bash
sudo journalctl -u naslai-worker -n 30 --no-pager
```

Ishga tushganda ro'yxatda `generation.run_job` chiqishi kerak.

### Osilib qolgan vazifalarni tozalash

Worker o'lib qolsa (xotira tugadi, server qayta yuklandi), vazifa
`processing` holatida abadiy qolib ketadi: odam token to'lagan, rasm
olmagan. Buni soatiga tekshirib turadigan timer qo'ying.

```bash
sudo nano /etc/systemd/system/naslai-cleanup.service
```

```ini
[Unit]
Description=Naslai: osilib qolgan generatsiyalarni yopish

[Service]
Type=oneshot
User=nurulloh
WorkingDirectory=/srv/naslai
EnvironmentFile=/etc/naslai/naslai.env
ExecStart=/srv/naslai/.venv/bin/python manage.py release_stuck_jobs
```

```bash
sudo nano /etc/systemd/system/naslai-cleanup.timer
```

```ini
[Unit]
Description=Har 15 daqiqada osilib qolgan generatsiyalarni tekshirish

[Timer]
OnBootSec=10min
OnUnitActiveSec=15min

[Install]
WantedBy=timers.target
```

```bash
sudo systemctl daemon-reload && sudo systemctl enable --now naslai-cleanup.timer
```

Nima qilishini oldindan ko'rish uchun (hech narsa o'zgartirmaydi):

```bash
cd /srv/naslai && set -a && . /etc/naslai/naslai.env && set +a && .venv/bin/python manage.py release_stuck_jobs --dry-run
```

---

## 8. Nginx

```bash
sudo nano /etc/nginx/sites-available/naslai
```

```nginx
server {
    listen 80;
    server_name naslai.uz www.naslai.uz;

    # Yig'ilgan frontend
    root /var/www/naslai/dist;
    index index.html;

    location / {
        try_files $uri $uri/ /index.html;
    }

    location /api/ {
        proxy_pass http://127.0.0.1:8787;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        # Busiz Django ulanishni shifrlanmagan deb hisoblaydi va
        # cookie'ga Secure bayrog'ini qo'ymaydi.
        proxy_set_header X-Forwarded-Proto $scheme;
        # Fotolar JSON ichida base64 bo'lib keladi.
        client_max_body_size 25m;
        proxy_read_timeout 300s;
    }

    location /media/  { alias /var/lib/naslai/media/;  expires 30d; }
    location /static/ { alias /var/lib/naslai/static/; expires 30d; }
}
```

```bash
sudo ln -sf /etc/nginx/sites-available/naslai /etc/nginx/sites-enabled/naslai && sudo rm -f /etc/nginx/sites-enabled/default
```

```bash
sudo nginx -t && sudo systemctl reload nginx
```

---

## 9. HTTPS — o'tkazib yubormang

Sertifikatsiz cookie'lar ishlamaydi: `DJANGO_DEBUG=0` da ular `Secure`
bayrog'i bilan chiqadi va brauzer ularni oddiy HTTP orqali qabul qilmaydi.
Ya'ni **HTTPS'siz kirish umuman ishlamaydi.**

```bash
sudo apt install -y certbot python3-certbot-nginx
```

```bash
sudo certbot --nginx -d naslai.uz -d www.naslai.uz
```

Certbot 80-portdan 443-ga yo'naltirishni o'zi qo'shadi. Yangilanish
avtomatik, tekshirish:

```bash
sudo certbot renew --dry-run
```

---

## 10. Frontendni joylash

Lokal kompyuteringizda (`front-nasl` papkasida):

```bash
npm run build
```

`dist/` papkasini serverga yuboring:

```bash
scp -r dist nurulloh@NASLAI_IP:/tmp/naslai-dist
```

Serverda:

```bash
sudo mkdir -p /var/www/naslai && sudo rm -rf /var/www/naslai/dist && sudo mv /tmp/naslai-dist /var/www/naslai/dist && sudo chown -R www-data:www-data /var/www/naslai
```

---

## 11. Tekshirish

```bash
curl -s https://naslai.uz/api/auth/session
```

Xavfsizlik ro'yxati:

```bash
cd /srv/naslai && set -a && . /etc/naslai/naslai.env && set +a && .venv/bin/python manage.py check --deploy
```

Ikkita ogohlantirish qolishi normal:

- **W008** (SSL redirect) — buni nginx qiladi.
- **W021** (HSTS preload) — ataylab o'chirilgan, chunki uni yoqsangiz
  orqaga qaytarib bo'lmaydi.

Brauzerda `https://naslai.uz` oching, ro'yxatdan o'ting va bitta mock
generatsiya qilib ko'ring. Ishlagach `/etc/naslai/naslai.env` da
`GENERATION_PROVIDER=openai` qiling va servisni qayta ishga tushiring:

```bash
sudo systemctl restart naslai
```

---

## 12. OpenAI'ni yoqish

Mock bilan hamma narsa ishlaganiga ishonch hosil qilgach.

### Uchta o'zgaruvchi, uchtasi ham kerak

Faqat kalitni yozish **yetarli emas** — provayder `mock` bo'lib qolaveradi
va server zaxira rasm chizaveradi. Xato emas, shunchaki hech narsa
o'zgarmaydi, va buni tushunmay uzoq vaqt qidirasiz.

```ini
OPENAI_API_KEY=sk-...
# Rasm generatsiyasi VA foto tahlili (/api/analyze) shunga qaraydi
GENERATION_PROVIDER=openai
# "Sotuv yomonmi?" bo'limi alohida
AUDIT_PROVIDER=openai
```

`.env` faqat jarayon ishga tushganda o'qiladi. O'zgartirgach albatta:

```bash
sudo systemctl restart naslai
```

### Model nomi hisobingizda bormi

Kod `gpt-image-2` ni kutadi (`OPENAI_IMAGE_MODEL`). Model nomi noto'g'ri
bo'lsa, generatsiya uch marta urinadi, har safar 15 soniya kutadi va
oxirida tushunarsiz xato beradi. Oldindan tekshiring:

```bash
curl -s https://api.openai.com/v1/models -H "Authorization: Bearer sk-..." | grep -o '"id":"gpt-image[^"]*"'
```

Ro'yxatda chiqmasa — `.env` da mavjud modelga o'zgartiring, masalan
`OPENAI_IMAGE_MODEL=gpt-image-1`. Kod o'lchamni model bo'yicha o'zi
tanlaydi (gpt-image-2 → 1088x1440, gpt-image-1 → 1024x1536).

Matn modeli alohida: `OPENAI_BRIEF_MODEL` (standart `gpt-4o`) — brif va
audit uchun.

### Hisobda pul va chegara

OpenAI'da avval balans to'ldirilgan bo'lishi kerak, aks holda har bir
so'rov 429 beradi. Platformada **oylik chegara** ham qo'ying: kodda
qayta urinish zanjiri bor, va sozlama xatosi bo'lsa u pulni tez yeydi.

### Narxni tekshiring

Butun iqtisod bitta raqamdan hisoblanadi — bir rasmning tannarxi:

```ini
API_IMAGE_COST_USD=0.05
USD_UZS=11935
```

Tanlagan modelingizning haqiqiy narxi boshqacha bo'lsa, shu yerni
to'g'rilang: paket narxlari, token narxi va marja hammasi shundan
kelib chiqadi (`billing/pricing.py`).

### Birinchi haqiqiy generatsiya

Bitta karta yarating va logga qarang:

```bash
sudo journalctl -u naslai -f
```

Xato bo'lsa, tokenlar avtomatik qaytariladi — kodda shunday qilingan.
Lekin sabab logda qoladi, o'sha yerdan qidiring.

### Bir vaqtda nechta generatsiya ketadi

Celery worker `--concurrency=2` bilan ishlaydi — bir vaqtda ikkita rasm
chiziladi, qolganlari navbatda kutadi. Sayt esa **hech qachon
sekinlashmaydi**: gunicorn faqat javob beradi, kutish worker tomonda.

Navbat uzayib ketsa concurrency'ni oshiring, lekin xotiraga qarab:

```bash
free -m
```

Har bir worker jarayoni ~150 MB oladi. 1 GB droplet'da 2 dan oshirmang va
swap qo'shing:

```bash
sudo fallocate -l 2G /swapfile && sudo chmod 600 /swapfile && sudo mkswap /swapfile && sudo swapon /swapfile
```

```bash
echo '/swapfile none swap sw 0 0' | sudo tee -a /etc/fstab
```

---

## Lokal frontendni SERVERDAGI backend bilan sinash

Buni **9-bosqichdan keyin** qiling — HTTPS ishlab turgan bo'lsin.

`front-nasl/vite.config.ts` da manzilni o'zgartiring:

```ts
const DJANGO_ORIGIN = "https://naslai.uz";
```

Va proksi sozlamasiga `changeOrigin` qo'shing, aks holda Django `Host`
sarlavhasini tanimay 400 qaytaradi:

```ts
proxy: {
  "/api":   { target: DJANGO_ORIGIN, changeOrigin: true },
  "/media": { target: DJANGO_ORIGIN, changeOrigin: true },
},
```

Serverda `/etc/naslai/naslai.env` ga vaqtincha qo'shing:

```ini
DJANGO_CORS_EXTRA_ORIGINS=http://127.0.0.1:5173
```

```bash
sudo systemctl restart naslai
```

Bu bitta o'zgaruvchi CORS'ni ham, CSRF ishonch ro'yxatini ham to'ldiradi —
`settings.py` da CSRF ro'yxati CORS ro'yxatidan olinadi.

**Sinovni tugatgach o'sha qatorni o'chiring va servisni qayta ishga
tushiring.** U turgan ekan, sizning kompyuteringizdagi istalgan sahifa
foydalanuvchilar nomidan so'rov yubora oladi.

---

## Keyingi yangilanishlar

```bash
cd /srv/naslai && git pull && .venv/bin/pip install -r requirements.txt
```

```bash
cd /srv/naslai && set -a && . /etc/naslai/naslai.env && set +a && .venv/bin/python manage.py migrate && .venv/bin/python manage.py collectstatic --noinput
```

Worker eski kodni xotirada ushlab turadi — uni ham qayta ishga tushiring:

```bash
sudo systemctl restart naslai naslai-worker
```

---

## Nima buzilsa, qayerga qarash

```bash
sudo journalctl -u naslai -n 100 --no-pager
```

```bash
sudo tail -50 /var/log/nginx/error.log
```

| Belgi | Sabab |
|---|---|
| 400 Bad Request | `DJANGO_ALLOWED_HOSTS` da domen yo'q |
| Kirish ishlamaydi, xato yo'q | HTTPS yo'q — `Secure` cookie o'rnatilmayapti |
| 403 CSRF | So'rov `apiFetch` dan o'tmagan yoki domen `CSRF_TRUSTED_ORIGINS` da yo'q |
| 502 Bad Gateway | Gunicorn o'chgan — `systemctl status naslai` |
| Rasmlar 404 | nginx `location /media/` yo'q yoki `NASLAI_MEDIA_ROOT` boshqa joyni ko'rsatyapti |
| Servis ishga tushmayapti | `DJANGO_SECRET_KEY` bo'sh — bu ataylab shunday |
| Generatsiya `navbatda` da qotib qoldi | Worker o'chgan — `systemctl status naslai-worker` |
| 503 QUEUE_UNAVAILABLE | Redis o'chgan. Tokenlar qaytarilgan, `systemctl status redis-server` |
| `Received unregistered task` | Worker eski kod bilan ishlayapti — `systemctl restart naslai-worker` |
