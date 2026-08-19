"""Django-настройки Naslai.

Здесь же вся защита периметра: список доменов, CORS, cookie, заголовки
безопасности и ограничение частоты запросов. Меняете что-то в этом файле —
сначала прочитайте комментарий рядом: почти каждое значение здесь стоит
не «по умолчанию», а по конкретной причине.
"""

import os
from pathlib import Path

from corsheaders.defaults import default_headers, default_methods
from django.core.exceptions import ImproperlyConfigured

BASE_DIR = Path(__file__).resolve().parent.parent


# --------------------------------------------------------------- окружение

def env_bool(name: str, default: bool = False) -> bool:
    raw = os.environ.get(name)
    if raw is None or raw == "":
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def env_list(name: str, default: list[str] | None = None) -> list[str]:
    raw = os.environ.get(name, "")
    items = [item.strip() for item in raw.split(",") if item.strip()]
    return items or list(default or [])


def load_env_file(path: Path) -> None:
    """Читает .env в окружение процесса.

    Своя реализация вместо python-dotenv: зависимость ради двадцати строк
    не нужна, а лишний пакет в списке — лишний повод для расхождения версий.

    Уже заданные переменные НЕ переопределяем. Это важно: на сервере
    значения выставляет systemd, и забытый рядом с кодом файл разработчика
    не должен молча подменить боевые настройки.
    """
    if not path.is_file():
        return

    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export "):].lstrip()

        key, separator, value = line.partition("=")
        if not separator:
            continue

        key = key.strip()
        value = value.strip()
        # Кавычки снимаем: без них значение с пробелом пришлось бы писать
        # неудобно, а с ними кавычки попали бы прямо в переменную.
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]

        if key and key not in os.environ:
            os.environ[key] = value


# Файл рядом с manage.py. Путь можно перенести: NASLAI_ENV_FILE=/etc/naslai.env
load_env_file(Path(os.environ.get("NASLAI_ENV_FILE") or BASE_DIR / ".env"))


# DEBUG по умолчанию ВЫКЛЮЧЕН. Забыть выставить DJANGO_DEBUG=0 на боевом
# сервере — самая дорогая ошибка из возможных: страница ошибки показывает
# SECRET_KEY, ключ OpenAI и всё остальное окружение. Для локальной работы
# ставьте DJANGO_DEBUG=1 явно.
DEBUG = env_bool("DJANGO_DEBUG", False)

# Домен сервиса. Всё остальное — списки хостов, CORS, CSRF — считается
# отсюда, чтобы не пришлось править адрес в четырёх местах.
SITE_DOMAIN = os.environ.get("NASLAI_SITE_DOMAIN", "naslai.uz").strip().lower()

SECRET_KEY = os.environ.get("DJANGO_SECRET_KEY", "").strip()
if not SECRET_KEY:
    if not DEBUG:
        raise ImproperlyConfigured(
            "DJANGO_SECRET_KEY не задан. На боевом сервере ключ обязателен: "
            "на нём держатся сессии и подписи CSRF. Сгенерировать можно "
            "командой: python -c 'import secrets; print(secrets.token_urlsafe(64))'"
        )
    SECRET_KEY = "django-insecure-local-development-only-do-not-deploy"


# ------------------------------------------------------------------ хосты

# Отвечаем только на свои имена. Запрос с чужим заголовком Host получает
# 400 и до кода приложения не доходит — так не работает подмена ссылок и
# отравление кэша.
ALLOWED_HOSTS = env_list(
    "DJANGO_ALLOWED_HOSTS",
    [SITE_DOMAIN, f"www.{SITE_DOMAIN}", f"api.{SITE_DOMAIN}"],
)
# Локальные адреса нужны всегда: с них ходят nginx, gunicorn и проверки
# доступности на самой машине.
for _host in ("127.0.0.1", "localhost", "[::1]"):
    if _host not in ALLOWED_HOSTS:
        ALLOWED_HOSTS.append(_host)


# ------------------------------------------------------------- приложения

INSTALLED_APPS = [
    'django.contrib.admin',
    'django.contrib.auth',
    'django.contrib.contenttypes',
    'django.contrib.sessions',
    'django.contrib.messages',
    'django.contrib.staticfiles',
    'rest_framework',
    'rest_framework.authtoken',
    'corsheaders',
    'accounts',
    'billing',
    'generation',
    'promos',
    'adminapi',
    'audit',
]

MIDDLEWARE = [
    'django.middleware.security.SecurityMiddleware',
    # CorsMiddleware стоит выше CommonMiddleware намеренно: иначе на
    # ответ-редирект заголовки CORS уже не попадут.
    'corsheaders.middleware.CorsMiddleware',
    'django.contrib.sessions.middleware.SessionMiddleware',
    'django.middleware.common.CommonMiddleware',
    'django.middleware.csrf.CsrfViewMiddleware',
    'django.contrib.auth.middleware.AuthenticationMiddleware',
    'django.contrib.messages.middleware.MessageMiddleware',
    'django.middleware.clickjacking.XFrameOptionsMiddleware',
]

AUTH_USER_MODEL = "accounts.User"

# Адрес админки. /admin/ перебирают боты круглосуточно; смена пути на
# что-то своё убирает почти весь этот шум из логов и из попыток подбора.
DJANGO_ADMIN_PATH = os.environ.get("DJANGO_ADMIN_PATH", "admin").strip("/") or "admin"

ROOT_URLCONF = 'config.urls'
WSGI_APPLICATION = 'config.wsgi.application'


# --------------------------------------------------------------------- DRF

REST_FRAMEWORK = {
    "DEFAULT_AUTHENTICATION_CLASSES": [
        # Браузер ходит с cookie сессии, внешние интеграции — с токеном.
        "rest_framework.authentication.SessionAuthentication",
        "rest_framework.authentication.TokenAuthentication",
    ],
    "DEFAULT_PERMISSION_CLASSES": ["rest_framework.permissions.IsAuthenticated"],
    # Ответ об ошибке приводим к виду {"error": ..., "message": ...} —
    # фронтенд читает поле message и показывает его человеку.
    "EXCEPTION_HANDLER": "config.api_errors.api_exception_handler",
    # Ограничение частоты. Без него страницу входа перебирают паролями, а
    # генерацию — запросами: каждый стоит денег.
    "DEFAULT_THROTTLE_CLASSES": [
        "rest_framework.throttling.AnonRateThrottle",
        "rest_framework.throttling.UserRateThrottle",
    ],
    "DEFAULT_THROTTLE_RATES": {
        "anon": os.environ.get("THROTTLE_ANON", "60/min"),
        "user": os.environ.get("THROTTLE_USER", "240/min"),
        # Подбор пароля и накрутка регистраций.
        "login": os.environ.get("THROTTLE_LOGIN", "10/min"),
        "register": os.environ.get("THROTTLE_REGISTER", "20/hour"),
        # Перебор промокодов.
        "promo": os.environ.get("THROTTLE_PROMO", "10/hour"),
        # Обращения к платному API.
        "generation": os.environ.get("THROTTLE_GENERATION", "30/hour"),
        "audit": os.environ.get("THROTTLE_AUDIT", "20/hour"),
        "analyze": os.environ.get("THROTTLE_ANALYZE", "60/hour"),
    },
}

if not DEBUG:
    # Просматриваемое HTML-API отключено: оно раскрывает структуру
    # эндпоинтов и допустимые поля любому, кто откроет адрес в браузере.
    REST_FRAMEWORK["DEFAULT_RENDERER_CLASSES"] = ["rest_framework.renderers.JSONRenderer"]


# ------------------------------------------------------------------ Celery

# Адрес Redis. ПУСТО = очереди нет, и задача выполняется прямо в запросе,
# как раньше. Это не заглушка «на потом», а рабочий режим:
#
#   * локальная разработка под Windows — там нет ни Redis, ни нормального
#     воркера Celery, и поднимать их ради одной задачи незачем;
#   * тесты — им нужен предсказуемый результат сразу после запроса.
#
# На боевом сервере переменную ЗАПОЛНЯЮТ, и генерация уходит в фон.
CELERY_BROKER_URL = os.environ.get("CELERY_BROKER_URL", "").strip()

# Ключевая строчка: без брокера задача считается выполненной на месте.
CELERY_TASK_ALWAYS_EAGER = not CELERY_BROKER_URL
# В синхронном режиме ошибка задачи должна долетать до вызывающего кода,
# иначе она молча проглатывается и генерация «зависает» без причины.
CELERY_TASK_EAGER_PROPAGATES = True

# Результат задачи мы нигде не читаем: состояние генерации лежит в базе,
# в полях status/progress, и фронтенд опрашивает именно их. Хранилище
# результатов только занимало бы память Redis.
CELERY_RESULT_BACKEND = None

CELERY_TASK_SERIALIZER = "json"
CELERY_ACCEPT_CONTENT = ["json"]
# Расписаний нет, поэтому часовой пояс очереди ни на что не влияет —
# оставляем UTC, как у самой Celery по умолчанию.
CELERY_ENABLE_UTC = True

# Один воркер берёт одну задачу за раз. Иначе он расхватывает очередь
# впрок, и задачи ждут занятого воркера, пока свободный простаивает.
CELERY_WORKER_PREFETCH_MULTIPLIER = 1

# Подтверждаем задачу СРАЗУ, до выполнения (acks_late=False, умолчание).
# Обратный вариант выглядит надёжнее, но здесь он опасен: упавший воркер
# вернул бы задачу в очередь, и мы заплатили бы OpenAI второй раз за одну
# оплаченную пользователем генерацию. Зависшие задачи вместо этого
# подбирает `manage.py release_stuck_jobs` — он же возвращает токены.
CELERY_TASK_ACKS_LATE = False

# Повторов НЕТ и быть не должно: каждый повтор — это ещё один платный
# вызов. Упавшая генерация возвращает токены, человек решает сам.
CELERY_TASK_DEFAULT_RETRY_DELAY = 0

# Пакет из пяти картинок при медленном ответе провайдера идёт до 15 минут.
CELERY_TASK_SOFT_TIME_LIMIT = int(os.environ.get("CELERY_SOFT_TIME_LIMIT", 20 * 60))
CELERY_TASK_TIME_LIMIT = int(os.environ.get("CELERY_TIME_LIMIT", 25 * 60))

# Без этого воркер падает при старте, если Redis поднимается чуть позже.
CELERY_BROKER_CONNECTION_RETRY_ON_STARTUP = True

# Задача старше этого срока считается зависшей: см. release_stuck_jobs.
STUCK_JOB_MINUTES = int(os.environ.get("STUCK_JOB_MINUTES", 30))


# -------------------------------------------------------------------- CORS

# Список доменов, которым браузер разрешит читать наши ответы. Звёздочки
# здесь нет и быть не должно: вместе с CORS_ALLOW_CREDENTIALS любой чужой
# сайт смог бы дёргать API от имени вошедшего человека.
CORS_ALLOW_ALL_ORIGINS = False

CORS_ALLOWED_ORIGINS = [
    f"https://{SITE_DOMAIN}",
    f"https://www.{SITE_DOMAIN}",
]

if DEBUG:
    # Только для локальной разработки. Порты — Vite: dev 5173/5174 и
    # preview 4173/4174.
    CORS_ALLOWED_ORIGINS += [
        "http://127.0.0.1:5173", "http://localhost:5173",
        "http://127.0.0.1:5174", "http://localhost:5174",
        "http://127.0.0.1:4173", "http://localhost:4173",
        "http://127.0.0.1:4174", "http://localhost:4174",
    ]

# Запасной вариант для отдельного стенда. Пусто — и правильно: каждый
# добавленный сюда адрес получает доступ к сессиям пользователей.
CORS_ALLOWED_ORIGINS += [
    origin for origin in env_list("DJANGO_CORS_EXTRA_ORIGINS")
    if origin not in CORS_ALLOWED_ORIGINS
]

CORS_ALLOW_CREDENTIALS = True

# Idempotency-Key фронтенд шлёт при создании генерации. Без него в списке
# предварительный запрос (preflight) отклоняется, и кнопка «Создать»
# перестаёт работать, когда сайт и API на разных доменах.
CORS_ALLOW_HEADERS = list(default_headers) + ["idempotency-key"]
CORS_ALLOW_METHODS = list(default_methods)
CORS_PREFLIGHT_MAX_AGE = 3600

# Админку и всё остальное через CORS не отдаём — только API и картинки.
CORS_URLS_REGEX = r"^/(api|media)/.*$"


# -------------------------------------------------------------------- CSRF

# Список для проверки заголовка Origin при небезопасных методах.
CSRF_TRUSTED_ORIGINS = list(CORS_ALLOWED_ORIGINS)

CSRF_COOKIE_SECURE = not DEBUG
CSRF_COOKIE_SAMESITE = "Lax"
# Читается JavaScript-ом и уходит обратно заголовком X-CSRFToken —
# httponly здесь сделал бы форму входа неработающей.
CSRF_COOKIE_HTTPONLY = False

SESSION_COOKIE_SECURE = not DEBUG
SESSION_COOKIE_SAMESITE = "Lax"
# JavaScript до cookie сессии не добирается: украсть её через XSS нельзя.
SESSION_COOKIE_HTTPONLY = True
SESSION_COOKIE_AGE = 60 * 60 * 24 * 14
SESSION_SAVE_EVERY_REQUEST = True

# Заполняется, только если сайт и API живут на разных поддоменах
# (naslai.uz и api.naslai.uz): тогда нужен общий домен cookie «.naslai.uz».
_cookie_domain = os.environ.get("DJANGO_COOKIE_DOMAIN", "").strip()
if _cookie_domain:
    SESSION_COOKIE_DOMAIN = _cookie_domain
    CSRF_COOKIE_DOMAIN = _cookie_domain


# ------------------------------------------------- заголовки безопасности

# Перенаправление на HTTPS обычно делает nginx. Если он этого не делает —
# поставьте DJANGO_SECURE_SSL_REDIRECT=1.
SECURE_SSL_REDIRECT = env_bool("DJANGO_SECURE_SSL_REDIRECT", False)
# За обратным прокси Django иначе считает соединение незашифрованным.
SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")

# Год HSTS. Включайте, ТОЛЬКО когда HTTPS уже работает на всех
# поддоменах: браузер запомнит правило, и обратно откатиться нельзя.
SECURE_HSTS_SECONDS = 0 if DEBUG else int(os.environ.get("DJANGO_HSTS_SECONDS", 31536000))
SECURE_HSTS_INCLUDE_SUBDOMAINS = env_bool("DJANGO_HSTS_SUBDOMAINS", True)
SECURE_HSTS_PRELOAD = env_bool("DJANGO_HSTS_PRELOAD", False)

SECURE_CONTENT_TYPE_NOSNIFF = True
SECURE_REFERRER_POLICY = "strict-origin-when-cross-origin"
# Вход через Google открывает своё окно, поэтому allow-popups.
SECURE_CROSS_ORIGIN_OPENER_POLICY = "same-origin-allow-popups"
# Наш сайт нельзя открыть внутри чужого фрейма — защита от подмены кликов.
X_FRAME_OPTIONS = "DENY"

# Фотографии приходят строкой data:base64 внутри JSON. Разбор карточки
# принимает до четырёх картинок сразу, поэтому предел по умолчанию
# (2.5 МБ) для нас мал. Верхняя граница нужна: без неё один запрос на
# сотни мегабайт кладёт процесс.
DATA_UPLOAD_MAX_MEMORY_SIZE = int(os.environ.get("DJANGO_MAX_BODY_BYTES", 20 * 1024 * 1024))
FILE_UPLOAD_MAX_MEMORY_SIZE = 10 * 1024 * 1024
DATA_UPLOAD_MAX_NUMBER_FIELDS = 200


# ----------------------------------------------------------------- шаблоны

TEMPLATES = [
    {
        'BACKEND': 'django.template.backends.django.DjangoTemplates',
        'DIRS': [],
        'APP_DIRS': True,
        'OPTIONS': {
            'context_processors': [
                'django.template.context_processors.request',
                'django.contrib.auth.context_processors.auth',
                'django.contrib.messages.context_processors.messages',
            ],
        },
    },
]


# --------------------------------------------------------------------- БД

# Путь к базе вынесен в окружение: на боевом сервере она не должна лежать
# внутри папки с кодом — иначе выкладка новой версии может её затереть.
DATABASES = {
    "default": {
        "ENGINE": "django.db.backends.sqlite3",
        "NAME": os.environ.get("NASLAI_DB_PATH", str(BASE_DIR / "db.sqlite3")),
    }
}


AUTH_PASSWORD_VALIDATORS = [
    {'NAME': 'django.contrib.auth.password_validation.UserAttributeSimilarityValidator'},
    {
        'NAME': 'django.contrib.auth.password_validation.MinimumLengthValidator',
        'OPTIONS': {'min_length': 8},
    },
    {'NAME': 'django.contrib.auth.password_validation.CommonPasswordValidator'},
    {'NAME': 'django.contrib.auth.password_validation.NumericPasswordValidator'},
]


LANGUAGE_CODE = 'en-us'
TIME_ZONE = os.environ.get("DJANGO_TIME_ZONE", "Asia/Tashkent")
USE_I18N = True
USE_TZ = True

STATIC_URL = 'static/'
STATIC_ROOT = os.environ.get("DJANGO_STATIC_ROOT", str(BASE_DIR / "staticfiles"))

MEDIA_URL = "/media/"
MEDIA_ROOT = os.environ.get("NASLAI_MEDIA_ROOT", str(BASE_DIR / "media"))

DEFAULT_AUTO_FIELD = 'django.db.models.BigAutoField'


# ------------------------------------------------------------------- логи

LOGGING = {
    "version": 1,
    "disable_existing_loggers": False,
    "formatters": {"simple": {"format": "{asctime} {levelname} {name}: {message}", "style": "{"}},
    "handlers": {"console": {"class": "logging.StreamHandler", "formatter": "simple"}},
    "root": {"handlers": ["console"], "level": os.environ.get("DJANGO_LOG_LEVEL", "INFO")},
    "loggers": {
        # Отказы по чужому заголовку Host и сработавшие проверки CSRF
        # нужно видеть: это самый заметный след попытки подобраться.
        "django.security": {"handlers": ["console"], "level": "WARNING", "propagate": False},
    },
}
