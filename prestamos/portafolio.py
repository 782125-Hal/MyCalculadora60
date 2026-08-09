"""
Valuación de posiciones de inversión, en Decimal puro.

No consulta ninguna plataforma: CETES y los instrumentos a tasa fija tienen
rendimiento determinista desde el momento de la compra, así que basta con
registrar la operación una vez para proyectar su valor a cualquier fecha.

Convenciones:

- CETES se emiten a descuento sobre un valor nominal de $10 y Banxico los
  valúa con  P = VN / (1 + r · t / 360)  — base 360 días. Despejando, el monto
  invertido P crece hasta VN = P · (1 + r · t / 360). El devengo intermedio a
  `d` días es P · (1 + r · d / 360), que en d = t reproduce exactamente VN.
- Los instrumentos a tasa fija (Briq, pagarés) usan base 365 salvo que se
  indique otra, porque no siguen la convención de mercado de dinero de Banxico.
- Los fondos (BONDDIA, ENERFIN) NO se proyectan: su valor depende del precio
  diario de la acción, que no es derivable de la fecha de compra. Se captura.
"""
from decimal import Decimal, ROUND_HALF_UP
from datetime import date

CENTAVOS = Decimal('0.01')

BASE_MERCADO_DINERO = 360  # convención Banxico para CETES
BASE_ANUAL = 365


def quantize_money(value: Decimal) -> Decimal:
    return Decimal(value).quantize(CENTAVOS, rounding=ROUND_HALF_UP)


def dias_transcurridos(fecha_compra: date, hasta: date, plazo_dias: int) -> int:
    """Días devengados, acotados al plazo: una posición vencida no sigue creciendo."""
    if hasta is None:
        hasta = date.today()
    transcurridos = (hasta - fecha_compra).days
    if transcurridos < 0:
        return 0
    return min(transcurridos, plazo_dias)


def valor_devengado(monto: Decimal, tasa_anual: Decimal, dias: int, base: int = BASE_MERCADO_DINERO) -> Decimal:
    """
    Valor de una posición tras `dias` devengados.

        valor = monto · (1 + r · dias / base)

    Con dias = plazo reproduce el valor al vencimiento. Es la misma expresión
    que usa Banxico para CETES, despejada desde el precio de descuento.
    """
    monto = Decimal(monto)
    r = Decimal(tasa_anual) / Decimal(100)
    factor = Decimal(1) + r * Decimal(dias) / Decimal(base)
    return quantize_money(monto * factor)


def valor_al_vencimiento(monto: Decimal, tasa_anual: Decimal, plazo_dias: int,
                         base: int = BASE_MERCADO_DINERO) -> Decimal:
    return valor_devengado(monto, tasa_anual, plazo_dias, base)


def precio_cete(valor_nominal: Decimal, tasa_anual: Decimal, plazo_dias: int) -> Decimal:
    """
    Precio de descuento de un CETE, fórmula de Banxico:

        P = VN / (1 + r · t / 360)

    Banxico redondea el precio a 7 decimales; aquí se conserva esa precisión
    porque el resultado suele multiplicarse por miles de títulos antes de
    convertirse en pesos.
    """
    vn = Decimal(valor_nominal)
    r = Decimal(tasa_anual) / Decimal(100)
    divisor = Decimal(1) + r * Decimal(plazo_dias) / Decimal(BASE_MERCADO_DINERO)
    return (vn / divisor).quantize(Decimal('0.0000001'), rounding=ROUND_HALF_UP)


def rendimiento_esperado(monto: Decimal, tasa_anual: Decimal, plazo_dias: int,
                         base: int = BASE_MERCADO_DINERO) -> Decimal:
    """Ganancia bruta al vencimiento (antes de ISR)."""
    return quantize_money(valor_al_vencimiento(monto, tasa_anual, plazo_dias, base) - Decimal(monto))


def tasa_efectiva_anual(tasa_anual: Decimal, plazo_dias: int,
                        base: int = BASE_MERCADO_DINERO) -> Decimal:
    """
    Tasa efectiva anualizada suponiendo reinversión al mismo plazo.

    Sirve para comparar plazos distintos entre sí: una tasa nominal de 28 días
    no es directamente comparable con una de 728.
    """
    if plazo_dias <= 0:
        return Decimal('0.00')
    r = Decimal(tasa_anual) / Decimal(100)
    rendimiento_periodo = r * Decimal(plazo_dias) / Decimal(base)
    periodos = Decimal(base) / Decimal(plazo_dias)
    efectiva = (Decimal(1) + rendimiento_periodo) ** int(periodos)
    # El exponente entero subestima cuando base/plazo no es entero; se ajusta
    # con la fracción restante de período.
    fraccion = periodos - int(periodos)
    if fraccion:
        efectiva *= Decimal(1) + rendimiento_periodo * fraccion
    return ((efectiva - Decimal(1)) * Decimal(100)).quantize(CENTAVOS, rounding=ROUND_HALF_UP)
