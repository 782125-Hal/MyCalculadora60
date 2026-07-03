import sys
from pathlib import Path
import dj_database_url
from decouple import config
from django.core.exceptions import ImproperlyConfigured

# True cuando corremos la batería de pruebas (`manage.py test`).
TESTING = 'test' in sys.argv

BASE_DIR = Path(__file__).resolve().parent.parent

# ============================================================
# Seguridad base
# ============================================================
DEBUG = config('DEBUG', default=True, cast=bool)   # True por defecto para desarrollo local

# SECRET_KEY: en desarrollo se permite un fallback; en producción DEBE venir del
# entorno. Si en producción se usara la clave insegura, se podrían falsificar
# sesiones y tokens, por eso fallamos de forma explícita.
SECRET_KEY = config('SECRET_KEY', default='django-insecure-dev-key-change-in-production')
if not DEBUG and SECRET_KEY.startswith('django-insecure-'):
    raise ImproperlyConfigured(
        "SECRET_KEY no está configurada. Define la variable de entorno SECRET_KEY "
        "en producción (genera una con "
        "`python -c \"from django.core.management.utils import get_random_secret_key; "
        "print(get_random_secret_key())\"`)."
    )

# ALLOWED_HOSTS: dominios concretos. '*' abre la puerta a Host-header injection,
# así que ya no es el valor por defecto.
ALLOWED_HOSTS = [h.strip() for h in config('ALLOWED_HOSTS', default='').split(',') if h.strip()]

# CSRF_TRUSTED_ORIGINS: necesario detrás del proxy HTTPS de Railway para que los
# POST no sean rechazados (Django 4+).
CSRF_TRUSTED_ORIGINS = [
    o.strip() for o in config('CSRF_TRUSTED_ORIGINS', default='').split(',') if o.strip()
]

# Railway expone el dominio público asignado en esta variable; lo añadimos
# automáticamente a hosts y orígenes de confianza.
RAILWAY_PUBLIC_DOMAIN = config('RAILWAY_PUBLIC_DOMAIN', default='')
if RAILWAY_PUBLIC_DOMAIN:
    if RAILWAY_PUBLIC_DOMAIN not in ALLOWED_HOSTS:
        ALLOWED_HOSTS.append(RAILWAY_PUBLIC_DOMAIN)
    trusted = f'https://{RAILWAY_PUBLIC_DOMAIN}'
    if trusted not in CSRF_TRUSTED_ORIGINS:
        CSRF_TRUSTED_ORIGINS.append(trusted)

if DEBUG:
    # Hosts habituales de desarrollo local.
    ALLOWED_HOSTS += [h for h in ['localhost', '127.0.0.1', '[::1]'] if h not in ALLOWED_HOSTS]
elif not ALLOWED_HOSTS:
    raise ImproperlyConfigured(
        "ALLOWED_HOSTS debe especificar dominios concretos en producción. "
        "Define ALLOWED_HOSTS (separado por comas) o RAILWAY_PUBLIC_DOMAIN."
    )

# Application definition
INSTALLED_APPS = [
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    "prestamos",
    "rest_framework",
    "axes",  # Rate limiting / bloqueo de fuerza bruta en el login
]

REST_FRAMEWORK = {
    'DEFAULT_PERMISSION_CLASSES': [
        'rest_framework.permissions.IsAuthenticated',
    ],
    'DEFAULT_AUTHENTICATION_CLASSES': [
        'rest_framework.authentication.SessionAuthentication',
        'rest_framework.authentication.BasicAuthentication',  # útil para browsable API y tests
    ],
    'DEFAULT_PAGINATION_CLASS': 'rest_framework.pagination.PageNumberPagination',
    'PAGE_SIZE': 20,
    # Throttling: limita peticiones para mitigar abuso/fuerza bruta en la API.
    'DEFAULT_THROTTLE_CLASSES': [
        'rest_framework.throttling.AnonRateThrottle',
        'rest_framework.throttling.UserRateThrottle',
    ],
    'DEFAULT_THROTTLE_RATES': {
        'anon': '30/min',
        'user': '120/min',
    },
}

# django-axes: backend de autenticación que bloquea tras varios intentos fallidos.
# AxesStandaloneBackend debe ir primero para poder cortar antes de ModelBackend.
AUTHENTICATION_BACKENDS = [
    'axes.backends.AxesStandaloneBackend',
    'django.contrib.auth.backends.ModelBackend',
]

# Configuración de bloqueo por fuerza bruta.
AXES_FAILURE_LIMIT = config('AXES_FAILURE_LIMIT', default=5, cast=int)   # intentos permitidos
AXES_COOLOFF_TIME = config('AXES_COOLOFF_TIME', default=1, cast=int)     # horas de bloqueo
AXES_LOCKOUT_PARAMETERS = [["username", "ip_address"]]  # bloquea por combinación usuario+IP
AXES_RESET_ON_SUCCESS = True   # limpia el contador tras un login exitoso
AXES_BEHIND_REVERSE_PROXY = not DEBUG  # Railway va detrás de proxy
# Desactivado durante los tests (el cliente de pruebas no pasa `request` a
# authenticate()); configurable por entorno para el resto de casos.
AXES_ENABLED = config('AXES_ENABLED', default=not TESTING, cast=bool)

# Autenticación
LOGIN_URL = 'login'
LOGIN_REDIRECT_URL = '/'
LOGOUT_REDIRECT_URL = '/'

MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "whitenoise.middleware.WhiteNoiseMiddleware",  # Sirve archivos estáticos en producción
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
    # AxesMiddleware debe ir al final, después de AuthenticationMiddleware.
    "axes.middleware.AxesMiddleware",
]

ROOT_URLCONF = "MyCalculadora60.urls"

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [BASE_DIR / 'templates'],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                'django.template.context_processors.debug',
                "django.template.context_processors.request",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
            ],
        },
    },
]

WSGI_APPLICATION = "MyCalculadora60.wsgi.application"

# Base de datos: PostgreSQL en producción (Railway), SQLite en desarrollo
DATABASE_URL = config('DATABASE_URL', default=f'sqlite:///{BASE_DIR / "db.sqlite3"}')
DATABASES = {
    'default': dj_database_url.parse(DATABASE_URL, conn_max_age=600)
}

# Validación de contraseñas
AUTH_PASSWORD_VALIDATORS = [
    {"NAME": "django.contrib.auth.password_validation.UserAttributeSimilarityValidator"},
    {"NAME": "django.contrib.auth.password_validation.MinimumLengthValidator"},
    {"NAME": "django.contrib.auth.password_validation.CommonPasswordValidator"},
    {"NAME": "django.contrib.auth.password_validation.NumericPasswordValidator"},
]

# ============================================================
# Logging: los errores internos se registran (no se exponen al usuario)
# ============================================================
LOGGING = {
    'version': 1,
    'disable_existing_loggers': False,
    'formatters': {
        'verbose': {'format': '[{asctime}] {levelname} {name}: {message}', 'style': '{'},
    },
    'handlers': {
        'console': {'class': 'logging.StreamHandler', 'formatter': 'verbose'},
    },
    'root': {'handlers': ['console'], 'level': 'WARNING'},
    'loggers': {
        'prestamos': {'handlers': ['console'], 'level': 'INFO', 'propagate': False},
    },
}

# Internacionalización
LANGUAGE_CODE = "es-mx"
TIME_ZONE = "America/Mexico_City"
USE_I18N = True
USE_TZ = True

# Archivos estáticos
STATIC_URL = "static/"
STATIC_ROOT = BASE_DIR / "staticfiles"
STATICFILES_STORAGE = "whitenoise.storage.CompressedManifestStaticFilesStorage"

DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

# ============================================================
# Cabeceras y cookies de seguridad (aplican siempre)
# ============================================================
SECURE_CONTENT_TYPE_NOSNIFF = True          # X-Content-Type-Options: nosniff
X_FRAME_OPTIONS = 'DENY'                     # Anti clickjacking
SESSION_COOKIE_HTTPONLY = True              # La cookie de sesión no es accesible por JS
SESSION_COOKIE_SAMESITE = 'Lax'
CSRF_COOKIE_SAMESITE = 'Lax'

# Seguridad en producción (se activan solo cuando DEBUG=False)
if not DEBUG:
    # Railway (y la mayoría de plataformas cloud) terminan HTTPS externamente
    # y pasan HTTP internamente. SECURE_SSL_REDIRECT causaría un loop infinito.
    # En su lugar, le decimos a Django que confíe en el header X-Forwarded-Proto
    # que Railway envía para indicar que la conexión original fue HTTPS.
    SECURE_PROXY_SSL_HEADER = ('HTTP_X_FORWARDED_PROTO', 'https')
    # Ahora que Django confía en X-Forwarded-Proto (arriba), el redirect a HTTPS
    # ya NO provoca el loop infinito que hubo antes. Se deja configurable por si
    # alguna vez hiciera falta desactivarlo sin redeploy de código.
    SECURE_SSL_REDIRECT = config('SECURE_SSL_REDIRECT', default=True, cast=bool)
    SECURE_HSTS_SECONDS = 31536000
    SECURE_HSTS_INCLUDE_SUBDOMAINS = True
    SECURE_HSTS_PRELOAD = True
    SESSION_COOKIE_SECURE = True
    CSRF_COOKIE_SECURE = True
