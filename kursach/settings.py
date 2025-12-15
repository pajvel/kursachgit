from dotenv import load_dotenv
load_dotenv()
from pathlib import Path
import os

# -------------------------
# Paths
# -------------------------
BASE_DIR = Path(__file__).resolve().parent.parent


# -------------------------
# Core security
# -------------------------
# В GitHub НЕ храним секреты. На Render обязательно задать SECRET_KEY.
SECRET_KEY = os.getenv("SECRET_KEY", "dev-only-secret-key-change-me")

# DEBUG: локально можно DEBUG=1, на Render ставь DEBUG=0
DEBUG = os.getenv("DEBUG", "0") == "1"

# ALLOWED_HOSTS: на Render поставь: "your-app.onrender.com"
# локально по умолчанию разрешаем localhost
ALLOWED_HOSTS = [h.strip() for h in os.getenv(
    "ALLOWED_HOSTS",
    "localhost,127.0.0.1"
).split(",") if h.strip()]


# -------------------------
# Applications
# -------------------------
INSTALLED_APPS = [
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",

    # твое приложение
    "football",
]


# -------------------------
# Middleware
# -------------------------
MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",

    # чтобы статика работала на Render без отдельного nginx
    "whitenoise.middleware.WhiteNoiseMiddleware",

    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
]


# -------------------------
# URL / WSGI
# -------------------------
ROOT_URLCONF = "kursach.urls"          # если у тебя проект НЕ kursach — поменяй тут и ниже
WSGI_APPLICATION = "kursach.wsgi.application"


# -------------------------
# Templates
# -------------------------
TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [],  # можно добавить BASE_DIR / "templates" если вынесешь шаблоны в корень
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.debug",
                "django.template.context_processors.request",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
            ],
        },
    },
]


# -------------------------
# Database (PostgreSQL)
# -------------------------
# Render/Railway обычно дают либо DATABASE_URL, либо набор PG*.
# Тут используем PG* (самое простое и без доп.пакетов).
DATABASES = {
    "default": {
        "ENGINE": "django.db.backends.postgresql",
        "NAME": os.getenv("PGDATABASE", "cursach"),
        "USER": os.getenv("PGUSER", "postgres"),
        "PASSWORD": os.getenv("PGPASSWORD", ""),
        "HOST": os.getenv("PGHOST", "localhost"),
        "PORT": os.getenv("PGPORT", "5432"),
    }
}


# -------------------------
# Password validation
# -------------------------
AUTH_PASSWORD_VALIDATORS = [
    {"NAME": "django.contrib.auth.password_validation.UserAttributeSimilarityValidator"},
    {"NAME": "django.contrib.auth.password_validation.MinimumLengthValidator"},
    {"NAME": "django.contrib.auth.password_validation.CommonPasswordValidator"},
    {"NAME": "django.contrib.auth.password_validation.NumericPasswordValidator"},
]


# -------------------------
# i18n
# -------------------------
LANGUAGE_CODE = "ru-ru"
TIME_ZONE = "Europe/Amsterdam"
USE_I18N = True
USE_TZ = True


# -------------------------
# Static files
# -------------------------
STATIC_URL = "/static/"

# Render будет запускать collectstatic → все сложится сюда
STATIC_ROOT = BASE_DIR / "staticfiles"

# Если у тебя статика лежит в football/static (как в твоём архиве) —
# Django и так ее увидит через AppDirectoriesFinder.
# Но если хочешь явно (не обязательно), можно раскомментировать:
# STATICFILES_DIRS = [BASE_DIR / "football" / "static"]

# Whitenoise: хранить статику с хэшами (норм для прода)
STATICFILES_STORAGE = "whitenoise.storage.CompressedManifestStaticFilesStorage"


# -------------------------
# Default primary key
# -------------------------
DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"


# -------------------------
# Basic production security (не ломает локалку)
# -------------------------
if not DEBUG:
    SESSION_COOKIE_SECURE = True
    CSRF_COOKIE_SECURE = True
    SECURE_SSL_REDIRECT = True
    SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")
