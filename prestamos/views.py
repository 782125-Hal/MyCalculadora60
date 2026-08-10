from django.shortcuts import render, redirect, get_object_or_404
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.contrib.auth.mixins import LoginRequiredMixin
from django.core.paginator import Paginator
from django.db import transaction
from django.db.models import Q, Sum
from django.http import HttpResponse
from django.utils import timezone
from django.views import View
from django.views.generic import DetailView

from .models import (
    Cliente, Prestamo, Movimiento, registrar_auditoria,
    prestamos_visibles, movimientos_visibles, clientes_visibles,
    Inversion, MovimientoInversion, inversiones_visibles,
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
    InversionForm,
    MovimientoInversionForm,
)
from .importador import IMPORTACIONES, leer_tabla, procesar
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
    hoy = timezone.now().date()

    # KPIs básicos — registros visibles (los propios; todos si es admin)
    visibles = prestamos_visibles(request.user)
    # Recalcular saldos de activos ANTES de agregar, para reflejar pagos/intereses al instante.
    for prestamo in visibles.filter(activo=True):
        prestamo.actualizar_saldo(hoy)

    # Lo que me deben y lo que debo son magnitudes opuestas: sumarlas en un
    # mismo total no significaría nada, así que van por separado.
    prestamos = visibles.filter(rol=Prestamo.ROL_PRESTAMO)
    deudas = visibles.filter(rol=Prestamo.ROL_DEUDA)

    total_original = prestamos.aggregate(total=Sum('monto_original'))['total'] or Decimal('0')
    total_saldo = prestamos.aggregate(total=Sum('saldo_actual'))['total'] or Decimal('0')
    activos = prestamos.filter(activo=True).count()
    inactivos = prestamos.filter(activo=False).count()
    total_prestamos = prestamos.count()

    total_deuda_original = deudas.aggregate(total=Sum('monto_original'))['total'] or Decimal('0')
    total_deuda_saldo = deudas.aggregate(total=Sum('saldo_actual'))['total'] or Decimal('0')
    deudas_activas = deudas.filter(activo=True).count()
    total_deudas = deudas.count()

    # Préstamos y deudas con saldo alto (top 5 de cada uno)
    top_saldos = prestamos.filter(activo=True).order_by('-saldo_actual')[:5]
    top_deudas = deudas.filter(activo=True).order_by('-saldo_actual')[:5]

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
        'total_deuda_original': total_deuda_original,
        'total_deuda_saldo': total_deuda_saldo,
        'deudas_activas': deudas_activas,
        'total_deudas': total_deudas,
        'top_deudas': top_deudas,
        'recientes': recientes,
        'proximos': proximos,
    }
    return render(request, 'prestamos/home.html', context)

PRESTAMOS_POR_PAGINA = 20

ORDEN_CHOICES = {
    'fecha_desc': '-fecha_inicio',
    'fecha_asc': 'fecha_inicio',
    'nombre': 'nombre_cliente',
    'monto_desc': '-monto_original',
    'saldo_desc': '-saldo_actual',
}


@login_required
def lista_prestamos(request):
    """Lista con búsqueda, filtros por tipo y estado, orden y paginación.

    El saldo se recalcula sólo para la página visible. actualizar_saldo() purga
    y regenera los cargos de interés de cada préstamo, así que recorrer el
    queryset completo significaba cientos de escrituras por cada GET.
    """
    q = request.GET.get('q', '').strip()
    rol = request.GET.get('rol', 'todos')
    estado = request.GET.get('estado', 'todos')
    orden = request.GET.get('orden', 'fecha_desc')

    prestamos = prestamos_visibles(request.user)
    if rol in (Prestamo.ROL_PRESTAMO, Prestamo.ROL_DEUDA):
        prestamos = prestamos.filter(rol=rol)
    if estado == 'activos':
        prestamos = prestamos.filter(activo=True)
    elif estado == 'inactivos':
        prestamos = prestamos.filter(activo=False)
    if q:
        qs_filter = (
            Q(nombre_cliente__icontains=q) |
            Q(telefono__icontains=q) |
            Q(concepto__icontains=q)
        )
        try:
            monto_q = Decimal(q.replace(',', '').replace('$', ''))
            qs_filter |= Q(monto_original=monto_q)
        except (InvalidOperation, TypeError, ValueError):
            pass
        prestamos = prestamos.filter(qs_filter)

    prestamos = prestamos.order_by(ORDEN_CHOICES.get(orden, ORDEN_CHOICES['fecha_desc']))

    paginator = Paginator(prestamos, PRESTAMOS_POR_PAGINA)
    page = paginator.get_page(request.GET.get('page'))

    hoy = timezone.now().date()
    suma_saldos = Decimal('0.00')
    for prestamo in page.object_list:
        prestamo.actualizar_saldo(hoy)
        suma_saldos += prestamo.saldo_actual

    titulos = {
        Prestamo.ROL_DEUDA: 'Mis Deudas',
        Prestamo.ROL_PRESTAMO: 'Préstamos Otorgados',
    }
    return render(request, 'prestamos/lista_prestamos.html', {
        'page': page,
        'prestamos': page.object_list,
        'q': q,
        'rol': rol,
        'estado': estado,
        'orden': orden,
        'total': paginator.count,
        'suma_saldos': suma_saldos,
        'titulo': titulos.get(rol, 'Préstamos y Deudas'),
        'es_vista_deudas': rol == Prestamo.ROL_DEUDA,
    })

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

            # Dejar el cálculo en sesión para que "Registrar" llegue prellenado.
            # RegistrarPrestamoView lee esta misma clave; sin este guardado el
            # prellenado nunca ocurría y el plazo siempre llegaba vacío.
            request.session['calculadora_data'] = {
                'monto_original': str(monto),
                'tasa_interes_anual': str(tasa),
                'tipo_pago': 'mensual',
                'plazo_meses': calculated_term if isinstance(calculated_term, int) else None,
                'pago_mensual': str(calculated_payment),
                'modo': 'fixed_term' if tipo_calculo == 'pago' else 'fixed_payment',
            }

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
            'rol': request.GET.get('rol') or Prestamo.ROL_PRESTAMO,
            'monto_original': calc_data.get('monto_original'),
            'tasa_interes_anual': calc_data.get('tasa_interes_anual'),
            'tipo_pago': calc_data.get('tipo_pago', 'mensual'),
            'plazo_meses': calc_data.get('plazo_meses'),
            # Sin default 0: un 0 prellenado parece campo lleno pero no pasa la
            # validación de "Pago Fijo", que era lo que bloqueaba el alta.
            'pago_mensual': calc_data.get('pago_mensual'),
            'modo': calc_data.get('modo', 'fixed_term'),
            'fecha_inicio': date.today(),
        }
        form = RegistrarPrestamoForm(initial=initial)
        return render(request, 'prestamos/registrar_prestamo.html', {
            'form': form,
            'viene_de_calculadora': bool(calc_data),
        })

    def post(self, request):
        form = RegistrarPrestamoForm(request.POST)
        if form.is_valid():
            try:
                with transaction.atomic():
                    nombre = form.cleaned_data['nombre']
                    telefono = form.cleaned_data['telefono']
                    fecha_inicio = form.cleaned_data['fecha_inicio']
                    modo = form.cleaned_data['modo']
                    monto = form.cleaned_data['monto_original']
                    tasa = form.cleaned_data['tasa_interes_anual']
                    tipo_pago = form.cleaned_data['tipo_pago']
                    plazo = form.cleaned_data.get('plazo_meses')
                    pago = form.cleaned_data.get('pago_mensual')

                    # Completar el dato que el modo elegido no pide. Además de
                    # mostrar cuota y plazo en el detalle, pago_mensual es la base
                    # del cargo por período en actualizar_saldo(): si queda en None
                    # el préstamo nunca genera intereses.
                    if modo == 'fixed_term' and not pago:
                        pago = calculate_payment_for_term(monto, tasa, plazo, tipo_pago)
                    elif modo == 'fixed_payment' and not plazo:
                        try:
                            plazo = calculate_term_for_payment(monto, tasa, pago, tipo_pago)
                        except ValueError:
                            # El pago no cubre ni los intereses: no se liquida
                            # nunca. Se registra igual, sin plazo.
                            plazo = None

                    cliente = Cliente.objects.create(owner=request.user, nombre=nombre, telefono=telefono)
                    prestamo = Prestamo.objects.create(
                        owner=request.user,
                        rol=form.cleaned_data['rol'],
                        concepto=form.cleaned_data['concepto'],
                        cliente=cliente,
                        nombre_cliente=nombre,
                        telefono=telefono,
                        monto_original=monto,
                        tipo_pago=tipo_pago,
                        fecha_inicio=fecha_inicio,
                        tasa_interes_anual=tasa,
                        modo=modo,
                        plazo_meses=plazo,
                        pago_mensual=pago,
                        saldo_actual=monto,
                    )
                    registrar_auditoria(request.user, 'crear', 'Prestamo', prestamo.pk,
                                        f"{prestamo.get_rol_display()} · {nombre} · ${monto}")
                    messages.success(
                        request,
                        "Deuda registrada exitosamente." if prestamo.es_deuda
                        else "Préstamo registrado exitosamente.",
                    )
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
        total_pagado = (
            prestamo.movimientos.filter(tipo='pago')
            .aggregate(total=Sum('monto'))['total'] or Decimal('0.00')
        )
        context.update({
            'amortizacion': amortizacion,
            'movimientos': movimientos,
            'saldo_actual': prestamo.saldo_actual,
            'total_pagado': total_pagado,
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
                        form.cleaned_data['fecha'],
                        form.cleaned_data.get('descripcion'),
                    )
                    registrar_auditoria(request.user, 'incremento', 'Prestamo', prestamo.pk,
                                        f"${form.cleaned_data['monto']} el {form.cleaned_data['fecha']}")
                    messages.success(
                        request,
                        "Cargo registrado exitosamente." if prestamo.es_deuda
                        else "Incremento de capital registrado exitosamente.",
                    )
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

# ============================================
# Portafolio de inversiones
#
# Ninguna de estas vistas consulta a CetesDirecto ni a Briq: no exponen API y
# automatizar su login sería frágil y contrario a sus términos. Las posiciones
# se capturan una vez y su valor se proyecta con prestamos/portafolio.py.
# ============================================

@login_required
def portafolio(request):
    """Dashboard consolidado del portafolio."""
    hoy = timezone.now().date()
    posiciones = list(inversiones_visibles(request.user).filter(activa=True))

    total_invertido = Decimal('0.00')
    total_valor = Decimal('0.00')
    por_plataforma = {}

    for posicion in posiciones:
        valor = posicion.valor_estimado(hoy)
        posicion.valor_hoy = valor
        posicion.rendimiento_hoy = posicion.rendimiento(hoy)
        posicion.dias_restantes = posicion.dias_para_vencer(hoy)

        posicion.capital = posicion.capital_invertido
        total_invertido += posicion.capital
        total_valor += valor

        resumen = por_plataforma.setdefault(posicion.get_plataforma_display(), {
            'invertido': Decimal('0.00'), 'valor': Decimal('0.00'), 'posiciones': 0,
        })
        resumen['invertido'] += posicion.capital
        resumen['valor'] += valor
        resumen['posiciones'] += 1

    for resumen in por_plataforma.values():
        resumen['rendimiento'] = resumen['valor'] - resumen['invertido']

    rendimiento_total = total_valor - total_invertido
    rendimiento_pct = (
        (rendimiento_total / total_invertido * Decimal('100')).quantize(Decimal('0.01'))
        if total_invertido else Decimal('0.00')
    )

    # Vencimientos próximos: sólo instrumentos a plazo, los fondos no vencen.
    proximos = sorted(
        (p for p in posiciones if p.fecha_vencimiento and not p.vencida),
        key=lambda p: p.fecha_vencimiento,
    )[:5]
    vencidas = [p for p in posiciones if p.vencida]

    return render(request, 'prestamos/portafolio.html', {
        'posiciones': sorted(posiciones, key=lambda p: p.valor_hoy, reverse=True),
        'total_invertido': total_invertido,
        'total_valor': total_valor,
        'rendimiento_total': rendimiento_total,
        'rendimiento_pct': rendimiento_pct,
        'por_plataforma': por_plataforma,
        'proximos': proximos,
        'vencidas': vencidas,
        'fecha_actual': hoy,
    })


@login_required
def nueva_inversion(request):
    if request.method != 'POST':
        return render(request, 'prestamos/inversion_form.html', {
            'form': InversionForm(), 'titulo': 'Registrar Inversión',
        })

    form = InversionForm(request.POST)
    if not form.is_valid():
        messages.error(request, "Corrija los errores marcados abajo.")
        return render(request, 'prestamos/inversion_form.html', {
            'form': form, 'titulo': 'Registrar Inversión',
        })

    try:
        with transaction.atomic():
            inversion = Inversion.objects.create(
                owner=request.user,
                plataforma=form.cleaned_data['plataforma'],
                nombre=form.cleaned_data['nombre'],
                tipo=form.cleaned_data['tipo'],
                monto_invertido=form.cleaned_data['monto_invertido'],
                fecha_compra=form.cleaned_data['fecha_compra'],
                tasa_anual=form.cleaned_data.get('tasa_anual') or Decimal('0'),
                plazo_dias=form.cleaned_data.get('plazo_dias') or 0,
                base_dias=int(form.cleaned_data['base_dias']),
                valor_manual=form.cleaned_data.get('valor_manual'),
                fecha_valor=form.cleaned_data.get('fecha_valor'),
                notas=form.cleaned_data.get('notas', ''),
            )
            registrar_auditoria(request.user, 'crear', 'Inversion', inversion.pk,
                                f"{inversion.nombre} · ${inversion.monto_invertido}")
        messages.success(request, "Inversión registrada exitosamente.")
        return redirect('prestamos:detalle_inversion', pk=inversion.pk)
    except Exception:
        logger.exception("Error al registrar inversión (user=%s)", request.user.pk)
        messages.error(request, "Ocurrió un error al registrar la inversión.")
        return render(request, 'prestamos/inversion_form.html', {
            'form': form, 'titulo': 'Registrar Inversión',
        })


@login_required
def detalle_inversion(request, pk):
    inversion = get_object_or_404(inversiones_visibles(request.user), pk=pk)
    hoy = timezone.now().date()
    return render(request, 'prestamos/detalle_inversion.html', {
        'inversion': inversion,
        'capital_invertido': inversion.capital_invertido,
        'aportaciones': inversion.aportaciones,
        'retiros': inversion.retiros,
        'rendimientos_cobrados': inversion.rendimientos_cobrados,
        'valor_hoy': inversion.valor_estimado(hoy),
        'valor_vencimiento': inversion.valor_al_vencimiento(),
        'rendimiento_hoy': inversion.rendimiento(hoy),
        'dias_devengados': inversion.dias_devengados(hoy),
        'dias_restantes': inversion.dias_para_vencer(hoy),
        'movimientos': inversion.movimientos.all(),
        'fecha_actual': hoy,
    })


@login_required
def registrar_movimiento_inversion(request, pk):
    inversion = get_object_or_404(inversiones_visibles(request.user), pk=pk)
    if request.method != 'POST':
        return redirect('prestamos:detalle_inversion', pk=pk)

    form = MovimientoInversionForm(request.POST)
    if not form.is_valid():
        _flash_errores(request, form)
        return redirect('prestamos:detalle_inversion', pk=pk)

    try:
        with transaction.atomic():
            MovimientoInversion.objects.create(
                inversion=inversion,
                fecha=form.cleaned_data['fecha'],
                monto=form.cleaned_data['monto'],
                tipo=form.cleaned_data['tipo'],
                descripcion=form.cleaned_data.get('descripcion', ''),
            )
            registrar_auditoria(request.user, 'crear', 'MovimientoInversion', inversion.pk,
                                f"{form.cleaned_data['tipo']} ${form.cleaned_data['monto']}")
        messages.success(request, "Movimiento registrado exitosamente.")
    except Exception:
        logger.exception("Error al registrar movimiento de inversión %s", pk)
        messages.error(request, "Ocurrió un error al registrar el movimiento.")
    return redirect('prestamos:detalle_inversion', pk=pk)


@login_required
def borrar_inversion(request, pk):
    inversion = get_object_or_404(inversiones_visibles(request.user), pk=pk)
    if request.method != 'POST':
        return render(request, 'prestamos/confirmar_borrado_inversion.html', {'inversion': inversion})
    try:
        with transaction.atomic():
            nombre = inversion.nombre
            inversion.delete()
            registrar_auditoria(request.user, 'borrar', 'Inversion', pk, nombre)
        messages.success(request, "Inversión eliminada.")
    except Exception:
        logger.exception("Error al borrar inversión %s", pk)
        messages.error(request, "Ocurrió un error al eliminar la inversión.")
    return redirect('prestamos:portafolio')


def _flash_errores(request, form):
    for campo, errores in form.errors.items():
        for error in errores:
            messages.error(request, f"{campo}: {error}")


@login_required
def editar_movimiento_inversion(request, pk):
    """Edita un movimiento del portafolio. El queryset se acota a las posiciones
    visibles: un pk ajeno da 404, no una edición silenciosa."""
    movimiento = get_object_or_404(
        MovimientoInversion.objects.filter(inversion__in=inversiones_visibles(request.user)),
        pk=pk,
    )

    if request.method != 'POST':
        form = MovimientoInversionForm(initial={
            'tipo': movimiento.tipo,
            'monto': movimiento.monto,
            'fecha': movimiento.fecha,
            'descripcion': movimiento.descripcion,
        })
        return render(request, 'prestamos/editar_movimiento_inversion.html', {
            'form': form, 'movimiento': movimiento,
        })

    form = MovimientoInversionForm(request.POST)
    if not form.is_valid():
        messages.error(request, "Corrija los errores marcados abajo.")
        return render(request, 'prestamos/editar_movimiento_inversion.html', {
            'form': form, 'movimiento': movimiento,
        })

    try:
        with transaction.atomic():
            movimiento.tipo = form.cleaned_data['tipo']
            movimiento.monto = form.cleaned_data['monto']
            movimiento.fecha = form.cleaned_data['fecha']
            movimiento.descripcion = form.cleaned_data.get('descripcion', '')
            movimiento.save()
            registrar_auditoria(request.user, 'editar', 'MovimientoInversion', movimiento.pk,
                                f"{movimiento.tipo} ${movimiento.monto}")
        messages.success(request, "Movimiento actualizado.")
    except Exception:
        logger.exception("Error al editar movimiento de inversión %s", pk)
        messages.error(request, "Ocurrió un error al actualizar el movimiento.")
    return redirect('prestamos:detalle_inversion', pk=movimiento.inversion_id)


@login_required
def borrar_movimiento_inversion(request, pk):
    movimiento = get_object_or_404(
        MovimientoInversion.objects.filter(inversion__in=inversiones_visibles(request.user)),
        pk=pk,
    )
    inversion_id = movimiento.inversion_id

    if request.method != 'POST':
        return redirect('prestamos:detalle_inversion', pk=inversion_id)

    try:
        with transaction.atomic():
            detalle = f"{movimiento.tipo} ${movimiento.monto} del {movimiento.fecha}"
            movimiento.delete()
            registrar_auditoria(request.user, 'borrar', 'MovimientoInversion', pk, detalle)
        messages.success(request, "Movimiento eliminado.")
    except Exception:
        logger.exception("Error al borrar movimiento de inversión %s", pk)
        messages.error(request, "Ocurrió un error al eliminar el movimiento.")
    return redirect('prestamos:detalle_inversion', pk=inversion_id)


# ============================================
# Importación desde CSV / Excel
#
# Flujo en dos pasos: se parsea y valida el archivo, se muestra qué se va a
# crear, y sólo al confirmar se escribe. Con cifras de dinero, un archivo mal
# armado que entra directo a la base cuesta más de arreglar que de revisar.
# ============================================

MAX_BYTES_IMPORTACION = 2 * 1024 * 1024  # 2 MB: de sobra para 1000 filas


def _destinos_disponibles(user):
    return {
        'prestamo': prestamos_visibles(user).order_by('nombre_cliente'),
        'inversion': inversiones_visibles(user).order_by('nombre'),
    }


@login_required
def importar(request):
    """Paso 1: subir archivo y previsualizar."""
    contexto = {
        'importaciones': IMPORTACIONES,
        'destinos': _destinos_disponibles(request.user),
    }

    if request.method != 'POST':
        return render(request, 'prestamos/importar.html', contexto)

    tipo = request.POST.get('tipo')
    if tipo not in IMPORTACIONES:
        messages.error(request, "Elige qué tipo de datos vas a importar.")
        return render(request, 'prestamos/importar.html', contexto)

    archivo = request.FILES.get('archivo')
    if not archivo:
        messages.error(request, "Adjunta un archivo .csv o .xlsx.")
        return render(request, 'prestamos/importar.html', contexto)
    if archivo.size > MAX_BYTES_IMPORTACION:
        messages.error(request, "El archivo supera los 2 MB.")
        return render(request, 'prestamos/importar.html', contexto)

    # Destino: los movimientos cuelgan de un préstamo o de una inversión.
    destino_tipo = IMPORTACIONES[tipo]['destino']
    destino = None
    if destino_tipo:
        destino_id = request.POST.get('destino_id')
        queryset = _destinos_disponibles(request.user)[destino_tipo]
        destino = queryset.filter(pk=destino_id).first() if destino_id else None
        if destino is None:
            messages.error(request, "Elige a qué registro se van a cargar los movimientos.")
            return render(request, 'prestamos/importar.html', contexto)

    filas, error = leer_tabla(archivo, archivo.name)
    if error:
        messages.error(request, error)
        return render(request, 'prestamos/importar.html', contexto)
    if not filas:
        messages.error(request, "El archivo no tiene filas con datos.")
        return render(request, 'prestamos/importar.html', contexto)

    validas, errores = procesar(filas, tipo)
    duplicados = _marcar_duplicados(validas, tipo, destino)

    # La vista previa viaja en sesión para no exigir subir el archivo dos veces.
    request.session['importacion'] = {
        'tipo': tipo,
        'destino_id': destino.pk if destino else None,
        'filas': [_serializar(f) for f in validas],
    }

    contexto.update({
        'previsualizacion': True,
        'tipo_elegido': tipo,
        'destino': destino,
        'validas': validas,
        'errores': errores,
        'duplicados': duplicados,
        'nombre_archivo': archivo.name,
    })
    return render(request, 'prestamos/importar.html', contexto)


def _serializar(fila):
    """Deja la fila lista para la sesión (JSON no admite date ni Decimal)."""
    salida = {}
    for clave, valor in fila.items():
        if isinstance(valor, date):
            salida[clave] = valor.isoformat()
        elif isinstance(valor, Decimal):
            salida[clave] = str(valor)
        else:
            salida[clave] = valor
    return salida


def _marcar_duplicados(validas, tipo, destino):
    """Fechas+monto que ya existen en el destino. No bloquea: sólo avisa."""
    if not destino or tipo not in ('movimientos_prestamo', 'movimientos_inversion'):
        return []
    existentes = {(m.fecha, m.monto, m.tipo) for m in destino.movimientos.all()}
    return [f['linea'] for f in validas
            if (f['fecha'], f['monto'], f['tipo']) in existentes]


@login_required
def importar_confirmar(request):
    """Paso 2: crear los registros previsualizados."""
    if request.method != 'POST':
        return redirect('prestamos:importar')

    pendiente = request.session.get('importacion')
    if not pendiente or not pendiente.get('filas'):
        messages.error(request, "No hay nada que importar. Sube el archivo de nuevo.")
        return redirect('prestamos:importar')

    tipo = pendiente['tipo']
    filas = pendiente['filas']
    destino_tipo = IMPORTACIONES[tipo]['destino']

    destino = None
    if destino_tipo:
        queryset = _destinos_disponibles(request.user)[destino_tipo]
        destino = queryset.filter(pk=pendiente['destino_id']).first()
        if destino is None:
            messages.error(request, "El registro de destino ya no está disponible.")
            return redirect('prestamos:importar')

    try:
        with transaction.atomic():
            creados = _crear_registros(request.user, tipo, filas, destino)
            registrar_auditoria(request.user, 'crear', 'Importacion', None,
                                f"{tipo}: {creados} registros")
    except Exception:
        logger.exception("Error al importar (%s, user=%s)", tipo, request.user.pk)
        messages.error(request, "Ocurrió un error al importar. No se guardó nada.")
        return redirect('prestamos:importar')

    request.session.pop('importacion', None)
    messages.success(request, f"Se importaron {creados} registros.")

    if destino_tipo == 'prestamo':
        return redirect('prestamos:detalle_prestamo', pk=destino.pk)
    if destino_tipo == 'inversion':
        return redirect('prestamos:detalle_inversion', pk=destino.pk)
    if tipo == 'inversiones':
        return redirect('prestamos:portafolio')
    return redirect('prestamos:lista_prestamos')


def _crear_registros(user, tipo, filas, destino):
    """Crea en bloque. Todo dentro de la transacción de quien llama."""
    if tipo == 'movimientos_prestamo':
        Movimiento.objects.bulk_create([
            Movimiento(prestamo=destino, fecha=date.fromisoformat(f['fecha']),
                       monto=Decimal(f['monto']), tipo=f['tipo'],
                       descripcion=f.get('descripcion', ''))
            for f in filas
        ])
        destino.actualizar_saldo(timezone.now().date())
        return len(filas)

    if tipo == 'movimientos_inversion':
        MovimientoInversion.objects.bulk_create([
            MovimientoInversion(inversion=destino, fecha=date.fromisoformat(f['fecha']),
                                monto=Decimal(f['monto']), tipo=f['tipo'],
                                descripcion=f.get('descripcion', ''))
            for f in filas
        ])
        return len(filas)

    if tipo == 'prestamos':
        for f in filas:
            cliente = Cliente.objects.create(owner=user, nombre=f['nombre_cliente'],
                                             telefono=f.get('telefono', ''))
            Prestamo.objects.create(
                owner=user, cliente=cliente, rol=f['rol'], concepto=f.get('concepto', ''),
                nombre_cliente=f['nombre_cliente'], telefono=f.get('telefono', ''),
                monto_original=Decimal(f['monto_original']),
                tasa_interes_anual=Decimal(f['tasa_interes_anual']),
                tipo_pago=f['tipo_pago'], fecha_inicio=date.fromisoformat(f['fecha_inicio']),
                modo=f['modo'],
                plazo_meses=f['plazo_meses'],
                pago_mensual=Decimal(f['pago_mensual']) if f['pago_mensual'] else None,
                saldo_actual=Decimal(f['monto_original']),
            )
        return len(filas)

    if tipo == 'inversiones':
        Inversion.objects.bulk_create([
            Inversion(owner=user, plataforma=f['plataforma'], nombre=f['nombre'],
                      tipo=f['tipo'], monto_invertido=Decimal(f['monto_invertido']),
                      fecha_compra=date.fromisoformat(f['fecha_compra']),
                      tasa_anual=Decimal(f['tasa_anual']), plazo_dias=f['plazo_dias'],
                      base_dias=f['base_dias'],
                      valor_manual=Decimal(f['valor_manual']) if f['valor_manual'] else None,
                      notas=f.get('notas', ''))
            for f in filas
        ])
        return len(filas)

    return 0
