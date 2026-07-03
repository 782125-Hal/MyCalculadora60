"""
prestamos/calculator.py

Centralized, pure financial calculation functions using Decimal for precision.
All loan amortization, payment and term calculations live here.

Used by:
- Prestamo.get_amortizacion (model)
- CalculadoraView and related forms
- PrestamoViewSet.calcular (API)
"""

from decimal import Decimal, ROUND_HALF_UP
from dateutil.relativedelta import relativedelta
from datetime import date
from typing import Literal, Optional, List, Dict, Any

PeriodType = Literal['mensual', 'semanal']
LoanMode = Literal['fixed_term', 'fixed_payment']


def get_period_rate_and_delta(tasa_anual: Decimal, tipo_pago: str) -> tuple[relativedelta, Decimal]:
    """Return (time delta per period, interest rate per period).
    Shared helper used by amortization schedule and saldo updater.
    """
    tasa = Decimal(str(tasa_anual)) / Decimal('100')
    if tipo_pago == 'semanal':
        return relativedelta(weeks=1), tasa / Decimal('52')
    # default mensual
    return relativedelta(months=1), tasa / Decimal('12')


def _get_delta_and_period_rate(tasa_anual: Decimal, tipo_pago: str) -> tuple[relativedelta, Decimal]:
    """Backward-compatible alias."""
    return get_period_rate_and_delta(tasa_anual, tipo_pago)


def quantize_money(value: Decimal) -> Decimal:
    """Round to 2 decimal places using banker's rounding (common for money)."""
    return value.quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)


def calculate_payment_for_term(
    monto: Decimal,
    tasa_anual: Decimal,
    plazo: int,
    tipo_pago: str = 'mensual'
) -> Decimal:
    """
    Calcula el pago periódico fijo necesario para liquidar el préstamo en 'plazo' periodos
    (sistema francés / cuota constante). Equivale al modo 'fixed_term'.
    """
    if plazo <= 0:
        return Decimal('0.00')
    balance = Decimal(str(monto))
    _, tasa_periodo = _get_delta_and_period_rate(tasa_anual, tipo_pago)

    if tasa_periodo == 0:
        pago = balance / Decimal(plazo)
    else:
        tmp = (Decimal(1) + tasa_periodo) ** plazo
        pago = balance * tasa_periodo * tmp / (tmp - Decimal(1))

    return quantize_money(pago)


def calculate_term_for_payment(
    monto: Decimal,
    tasa_anual: Decimal,
    pago_deseado: Decimal,
    tipo_pago: str = 'mensual'
) -> int:
    """
    Calcula el número de periodos necesarios para liquidar el préstamo
    con un pago periódico fijo dado. Equivale al modo 'fixed_payment'.
    Devuelve el plazo redondeado hacia arriba.
    """
    balance = Decimal(str(monto))
    pago = Decimal(str(pago_deseado))
    _, tasa_periodo = _get_delta_and_period_rate(tasa_anual, tipo_pago)

    if tasa_periodo == 0:
        if pago <= 0:
            return 0
        return int((balance / pago).to_integral_value(rounding=ROUND_HALF_UP))

    interes_periodo = balance * tasa_periodo
    if pago <= interes_periodo:
        # Pago insuficiente para cubrir intereses → no se liquida nunca
        raise ValueError("El pago periódico es insuficiente para cubrir los intereses.")

    # Term estimation uses float + math.log (result is an integer number of periods;
    # the payment amounts themselves remain fully Decimal-precise in the schedule).
    import math
    n_float = math.log(float(pago) / float(pago - interes_periodo)) / math.log(1 + float(tasa_periodo))
    return math.ceil(n_float)


def build_amortization_schedule(
    monto: Decimal,
    tasa_anual: Decimal,
    modo: str,
    tipo_pago: str = 'mensual',
    plazo: Optional[int] = None,
    pago_fijo: Optional[Decimal] = None,
    fecha_inicio: Optional[date] = None,
) -> List[Dict[str, Any]]:
    """
    Genera la tabla de amortización completa.

    Devuelve lista de dicts con las claves esperadas por templates y serializers:
        periodo, fecha, pago, interes, capital, saldo

    Los valores numéricos se devuelven como float (para compatibilidad con el código existente).
    Todo el cálculo interno usa Decimal.
    """
    if fecha_inicio is None:
        fecha_inicio = date.today()

    balance = Decimal(str(monto))
    delta, tasa_periodo = _get_delta_and_period_rate(tasa_anual, tipo_pago)
    fecha = fecha_inicio + delta
    amortizacion: List[Dict[str, Any]] = []
    periodo = 1

    if modo == 'fixed_term':
        if plazo is None or plazo <= 0:
            return []
        # Calcular pago teórico (cuota)
        if tasa_periodo == 0:
            pago = balance / Decimal(plazo)
        else:
            tmp = (Decimal(1) + tasa_periodo) ** plazo
            pago = balance * tasa_periodo * tmp / (tmp - Decimal(1))
        pago = quantize_money(pago)

        max_iter = plazo + 5  # safety
        while periodo <= plazo and balance > 0 and periodo < max_iter:
            intereses = quantize_money(balance * tasa_periodo)
            capital = pago - intereses
            current_pago = pago
            if capital > balance:
                capital = balance
                current_pago = quantize_money(intereses + capital)
            balance = quantize_money(balance - capital)

            amortizacion.append({
                'periodo': periodo,
                'fecha': fecha,
                'pago': float(current_pago),
                'interes': float(intereses),
                'capital': float(capital),
                'saldo': float(balance),
            })
            fecha += delta
            periodo += 1

    elif modo == 'fixed_payment':
        if pago_fijo is None or pago_fijo <= 0:
            return []
        pago_fixed = quantize_money(Decimal(str(pago_fijo)))
        max_periodos = 10000  # original safety limit

        while balance > 0 and periodo <= max_periodos:
            intereses = quantize_money(balance * tasa_periodo)
            capital = pago_fixed - intereses
            current_pago = pago_fixed
            if capital >= 0:
                if capital > balance:
                    capital = balance
                    current_pago = quantize_money(intereses + capital)
            # if capital < 0, balance will grow (intereses > pago)
            balance = quantize_money(max(balance - capital, Decimal('0.00')))

            amortizacion.append({
                'periodo': periodo,
                'fecha': fecha,
                'pago': float(current_pago),
                'interes': float(intereses),
                'capital': float(capital if capital > 0 else Decimal('0.00')),
                'saldo': float(balance),
            })
            if balance <= 0:
                break
            fecha += delta
            periodo += 1

    return amortizacion


# Convenience re-exports used by views / API for the "what-if" calculator
def calculate_loan_payment(monto: Decimal, tasa: Decimal, plazo: int, tipo_pago: str = 'mensual') -> Decimal:
    """Wrapper for the calculator form 'pago' mode."""
    return calculate_payment_for_term(monto, tasa, plazo, tipo_pago)


def calculate_loan_term(monto: Decimal, tasa: Decimal, pago: Decimal, tipo_pago: str = 'mensual') -> int:
    """Wrapper for the calculator form 'plazo' mode."""
    return calculate_term_for_payment(monto, tasa, pago, tipo_pago)
