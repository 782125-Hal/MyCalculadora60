# MyCalculadora60 — Gestión de Préstamos

Aplicación web desarrollada con **Django 5** y **Django REST Framework** para gestionar préstamos personales: registro de clientes, cálculo de amortización, seguimiento de pagos e incrementos de capital.

---

## Funcionalidades

- Calculadora financiera: calcula pago mensual o plazo según el modo elegido
- Registro de préstamos en modo **plazo fijo** o **pago fijo**
- Registro de **deudas propias** (casa, terreno, auto) con sus pagos realizados
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

**Nota (desde Fase 2):** Toda la aplicación requiere iniciar sesión. 
Crea un superusuario con:
```bash
python manage.py createsuperuser
```
Luego inicia sesión en `/accounts/login/`. La API también requiere autenticación (usa la sesión del navegador o Basic Auth).

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

## Préstamos vs. deudas

Un mismo modelo (`Prestamo`) cubre los dos sentidos del dinero mediante el campo `rol`:

| `rol` | Significado | Un movimiento de tipo `pago` es… |
|---|---|---|
| `prestamo` | Presté dinero y me deben | un abono que me hizo el cliente |
| `deuda` | Debo dinero por una compra a plazos | un abono que yo le hice al acreedor |

En ambos casos el saldo baja con los pagos y sube con los cargos de los períodos
no cubiertos, así que la amortización y `actualizar_saldo()` son las mismas. Una
deuda sin intereses es simplemente `tasa_interes_anual = 0`. El campo `concepto`
guarda qué se compró (ej. "Terreno en Misiones").

El dashboard muestra los dos lados por separado —"Me deben" y "Yo debo"— porque
son magnitudes opuestas y sumarlas en un solo total no significaría nada. Filtra
con `?rol=deuda` o `?rol=prestamo` en `/prestamos/lista-prestamos/`.

---

## Portafolio de inversiones

Consolida posiciones de CetesDirecto, Briq.mx u otras plataformas en `/prestamos/portafolio/`.

**No se conecta a ninguna plataforma, y es deliberado.** Ni CetesDirecto —cuyas Reglas
de Operación sólo contemplan web, apps móviles e IVR— ni Briq.mx exponen API pública.
Automatizar su login sería frágil (2FA, cambios de maquetado), contrario a sus términos
y arriesgaría el bloqueo de una cuenta con dinero real.

No hace falta: el rendimiento es determinista desde la compra. Con monto, tasa y plazo
capturados una vez, el valor a cualquier fecha es aritmética.

| Tipo | Cómo se valúa |
|---|---|
| A descuento (CETES, Bondes) | `valor = monto · (1 + r · días / 360)` |
| Tasa fija a plazo (Briq, pagarés) | Igual, con base 365 |
| Fondo (BONDDIA, ENERFIN) | **No se proyecta** — depende del precio diario de la acción, se captura |

La base 360 y el devengo salen de la fórmula de Banxico para CETES,
`P = VN / (1 + r · t / 360)`. Despejada, el monto invertido crece hasta el valor
nominal exactamente al vencimiento; hay un test que lo comprueba partiendo del
precio de descuento (`test_el_devengo_reproduce_el_valor_nominal_al_vencimiento`).

Los rendimientos que ya cobraste se registran como movimientos: en Briq salen de la
posición a tu bolsillo, y sin contarlos el rendimiento total quedaría subestimado.
Concilia contra tu estado de cuenta; si un valor no cuadra, `valor_manual` lo fuerza.

La aritmética vive en [`prestamos/portafolio.py`](prestamos/portafolio.py), en `Decimal` puro.

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

### Cierre automático de períodos (cron)

Los cargos de interés se generan al recalcular el saldo. Sin un cron, eso sólo
ocurre cuando alguien abre el dashboard, la lista o el detalle: si nadie entra en
todo el mes, los cargos no existen hasta la siguiente visita.

El comando `cerrar_periodos` lo hace por fuera. Para automatizarlo en Railway hace
falta un **segundo servicio** en el mismo proyecto, apuntando a este mismo repo:
Railway salta las ejecuciones programadas mientras haya un deploy activo, y el
servicio web corre `uvicorn` de forma permanente, así que el cron nunca dispararía
si se configurara sobre él.

Configuración del servicio cron:

| Ajuste (Settings del servicio) | Valor |
|---|---|
| Config-as-code file path | `/railway.cron.json` |
| Variables | `DATABASE_URL` y `SECRET_KEY`, referenciadas del mismo Postgres y entorno que el web |

El resto —comando, horario y política de reinicio— vive en
[`railway.cron.json`](railway.cron.json), versionado junto al código:

```json
"startCommand": "ALLOWED_HOSTS=mycalculadora60-production.up.railway.app python manage.py cerrar_periodos",
"cronSchedule": "0 12 * * *",
"restartPolicyType": "NEVER"
```

El `ALLOWED_HOSTS` antepuesto al comando no es decorativo: `settings.py` lo exige
cuando `DEBUG=False`, y sin él el proceso muere al arrancar con
`ImproperlyConfigured` —en un cron, silenciosamente—. El comando no atiende HTTP,
así que el valor sólo satisface esa validación; se usa el dominio real para no
confundir a quien lea el archivo. Va en el `startCommand` porque `railway.json`
**no admite un bloque de variables de entorno**, y así queda versionado en lugar
de vivir sólo en el panel, donde se perdería al recrear el servicio.

`0 12 * * *` es 06:00 en `America/Mexico_City` (Railway programa en UTC; México
no aplica horario de verano desde 2022, así que el desfase es constante).
Railway no permite intervalos menores a 5 minutos.

El comando es idempotente —`actualizar_saldo()` purga y regenera los cargos— así
que repetirlo no duplica nada, y cierra la conexión a la base al terminar para que
el proceso salga y no bloquee la siguiente ejecución.

```bash
python manage.py cerrar_periodos                  # hasta hoy
python manage.py cerrar_periodos --hasta 2026-01-01
python manage.py cerrar_periodos --rol deuda      # sólo deudas propias
```

---

## Stack tecnológico

- Python 3.13
- Django 5.2
- Django REST Framework 3.17
- PostgreSQL (producción) / SQLite (desarrollo)
- Uvicorn (servidor ASGI)
- WhiteNoise (archivos estáticos)
