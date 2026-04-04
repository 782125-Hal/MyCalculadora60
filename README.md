# MyCalculadora60 — Gestión de Préstamos

Aplicación web desarrollada con **Django 5** y **Django REST Framework** para gestionar préstamos personales: registro de clientes, cálculo de amortización, seguimiento de pagos e incrementos de capital.

---

## Funcionalidades

- Calculadora financiera: calcula pago mensual o plazo según el modo elegido
- Registro de préstamos en modo **plazo fijo** o **pago fijo**
- Tabla de amortización automática (mensual o semanal)
- Registro de pagos e incrementos de capital
- Actualización automática de saldo con cargos por mora
- API REST completa (`/api/`) con Django REST Framework

---

## Correr en local

```bash
# 1. Clonar el repositorio
git clone https://github.com/782125-Hal/MyCalculadora60.git
cd MyCalculadora60

# 2. Crear entorno virtual e instalar dependencias
python -m venv .venv
source .venv/bin/activate        # Mac/Linux
.venv\Scripts\activate           # Windows

pip install -r requirements.txt

# 3. Configurar variables de entorno
cp .env.example .env
# Edita .env con tus valores reales

# 4. Aplicar migraciones y correr el servidor
python manage.py migrate
python manage.py runserver
```

Abre `http://127.0.0.1:8000/` en tu navegador.

---

## Variables de entorno requeridas

Copia `.env.example` como `.env` y completa los valores:

| Variable | Descripción |
|---|---|
| `SECRET_KEY` | Clave secreta de Django (genera una única para producción) |
| `DEBUG` | `True` en desarrollo, `False` en producción |
| `ALLOWED_HOSTS` | Dominios permitidos, separados por coma |
| `DATABASE_URL` | URL de PostgreSQL. Si se omite, usa SQLite local |

Generar una `SECRET_KEY` segura:
```bash
python -c "from django.core.management.utils import get_random_secret_key; print(get_random_secret_key())"
```

---

## API REST

El browser de la API está disponible en `/api/` cuando el servidor está corriendo.

| Endpoint | Descripción |
|---|---|
| `GET/POST /api/prestamos/` | Listar y crear préstamos |
| `GET /api/prestamos/{id}/` | Detalle con amortización y movimientos |
| `POST /api/prestamos/{id}/registrar_pago/` | Registrar un pago |
| `POST /api/prestamos/calcular/` | Calcular pago o plazo sin guardar |
| `GET/POST /api/clientes/` | Gestión de clientes |
| `GET/POST /api/movimientos/` | Gestión de movimientos |

---

## Deploy en Railway

Ver [.env.example](.env.example) para las variables que debes configurar.

1. Crear proyecto en [railway.app](https://railway.app)
2. Conectar este repositorio de GitHub
3. Agregar servicio **PostgreSQL** — Railway genera `DATABASE_URL` automáticamente
4. Configurar las variables de entorno en el panel de Railway
5. Railway despliega automáticamente al hacer `git push`

El `Procfile` ejecuta las migraciones y levanta el servidor automáticamente en cada deploy.

---

## Stack tecnológico

- Python 3.13
- Django 5.2
- Django REST Framework 3.17
- PostgreSQL (producción) / SQLite (desarrollo)
- Uvicorn (servidor ASGI)
- WhiteNoise (archivos estáticos)
