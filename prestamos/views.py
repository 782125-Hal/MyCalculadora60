from django.shortcuts import render, redirect, get_object_or_404
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.contrib.auth.mixins import LoginRequiredMixin
from django.db import transaction
from django.db.models import Q
from django.http import HttpResponse
from django.utils import timezone
from django.views import View
from django.views.generic import DetailView

from .models import (
    Cliente, Prestamo, Movimiento, registrar_auditoria,
    prestamos_visibles, movimientos_visibles, clientes_visibles,
)
from .forms import (
    CalculatorForm,
    RegistrationForm,
    RegistrarPrestamoForm,
    PagoForm,
    IncrementoForm,
    MovimientoForm,
    PrestamoEditForm,
    CrearPrestamoSimpleForm,
    RegistrarInversionForm,
)
from .calculator import (
    calculate_payment_for_term,
    calculate_term_for_payment,
    build_amortization_schedule,
)

import csv
import logging
from decimal import Decimal, InvalidOperation
from datetime import date, datetime, timedelta
from datetime import datetime as dt  # for PDF generation (dt.now)

logger = logging.getLogger(__name__)


def _csv_safe(value):
    """Neutraliza fórmulas en exports CSV (CSV/formula injection).

    Excel/Sheets ejecutan celdas que empiezan con = + - @ (o tab/CR). Prefijamos
    un apóstrofo para que se traten como texto. Devuelve str siempre."""
    text = '' if value is None else str(value)
    if text and text[0] in ('=', '+', '-', '@', '\t', '\r'):
        return "'" + text
    return text

@login_required
def home(request):
    """Dashboard principal con KPIs y accesos rápidos."""
    from django.db.models import Sum

    hoy = timezone.now().date()

    # KPIs básicos — préstamos visibles (los propios; todos si es admin)
    prestamos = prestamos_visibles(request.user)
    # Recalcular saldos de activos ANTES de agregar, para reflejar pagos/intereses al instante.
    for prestamo in prestamos.filter(activo=True):
        prestamo.actualizar_saldo(hoy)
    total_original = prestamos.aggregate(total=Sum('monto_original'))['total'] or Decimal('0')
    total_saldo = prestamos.aggregate(total=Sum('saldo_actual'))['total'] or Decimal('0')
    activos = prestamos.filter(activo=True).count()
    inactivos = prestamos.filter(activo=False).count()
    total_prestamos = prestamos.count()

    # Préstamos con saldo alto (top 5)
    top_saldos = prestamos.filter(activo=True).order_by('-saldo_actual')[:5]

    # Movimientos recientes (últimos 7 días)
    recientes = movimientos_visibles(request.user).filter(
        fecha__gte=hoy - timedelta(days=7)
    ).select_related('prestamo').order_by('-fecha')[:8]

    # Estimación simple de "próximos" (préstamos con pagos esperados pronto - heurística básica)
    proximos = prestamos.filter(activo=True, saldo_actual__gt=0).order_by('ultimo_pago')[:3]

    context = {
        'total_original': total_original,
        'total_saldo': total_saldo,
        'activos': activos,
        'inactivos': inactivos,
        'total_prestamos': total_prestamos,
        'top_saldos': top_saldos,
        'recientes': recientes,
        'proximos': proximos,
    }
    return render(request, 'prestamos/home.html', context)

@login_required
def lista_prestamos(request):
    """Vista para listar todos los préstamos con actualización diaria del saldo (Punto 5)."""
    q = request.GET.get('q', '').strip()
    prestamos = prestamos_visibles(request.user)
    if q:
        qs_filter = (
            Q(nombre_cliente__icontains=q) |
            Q(telefono__icontains=q)
        )
        try:
            monto_q = Decimal(q.replace(',', '').replace('$', ''))
            qs_filter |= Q(monto_original=monto_q)
        except (InvalidOperation, TypeError, ValueError):
            pass
        prestamos = prestamos.filter(qs_filter)
    hoy = timezone.now().date()
    for prestamo in prestamos:
        prestamo.actualizar_saldo(hoy)  # Actualiza el saldo considerando pagos e intereses
    return render(request, 'prestamos/lista_prestamos.html', {'prestamos': prestamos})

class CalculadoraView(LoginRequiredMixin, View):
    """Vista para la calculadora financiera (Puntos 1-2) y registro de préstamo (Punto 3)."""
    def get(self, request):
        form = CalculatorForm(initial={'monto': Decimal('1000000.00')})
        return render(request, 'prestamos/calculadora_financiera.html', {'form': form})

    def post(self, request):
        form = CalculatorForm(request.POST)
        result = None
        reg_form = None
        calculated_payment = None
        calculated_term = None
        tipo_calculo = None

        if form.is_valid():
            monto = form.cleaned_data['monto']
            tasa = form.cleaned_data['tasa']
            pago_mensual_input = form.cleaned_data['pago_mensual']
            plazo_meses_input = form.cleaned_data['plazo_meses']
            tipo_calculo = form.cleaned_data['tipo_calculo']

            # Use centralized Decimal calculator (supports only mensual in the old CalculatorForm for now)
            if tipo_calculo == 'pago':
                n = plazo_meses_input
                try:
                    calculated_payment = calculate_payment_for_term(monto, tasa, n, tipo_pago='mensual')
                except Exception:
                    calculated_payment = Decimal('0.00')
                calculated_term = n
                result = f'Pago mensual calculado: {calculated_payment}'
            elif tipo_calculo == 'plazo':
                pago = pago_mensual_input
                try:
                    calculated_term = calculate_term_for_payment(monto, tasa, pago, tipo_pago='mensual')
                except ValueError as e:
                    messages.error(request, str(e))
                    return render(request, 'prestamos/calculadora_financiera.html', {'form': form})
                except Exception:
                    calculated_term = 0
                calculated_payment = Decimal(pago).quantize(Decimal('0.01'))
                result = f'Plazo calculado: {calculated_term} meses'

            # Inicializar formulario de registro con datos calculados
            reg_form = RegistrationForm(initial={
                'monto': monto,
                'tasa': tasa,
                'pago_mensual': calculated_payment,
                'plazo_meses': calculated_term if isinstance(calculated_term, int) else None,
                'fecha_inicio': date.today(),
            })

        if 'register' in request.POST:
            reg_form = RegistrationForm(request.POST)
            if reg_form.is_valid():
                try:
                    with transaction.atomic():
                        nombre = reg_form.cleaned_data['nombre']
                        fecha_inicio = reg_form.cleaned_data['fecha_inicio']
                        monto = reg_form.cleaned_data['monto']
                        tasa = reg_form.cleaned_data['tasa']
                        pago_mensual = reg_form.cleaned_data['pago_mensual']
                        plazo_meses = reg_form.cleaned_data['plazo_meses']
                        cliente = Cliente.objects.create(owner=request.user, nombre=nombre, telefono='N/A')
                        prestamo = Prestamo(
                            owner=request.user,
                            cliente=cliente,
                            nombre_cliente=nombre,
                            monto_original=monto,
                            tasa_interes_anual=tasa,
                            pago_mensual=pago_mensual,
                            plazo_meses=plazo_meses,
                            fecha_inicio=fecha_inicio,
                            saldo_actual=monto,
                            tipo_pago='mensual',
                            modo='fixed_term' if tipo_calculo == 'pago' else 'fixed_payment'
                        )
                        prestamo.save()
                        registrar_auditoria(request.user, 'crear', 'Prestamo', prestamo.pk,
                                            f"{nombre} · ${monto}")
                        messages.success(request, 'Préstamo registrado exitosamente.')
                        return redirect('prestamos:detalle_prestamo', pk=prestamo.pk)
                except Exception:
                    logger.exception("Error al registrar préstamo (user=%s)", request.user.pk)
                    messages.error(request, "Ocurrió un error al registrar el préstamo. Intenta de nuevo.")
            else:
                messages.error(request, "Corrija los errores en el formulario de registro.")

        context = {
            'form': form,
            'result': result,
            'reg_form': reg_form,
        }
        return render(request, 'prestamos/calculadora_financiera.html', context)

class RegistrarPrestamoView(LoginRequiredMixin, View):
    """Vista para registrar un préstamo manualmente (Punto 3)."""
    def get(self, request):
        calc_data = request.session.get('calculadora_data', {})
        initial = {
            'monto_original': calc_data.get('monto_original'),
            'tasa_interes_anual': calc_data.get('tasa_interes_anual'),
            'tipo_pago': calc_data.get('tipo_pago', 'mensual'),
            'plazo_meses': calc_data.get('plazo_meses'),
            'pago_mensual': calc_data.get('pago_mensual', Decimal('0')),
            'modo': calc_data.get('modo', 'fixed_term'),
            'fecha_inicio': date.today(),
        }
        form = RegistrarPrestamoForm(initial=initial)
        return render(request, 'prestamos/registrar_prestamo.html', {'form': form})

    def post(self, request):
        form = RegistrarPrestamoForm(request.POST)
        if form.is_valid():
            try:
                with transaction.atomic():
                    nombre = form.cleaned_data['nombre']
                    telefono = form.cleaned_data['telefono']
                    fecha_inicio = form.cleaned_data['fecha_inicio']
                    cliente = Cliente.objects.create(owner=request.user, nombre=nombre, telefono=telefono)
                    prestamo = Prestamo.objects.create(
                        owner=request.user,
                        cliente=cliente,
                        nombre_cliente=nombre,
                        monto_original=form.cleaned_data['monto_original'],
                        tipo_pago=form.cleaned_data['tipo_pago'],
                        fecha_inicio=fecha_inicio,
                        tasa_interes_anual=form.cleaned_data['tasa_interes_anual'],
                        modo=form.cleaned_data['modo'],
                        plazo_meses=form.cleaned_data.get('plazo_meses'),
                        pago_mensual=form.cleaned_data.get('pago_mensual', Decimal('0')),
                        saldo_actual=form.cleaned_data['monto_original']
                    )
                    registrar_auditoria(request.user, 'crear', 'Prestamo', prestamo.pk,
                                        f"{nombre} · ${form.cleaned_data['monto_original']}")
                    messages.success(request, "Préstamo registrado exitosamente.")
                    if 'calculadora_data' in request.session:
                        del request.session['calculadora_data']
                    return redirect('prestamos:detalle_prestamo', pk=prestamo.pk)
            except Exception:
                logger.exception("Error al registrar préstamo manual (user=%s)", request.user.pk)
                messages.error(request, "Ocurrió un error al registrar el préstamo. Intenta de nuevo.")
        else:
            messages.error(request, "Corrija los errores en el formulario.")
        return render(request, 'prestamos/registrar_prestamo.html', {'form': form})

class PrestamoDetailView(LoginRequiredMixin, DetailView):
    """Vista para mostrar detalles del préstamo con amortización y movimientos (Punto 4)."""
    model = Prestamo
    template_name = 'prestamos/detalle_prestamo.html'

    def get_queryset(self):
        # Solo préstamos visibles; ajenos → 404 en vez de exponerse.
        return prestamos_visibles(self.request.user)

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        prestamo = self.object
        hoy = timezone.now().date()
        prestamo.actualizar_saldo(hoy)  # Punto 5: Actualiza saldo diario
        movimientos = prestamo.movimientos.order_by('fecha')
        amortizacion = prestamo.get_amortizacion()
        context.update({
            'amortizacion': amortizacion,
            'movimientos': movimientos,
            'saldo_actual': prestamo.saldo_actual,
            'fecha_actual': hoy,
        })
        return context

@login_required
def registrar_pago(request, prestamo_id):
    """Vista para registrar un pago en un préstamo usando PagoForm."""
    prestamo = get_object_or_404(prestamos_visibles(request.user), id=prestamo_id)

    if request.method == 'POST':
        form = PagoForm(request.POST)
        if form.is_valid():
            try:
                with transaction.atomic():
                    Movimiento.objects.create(
                        prestamo=prestamo,
                        fecha=form.cleaned_data['fecha'],
                        monto=form.cleaned_data['monto'],
                        tipo='pago',
                        descripcion=form.cleaned_data.get('descripcion', 'Pago registrado')
                    )
                    prestamo.actualizar_saldo(timezone.now().date())
                    registrar_auditoria(request.user, 'pago', 'Prestamo', prestamo.pk,
                                        f"${form.cleaned_data['monto']} el {form.cleaned_data['fecha']}")
                    messages.success(request, "Pago registrado exitosamente.")
            except Exception:
                logger.exception("Error al registrar pago (prestamo=%s)", prestamo_id)
                messages.error(request, "Ocurrió un error al registrar el pago. Intenta de nuevo.")
        else:
            for field, errors in form.errors.items():
                for error in errors:
                    messages.error(request, f"{field}: {error}")
        return redirect('prestamos:detalle_prestamo', pk=prestamo_id)

    return redirect('prestamos:detalle_prestamo', pk=prestamo_id)

@login_required
def registrar_incremento(request, prestamo_id):
    """Vista para registrar un incremento de capital usando IncrementoForm."""
    prestamo = get_object_or_404(prestamos_visibles(request.user), id=prestamo_id)

    if request.method == 'POST':
        form = IncrementoForm(request.POST)
        if form.is_valid():
            try:
                with transaction.atomic():
                    prestamo.registrar_incremento(
                        form.cleaned_data['monto'],
                        form.cleaned_data['fecha']
                    )
                    registrar_auditoria(request.user, 'incremento', 'Prestamo', prestamo.pk,
                                        f"${form.cleaned_data['monto']} el {form.cleaned_data['fecha']}")
                    messages.success(request, "Incremento de capital registrado exitosamente.")
            except Exception:
                logger.exception("Error al registrar incremento (prestamo=%s)", prestamo_id)
                messages.error(request, "Ocurrió un error al registrar el incremento. Intenta de nuevo.")
        else:
            for field, errors in form.errors.items():
                for error in errors:
                    messages.error(request, f"{field}: {error}")
        return redirect('prestamos:detalle_prestamo', pk=prestamo_id)

    return redirect('prestamos:detalle_prestamo', pk=prestamo_id)

@login_required
def editar_movimiento(request, movimiento_id):
    """Vista para editar un movimiento existente usando MovimientoForm."""
    movimiento = get_object_or_404(movimientos_visibles(request.user), id=movimiento_id)
    prestamo_id = movimiento.prestamo.id

    if request.method == 'POST':
        form = MovimientoForm(request.POST)
        if form.is_valid():
            try:
                with transaction.atomic():
                    movimiento.monto = form.cleaned_data['monto']
                    movimiento.fecha = form.cleaned_data['fecha']
                    movimiento.descripcion = form.cleaned_data.get('descripcion', movimiento.descripcion)
                    movimiento.save()
                    movimiento.prestamo.actualizar_saldo()
                    registrar_auditoria(request.user, 'editar', 'Movimiento', movimiento.pk,
                                        f"préstamo #{prestamo_id} · ${form.cleaned_data['monto']}")
                    messages.success(request, "Movimiento editado exitosamente.")
                    return redirect('prestamos:detalle_prestamo', pk=prestamo_id)
            except Exception:
                logger.exception("Error al editar movimiento (movimiento=%s)", movimiento_id)
                messages.error(request, "Ocurrió un error al editar el movimiento. Intenta de nuevo.")
        else:
            for field, errors in form.errors.items():
                for error in errors:
                    messages.error(request, f"{field}: {error}")
    else:
        # Pre-llenar el formulario para GET
        form = MovimientoForm(initial={
            'monto': movimiento.monto,
            'fecha': movimiento.fecha,
            'descripcion': movimiento.descripcion,
        })
        # Pasamos el form al template (el template actual usa request.POST directo;
        # por compatibilidad mínima seguimos renderizando, pero el form ya valida)
        # Para no romper el template actual de inmediato, seguimos usando el render original.

    return render(request, 'prestamos/editar_movimiento.html', {'movimiento': movimiento})

@login_required
def borrar_movimiento(request, movimiento_id):
    """Vista para borrar un movimiento (Punto 4)."""
    movimiento = get_object_or_404(movimientos_visibles(request.user), id=movimiento_id)
    prestamo_id = movimiento.prestamo.id
    if request.method == 'POST':
        try:
            with transaction.atomic():
                prestamo = movimiento.prestamo  # Guardar referencia antes del delete
                detalle = f"préstamo #{prestamo_id} · {movimiento.tipo} ${movimiento.monto}"
                movimiento.delete()
                prestamo.actualizar_saldo()  # Recalcula saldo con referencia segura
                registrar_auditoria(request.user, 'borrar', 'Movimiento', movimiento_id, detalle)
                messages.success(request, "Movimiento borrado exitosamente.")
        except Exception:
            logger.exception("Error al borrar movimiento (movimiento=%s)", movimiento_id)
            messages.error(request, "Ocurrió un error al borrar el movimiento. Intenta de nuevo.")
    return redirect('prestamos:detalle_prestamo', pk=prestamo_id)

@login_required
def editar_prestamo(request, prestamo_id):
    """Vista para editar los datos de un préstamo usando PrestamoEditForm."""
    prestamo = get_object_or_404(prestamos_visibles(request.user), id=prestamo_id)

    if request.method == 'POST':
        form = PrestamoEditForm(request.POST)
        if form.is_valid():
            try:
                with transaction.atomic():
                    prestamo.monto_original = form.cleaned_data['monto_original']
                    prestamo.tasa_interes_anual = form.cleaned_data['tasa_interes_anual']
                    prestamo.tipo_pago = form.cleaned_data['tipo_pago']
                    prestamo.saldo_actual = prestamo.monto_original  # Reset
                    prestamo.save()
                    prestamo.actualizar_saldo()
                    registrar_auditoria(request.user, 'editar', 'Prestamo', prestamo.pk,
                                        f"monto ${prestamo.monto_original} · tasa {prestamo.tasa_interes_anual}%")
                    messages.success(request, "Préstamo actualizado exitosamente.")
                    return redirect('prestamos:detalle_prestamo', pk=prestamo_id)
            except Exception:
                logger.exception("Error al actualizar préstamo (prestamo=%s)", prestamo_id)
                messages.error(request, "Ocurrió un error al actualizar el préstamo. Intenta de nuevo.")
        else:
            for field, errors in form.errors.items():
                for error in errors:
                    messages.error(request, f"{field}: {error}")
    # (No inicializamos el form en GET porque el template actual usa el objeto prestamo directamente)

    return render(request, 'prestamos/editar_prestamo.html', {'prestamo': prestamo})

@login_required
def delete_prestamo(request, prestamo_id):
    """Vista para eliminar un préstamo (Punto 4)."""
    prestamo = get_object_or_404(prestamos_visibles(request.user), id=prestamo_id)
    if request.method == 'POST':
        try:
            with transaction.atomic():
                detalle = f"{prestamo.nombre_cliente} · ${prestamo.monto_original}"
                prestamo.delete()  # Deletes loan and related movements due to CASCADE
                registrar_auditoria(request.user, 'borrar', 'Prestamo', prestamo_id, detalle)
                messages.success(request, f"El préstamo #{prestamo_id} ha sido eliminado exitosamente.")
                return redirect('prestamos:lista_prestamos')
        except Exception:
            logger.exception("Error al eliminar préstamo (prestamo=%s)", prestamo_id)
            messages.error(request, "Ocurrió un error al eliminar el préstamo. Intenta de nuevo.")
    return render(request, 'prestamos/confirmar_borrado.html', {
        'prestamo': prestamo,
        'titulo': 'Confirmar Eliminación',
        'mensaje_confirmacion': '¿Está seguro de eliminar este préstamo?'
    })

@login_required
def crear_prestamo(request):
    """Vista para crear un préstamo desde un formulario simple."""
    if request.method == 'POST':
        form = CrearPrestamoSimpleForm(request.POST)
        # Clientes seleccionables: los propios (todos si es admin).
        form.fields['cliente'].queryset = clientes_visibles(request.user)
        if form.is_valid():
            try:
                cliente = form.cleaned_data['cliente']
                monto = form.cleaned_data['monto']
                with transaction.atomic():
                    prestamo = Prestamo.objects.create(
                        owner=request.user,
                        cliente=cliente,
                        nombre_cliente=cliente.nombre,
                        monto_original=monto,
                        tipo_pago=form.cleaned_data['tipo_pago'],
                        fecha_inicio=form.cleaned_data['fecha_inicio'],
                        tasa_interes_anual=form.cleaned_data['tasa_interes_anual'],
                        saldo_actual=monto,
                        plazo_meses=form.cleaned_data['periodos_totales'],
                    )
                    registrar_auditoria(request.user, 'crear', 'Prestamo', prestamo.pk,
                                        f"{cliente.nombre} · ${monto}")
                    messages.success(request, "Préstamo creado exitosamente.")
                    return redirect('prestamos:detalle_prestamo', pk=prestamo.pk)
            except Exception:
                logger.exception("Error al crear préstamo (user=%s)", request.user.pk)
                messages.error(request, "Ocurrió un error al crear el préstamo. Intenta de nuevo.")
        else:
            for field, errors in form.errors.items():
                for error in errors:
                    messages.error(request, f"{field}: {error}")
    clientes = clientes_visibles(request.user)
    return render(request, 'prestamos/crear_prestamo.html', {'clientes': clientes})

@login_required
def inversiones(request):
    """Vista para la calculadora de inversiones."""
    return render(request, 'prestamos/inversiones.html', {'title': 'Calculadora de Inversiones'})

@login_required
def registrar_inversion(request):
    """Vista para registrar una inversión como préstamo + sus movimientos simulados."""
    if request.method == 'POST':
        form = RegistrarInversionForm(request.POST)
        if not form.is_valid():
            for field, errors in form.errors.items():
                for error in errors:
                    messages.error(request, f"{field}: {error}")
            return render(request, 'prestamos/inversiones.html')

        # Tipos de movimiento permitidos desde el frontend (evita valores arbitrarios).
        TIPOS_MOV_VALIDOS = {'pago', 'incremento_capital'}

        try:
            inversion_inicial = form.cleaned_data['inversionInicial']
            tasa_descuento = form.cleaned_data['tasaDescuento']
            anos = form.cleaned_data['anos']
            fecha_base = form.cleaned_data.get('fecha_inicio_simulacion') or timezone.now().date()

            with transaction.atomic():
                cliente = Cliente.objects.create(owner=request.user, nombre="Inversión Automática", telefono="N/A")
                prestamo = Prestamo.objects.create(
                    owner=request.user,
                    cliente=cliente,
                    nombre_cliente="Inversión Automática",
                    monto_original=inversion_inicial,
                    tipo_pago="mensual",
                    fecha_inicio=fecha_base,
                    tasa_interes_anual=tasa_descuento,
                    saldo_actual=inversion_inicial,
                    plazo_meses=anos * 12
                )

                # Crear movimientos a partir de la simulación enviada por el frontend
                # Esperamos campos: movimiento_fecha_0, movimiento_monto_0, movimiento_tipo_0, ...
                idx = 0
                while True:
                    fecha_str = request.POST.get(f'movimiento_fecha_{idx}')
                    monto_str = request.POST.get(f'movimiento_monto_{idx}')
                    tipo = request.POST.get(f'movimiento_tipo_{idx}')

                    if not fecha_str or not monto_str or not tipo:
                        break

                    try:
                        mov_fecha = datetime.strptime(fecha_str, '%Y-%m-%d').date()
                        mov_monto = Decimal(monto_str)
                        if mov_monto > 0 and tipo in TIPOS_MOV_VALIDOS:
                            Movimiento.objects.create(
                                prestamo=prestamo,
                                fecha=mov_fecha,
                                monto=mov_monto,
                                tipo=tipo,  # 'incremento_capital' para ingresos, 'pago' para retiros
                                descripcion=f"Simulado - Año {idx+1}"
                            )
                    except (ValueError, TypeError, InvalidOperation):
                        pass
                    idx += 1

                # Si no se enviaron movimientos detallados, al menos crear el inicial
                if idx == 0:
                    Movimiento.objects.create(
                        prestamo=prestamo,
                        fecha=fecha_base,
                        monto=inversion_inicial,
                        tipo='incremento_capital',
                        descripcion='Inversión inicial'
                    )

                registrar_auditoria(request.user, 'crear', 'Prestamo', prestamo.pk,
                                    f"Inversión · ${inversion_inicial}")
                messages.success(request, "Inversión registrada exitosamente con sus movimientos.")
                return redirect('prestamos:detalle_prestamo', pk=prestamo.pk)

        except Exception:
            logger.exception("Error al registrar inversión (user=%s)", request.user.pk)
            messages.error(request, "Ocurrió un error al registrar la inversión. Intenta de nuevo.")

    return render(request, 'prestamos/inversiones.html')

# Cálculos centralizados en prestamos/calculator.py
# (calculate_payment_for_term, calculate_term_for_payment, build_amortization_schedule, etc.)
# Las funciones auxiliares antiguas con float() fueron removidas.


# ============================================================
# Exportaciones CSV (Fase 3)
# ============================================================

@login_required
def export_prestamos_csv(request):
    """Exporta todos los préstamos a CSV."""
    response = HttpResponse(content_type='text/csv; charset=utf-8')
    response['Content-Disposition'] = 'attachment; filename="prestamos.csv"'

    writer = csv.writer(response)
    writer.writerow([
        'ID', 'Cliente', 'Monto Original', 'Saldo Actual', 'Tasa %',
        'Tipo Pago', 'Modo', 'Fecha Inicio', 'Activo', 'Ultimo Pago'
    ])

    for p in prestamos_visibles(request.user).order_by('-fecha_inicio'):
        writer.writerow([
            p.id,
            _csv_safe(p.nombre_cliente),
            p.monto_original,
            p.saldo_actual,
            p.tasa_interes_anual,
            p.tipo_pago,
            p.modo,
            p.fecha_inicio,
            'Sí' if p.activo else 'No',
            p.ultimo_pago or '',
        ])
    return response


@login_required
def export_prestamo_csv(request, pk):
    """Exporta movimientos + tabla de amortización de un préstamo específico."""
    prestamo = get_object_or_404(prestamos_visibles(request.user), pk=pk)
    response = HttpResponse(content_type='text/csv; charset=utf-8')
    response['Content-Disposition'] = f'attachment; filename="prestamo_{pk}.csv"'

    writer = csv.writer(response)

    # Encabezado del préstamo
    writer.writerow(['PRESTAMO', _csv_safe(prestamo.nombre_cliente), 'ID', prestamo.id])
    writer.writerow(['Monto Original', prestamo.monto_original, 'Saldo Actual', prestamo.saldo_actual])
    writer.writerow(['Tasa Anual %', prestamo.tasa_interes_anual, 'Tipo', prestamo.tipo_pago])
    writer.writerow([])

    # Movimientos
    writer.writerow(['MOVIMIENTOS'])
    writer.writerow(['Fecha', 'Tipo', 'Monto', 'Descripción'])
    for m in prestamo.movimientos.order_by('fecha'):
        writer.writerow([m.fecha, m.tipo, m.monto, _csv_safe(m.descripcion)])

    writer.writerow([])

    # Amortización
    writer.writerow(['TABLA DE AMORTIZACIÓN (proyectada)'])
    writer.writerow(['Periodo', 'Fecha', 'Pago', 'Interés', 'Capital', 'Saldo'])
    for fila in prestamo.get_amortizacion():
        writer.writerow([
            fila['periodo'],
            fila['fecha'],
            fila['pago'],
            fila['interes'],
            fila['capital'],
            fila['saldo'],
        ])

    return response


# ============================================================
# Reportes PDF (Fase 3) - requiere reportlab
# ============================================================

@login_required
def export_prestamo_pdf(request, pk):
    """Genera un PDF simple de Estado de Cuenta / Amortización usando reportlab."""
    from reportlab.lib.pagesizes import letter
    from reportlab.lib import colors
    from reportlab.lib.styles import getSampleStyleSheet
    from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle
    from reportlab.lib.units import inch
    from io import BytesIO

    prestamo = get_object_or_404(prestamos_visibles(request.user), pk=pk)

    buffer = BytesIO()
    doc = SimpleDocTemplate(buffer, pagesize=letter,
                            rightMargin=0.5*inch, leftMargin=0.5*inch,
                            topMargin=0.5*inch, bottomMargin=0.5*inch)

    styles = getSampleStyleSheet()
    elements = []

    # Título
    elements.append(Paragraph(f"Estado de Cuenta - Préstamo #{prestamo.id}", styles['Title']))
    elements.append(Paragraph(f"Cliente: {prestamo.nombre_cliente}", styles['Normal']))
    elements.append(Spacer(1, 12))

    # Datos básicos
    data = [
        ['Monto Original', f'${prestamo.monto_original:,.2f}'],
        ['Saldo Actual', f'${prestamo.saldo_actual:,.2f}'],
        ['Tasa Anual', f'{prestamo.tasa_interes_anual}%'],
        ['Frecuencia', prestamo.tipo_pago.title()],
        ['Modo', prestamo.modo],
        ['Fecha Inicio', str(prestamo.fecha_inicio)],
        ['Estado', 'Activo' if prestamo.activo else 'Pagado/Cancelado'],
    ]
    t = Table(data, colWidths=[2.5*inch, 3*inch])
    t.setStyle(TableStyle([
        ('BACKGROUND', (0, 0), (0, -1), colors.lightgrey),
        ('TEXTCOLOR', (0, 0), (-1, -1), colors.black),
        ('ALIGN', (0, 0), (-1, -1), 'LEFT'),
        ('FONTNAME', (0, 0), (-1, 0), 'Helvetica-Bold'),
        ('FONTSIZE', (0, 0), (-1, -1), 10),
        ('BOTTOMPADDING', (0, 0), (-1, -1), 6),
        ('GRID', (0, 0), (-1, -1), 0.5, colors.grey),
    ]))
    elements.append(t)
    elements.append(Spacer(1, 20))

    # Movimientos recientes
    elements.append(Paragraph("<b>Movimientos (últimos)</b>", styles['Heading3']))
    mov_data = [['Fecha', 'Tipo', 'Monto', 'Descripción']]
    for m in prestamo.movimientos.order_by('-fecha')[:10]:
        mov_data.append([
            str(m.fecha),
            m.get_tipo_display(),
            f'${m.monto:,.2f}',
            (m.descripcion or '')[:40]
        ])
    if len(mov_data) == 1:
        mov_data.append(['-', '-', '-', 'Sin movimientos'])
    t2 = Table(mov_data, colWidths=[1.2*inch, 1.5*inch, 1.2*inch, 2.5*inch])
    t2.setStyle(TableStyle([
        ('BACKGROUND', (0, 0), (-1, 0), colors.Color(0.2, 0.3, 0.5)),
        ('TEXTCOLOR', (0, 0), (-1, 0), colors.whitesmoke),
        ('FONTNAME', (0, 0), (-1, 0), 'Helvetica-Bold'),
        ('FONTSIZE', (0, 0), (-1, -1), 9),
        ('GRID', (0, 0), (-1, -1), 0.5, colors.grey),
        ('ALIGN', (2, 0), (2, -1), 'RIGHT'),
    ]))
    elements.append(t2)
    elements.append(Spacer(1, 20))

    # Tabla de amortización
    elements.append(Paragraph("<b>Tabla de Amortización (Proyectada)</b>", styles['Heading3']))
    amort = prestamo.get_amortizacion()
    amort_data = [['Periodo', 'Fecha', 'Pago', 'Interés', 'Capital', 'Saldo']]
    for fila in amort[:25]:  # Limitar filas
        amort_data.append([
            fila['periodo'],
            str(fila['fecha']),
            f"${fila['pago']:,.2f}",
            f"${fila['interes']:,.2f}",
            f"${fila['capital']:,.2f}",
            f"${fila['saldo']:,.2f}",
        ])
    if len(amort) > 25:
        amort_data.append(['...', '...', '...', '...', '...', '...'])
    t3 = Table(amort_data, colWidths=[0.7*inch, 1.1*inch, 1.1*inch, 1.1*inch, 1.1*inch, 1.1*inch])
    t3.setStyle(TableStyle([
        ('BACKGROUND', (0, 0), (-1, 0), colors.Color(0.2, 0.3, 0.5)),
        ('TEXTCOLOR', (0, 0), (-1, 0), colors.whitesmoke),
        ('FONTNAME', (0, 0), (-1, 0), 'Helvetica-Bold'),
        ('FONTSIZE', (0, 0), (-1, -1), 8),
        ('GRID', (0, 0), (-1, -1), 0.5, colors.grey),
        ('ALIGN', (0, 0), (-1, -1), 'CENTER'),
        ('ALIGN', (2, 1), (-1, -1), 'RIGHT'),
    ]))
    elements.append(t3)

    elements.append(Spacer(1, 30))
    elements.append(Paragraph(f"Generado el {dt.now().strftime('%Y-%m-%d %H:%M')}", styles['Normal']))

    doc.build(elements)
    buffer.seek(0)

    response = HttpResponse(buffer, content_type='application/pdf')
    response['Content-Disposition'] = f'attachment; filename="estado_cuenta_prestamo_{pk}.pdf"'
    return response