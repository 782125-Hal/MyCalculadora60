from django.shortcuts import render, redirect, get_object_or_404
from django.contrib import messages
from django.db import transaction
from django.utils import timezone
from django.views import View
from django.views.generic import DetailView
from .models import Cliente, Prestamo, Movimiento
from .forms import CalculatorForm, RegistrationForm, RegistrarPrestamoForm
import math
from decimal import Decimal
from datetime import date, datetime

def home(request):
    """Vista para la página principal con enlaces a las funcionalidades."""
    aplicaciones = [
        {"nombre": "Calculadora de Inversiones", "descripcion": "Evalúa proyectos financieros", "url": "prestamos:inversiones"},
        {"nombre": "Calculadora Financiera", "url": "prestamos:calculadora_financiera", "descripcion": "Calcula cuotas de préstamos"},
        {"nombre": "Ver Préstamos", "url": "prestamos:lista_prestamos", "descripcion": "Consulta los préstamos registrados"},
        {"nombre": "Registrar Préstamo", "url": "prestamos:registrar_prestamo", "descripcion": "Registra un nuevo préstamo"},
    ]
    return render(request, 'prestamos/home.html', {'aplicaciones': aplicaciones})

def lista_prestamos(request):
    """Vista para listar todos los préstamos con actualización diaria del saldo (Punto 5)."""
    prestamos = Prestamo.objects.all()
    hoy = timezone.now().date()
    for prestamo in prestamos:
        prestamo.actualizar_saldo(hoy)  # Actualiza el saldo considerando pagos e intereses
    return render(request, 'prestamos/lista_prestamos.html', {'prestamos': prestamos})

class CalculadoraView(View):
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
            r = float(tasa) / 100 / 12  # Tasa mensual

            if tipo_calculo == 'pago':
                n = plazo_meses_input
                if r == 0:
                    pago_calculado = float(monto) / n
                else:
                    pago_calculado = float(monto) * r * (1 + r)**n / ((1 + r)**n - 1)
                calculated_payment = Decimal(pago_calculado).quantize(Decimal('0.01'))
                calculated_term = plazo_meses_input
                result = f'Pago mensual calculado: {calculated_payment}'
            elif tipo_calculo == 'plazo':
                pago = pago_mensual_input
                if r == 0:
                    plazo_calculado = math.ceil(float(monto) / float(pago))
                else:
                    interest = float(monto) * r
                    if float(pago) <= interest:
                        messages.error(request, 'El pago mensual es insuficiente para cubrir los intereses.')
                        return render(request, 'prestamos/calculadora_financiera.html', {'form': form})
                    plazo_calculado = math.log(float(pago) / (float(pago) - interest)) / math.log(1 + r)
                    plazo_calculado = math.ceil(plazo_calculado)
                calculated_term = plazo_calculado
                calculated_payment = Decimal(pago)
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
                        cliente = Cliente.objects.create(nombre=nombre, telefono='N/A')
                        prestamo = Prestamo(
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
                        messages.success(request, 'Préstamo registrado exitosamente.')
                        return redirect('prestamos:detalle_prestamo', pk=prestamo.pk)
                except Exception as e:
                    messages.error(request, f"Error al registrar: {str(e)}")
            else:
                messages.error(request, "Corrija los errores en el formulario de registro.")

        context = {
            'form': form,
            'result': result,
            'reg_form': reg_form,
        }
        return render(request, 'prestamos/calculadora_financiera.html', context)

class RegistrarPrestamoView(View):
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
                    cliente = Cliente.objects.create(nombre=nombre, telefono=telefono)
                    prestamo = Prestamo.objects.create(
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
                    messages.success(request, "Préstamo registrado exitosamente.")
                    if 'calculadora_data' in request.session:
                        del request.session['calculadora_data']
                    return redirect('prestamos:detalle_prestamo', pk=prestamo.pk)
            except Exception as e:
                messages.error(request, f"Error al registrar: {str(e)}")
        else:
            messages.error(request, "Corrija los errores en el formulario.")
        return render(request, 'prestamos/registrar_prestamo.html', {'form': form})

class PrestamoDetailView(DetailView):
    """Vista para mostrar detalles del préstamo con amortización y movimientos (Punto 4)."""
    model = Prestamo
    template_name = 'prestamos/detalle_prestamo.html'

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

def registrar_pago(request, prestamo_id):
    """Vista para registrar un pago en un préstamo (Punto 4)."""
    if request.method == 'POST':
        prestamo = get_object_or_404(Prestamo, id=prestamo_id)
        monto_str = request.POST.get('monto', '0')
        fecha_str = request.POST.get('fecha', timezone.now().date().isoformat())
        descripcion = request.POST.get('descripcion', 'Pago registrado')
        try:
            monto = Decimal(monto_str)
            fecha = datetime.strptime(fecha_str, '%Y-%m-%d').date()
            if monto <= 0:
                raise ValueError("El monto debe ser mayor que cero.")
            with transaction.atomic():
                Movimiento.objects.create(
                    prestamo=prestamo,
                    fecha=fecha,
                    monto=monto,
                    tipo='pago',
                    descripcion=descripcion
                )
                prestamo.actualizar_saldo(fecha)  # Actualiza saldo tras el pago
                messages.success(request, "Pago registrado exitosamente.")
        except ValueError as e:
            messages.error(request, f"Error al registrar el pago: {str(e)}")
        except Exception as e:
            messages.error(request, f"Error inesperado: {str(e)}")
        return redirect('prestamos:detalle_prestamo', pk=prestamo_id)
    return redirect('prestamos:detalle_prestamo', pk=prestamo_id)

def registrar_incremento(request, prestamo_id):
    """Vista para registrar un incremento de capital (Punto 4)."""
    if request.method == 'POST':
        prestamo = get_object_or_404(Prestamo, id=prestamo_id)
        monto_str = request.POST.get('monto_incremento', '0')
        fecha_str = request.POST.get('fecha', timezone.now().date().isoformat())
        descripcion = request.POST.get('descripcion', 'Incremento de Capital')
        try:
            monto = Decimal(monto_str)
            fecha = datetime.strptime(fecha_str, '%Y-%m-%d').date()
            if monto <= 0:
                raise ValueError("El monto debe ser mayor que cero.")
            with transaction.atomic():
                prestamo.registrar_incremento(monto, fecha)
                messages.success(request, "Incremento de capital registrado exitosamente.")
        except ValueError as e:
            messages.error(request, f"Error al registrar el incremento: {str(e)}")
        except Exception as e:
            messages.error(request, f"Error inesperado: {str(e)}")
        return redirect('prestamos:detalle_prestamo', pk=prestamo_id)
    return redirect('prestamos:detalle_prestamo', pk=prestamo_id)

def editar_movimiento(request, movimiento_id):
    """Vista para editar un movimiento existente (Punto 4)."""
    movimiento = get_object_or_404(Movimiento, id=movimiento_id)
    prestamo_id = movimiento.prestamo.id
    if request.method == 'POST':
        try:
            nuevo_monto = Decimal(request.POST.get('monto', '0'))
            nueva_fecha = datetime.strptime(
                request.POST.get('fecha', timezone.now().date().isoformat()), '%Y-%m-%d'
            ).date()
            if nuevo_monto <= 0:
                raise ValueError("El monto debe ser mayor que cero.")
            with transaction.atomic():
                movimiento.monto = nuevo_monto
                movimiento.fecha = nueva_fecha
                movimiento.descripcion = request.POST.get('descripcion', movimiento.descripcion)
                movimiento.save()
                movimiento.prestamo.actualizar_saldo()  # Recalcula saldo
                messages.success(request, "Movimiento editado exitosamente.")
                return redirect('prestamos:detalle_prestamo', pk=prestamo_id)
        except ValueError as e:
            messages.error(request, f"Error al editar el movimiento: {str(e)}")
        except Exception as e:
            messages.error(request, f"Error inesperado: {str(e)}")
    return render(request, 'prestamos/editar_movimiento.html', {'movimiento': movimiento})

def borrar_movimiento(request, movimiento_id):
    """Vista para borrar un movimiento (Punto 4)."""
    movimiento = get_object_or_404(Movimiento, id=movimiento_id)
    prestamo_id = movimiento.prestamo.id
    if request.method == 'POST':
        try:
            with transaction.atomic():
                prestamo = movimiento.prestamo  # Guardar referencia antes del delete
                movimiento.delete()
                prestamo.actualizar_saldo()  # Recalcula saldo con referencia segura
                messages.success(request, "Movimiento borrado exitosamente.")
        except Exception as e:
            messages.error(request, f"Error al borrar el movimiento: {str(e)}")
    return redirect('prestamos:detalle_prestamo', pk=prestamo_id)

def editar_prestamo(request, prestamo_id):
    """Vista para editar los datos de un préstamo."""
    prestamo = get_object_or_404(Prestamo, id=prestamo_id)
    if request.method == 'POST':
        try:
            monto = Decimal(request.POST.get('monto_original', prestamo.monto_original))
            tasa_interes_anual = Decimal(request.POST.get('tasa_interes_anual', prestamo.tasa_interes_anual))
            tipo_pago = request.POST.get('tipo_pago', prestamo.tipo_pago)
            if monto <= 0 or tasa_interes_anual < 0:
                raise ValueError("El monto debe ser mayor que cero y la tasa no puede ser negativa.")
            with transaction.atomic():
                prestamo.monto_original = monto
                prestamo.tasa_interes_anual = tasa_interes_anual
                prestamo.tipo_pago = tipo_pago
                prestamo.saldo_actual = monto  # Resetear saldo
                prestamo.save()
                prestamo.actualizar_saldo()  # Recalcula saldo
                messages.success(request, "Préstamo actualizado exitosamente.")
                return redirect('prestamos:detalle_prestamo', pk=prestamo_id)
        except ValueError as e:
            messages.error(request, f"Error al actualizar el préstamo: {str(e)}")
        except Exception as e:
            messages.error(request, f"Error inesperado: {str(e)}")
    return render(request, 'prestamos/editar_prestamo.html', {'prestamo': prestamo})

def delete_prestamo(request, prestamo_id):
    """Vista para eliminar un préstamo (Punto 4)."""
    prestamo = get_object_or_404(Prestamo, id=prestamo_id)
    if request.method == 'POST':
        try:
            with transaction.atomic():
                prestamo.delete()  # Deletes loan and related movements due to CASCADE
                messages.success(request, f"El préstamo #{prestamo_id} ha sido eliminado exitosamente.")
                return redirect('prestamos:lista_prestamos')
        except Exception as e:
            messages.error(request, f"Error al eliminar el préstamo: {str(e)}")
    return render(request, 'prestamos/confirmar_borrado.html', {
        'prestamo': prestamo,
        'titulo': 'Confirmar Eliminación',
        'mensaje_confirmacion': '¿Está seguro de eliminar este préstamo?'
    })

def crear_prestamo(request):
    """Vista para crear un préstamo desde un formulario simple."""
    if request.method == 'POST':
        try:
            cliente_id = request.POST.get('cliente')
            monto = Decimal(request.POST.get('monto_original'))
            tipo_pago = request.POST.get('tipo_pago')
            fecha_inicio = datetime.strptime(request.POST.get('fecha_inicio'), '%Y-%m-%d').date()
            tasa_interes_anual = Decimal(request.POST.get('tasa_interes_anual'))
            plazo_periodos = int(request.POST.get('plazo_periodos', 36))
            cliente = Cliente.objects.get(id=cliente_id)
            with transaction.atomic():
                prestamo = Prestamo.objects.create(
                    cliente=cliente,
                    nombre_cliente=cliente.nombre,
                    monto_original=monto,
                    tipo_pago=tipo_pago,
                    fecha_inicio=fecha_inicio,
                    tasa_interes_anual=tasa_interes_anual,
                    saldo_actual=monto,
                    plazo_meses=plazo_periodos
                )
                messages.success(request, "Préstamo creado exitosamente.")
                return redirect('prestamos:detalle_prestamo', pk=prestamo.pk)
        except ValueError as e:
            messages.error(request, f"Error al crear el préstamo: {str(e)}")
        except Exception as e:
            messages.error(request, f"Error inesperado: {str(e)}")
    clientes = Cliente.objects.all()
    return render(request, 'prestamos/crear_prestamo.html', {'clientes': clientes})

def inversiones(request):
    """Vista para la calculadora de inversiones."""
    return render(request, 'prestamos/inversiones.html', {'title': 'Calculadora de Inversiones'})

def registrar_inversion(request):
    """Vista para registrar una inversión como préstamo."""
    if request.method == 'POST':
        try:
            inversion_inicial = Decimal(request.POST.get('inversionInicial'))
            tasa_descuento = Decimal(request.POST.get('tasaDescuento'))
            anos = int(request.POST.get('anos', 0))
            if inversion_inicial <= 0 or anos <= 0:
                raise ValueError("Valores inválidos para inversión.")
            with transaction.atomic():
                cliente = Cliente.objects.create(nombre="Inversión Automática", telefono="N/A")
                prestamo = Prestamo.objects.create(
                    cliente=cliente,
                    nombre_cliente="Inversión Automática",
                    monto_original=inversion_inicial,
                    tipo_pago="mensual",
                    fecha_inicio=timezone.now().date(),
                    tasa_interes_anual=tasa_descuento,
                    saldo_actual=inversion_inicial,
                    plazo_meses=anos * 12
                )
                messages.success(request, "Inversión registrada exitosamente.")
                return redirect('prestamos:detalle_prestamo', pk=prestamo.pk)
        except ValueError as e:
            messages.error(request, f"Error al registrar la inversión: {str(e)}")
        except Exception as e:
            messages.error(request, f"Error inesperado: {str(e)}")
    return render(request, 'prestamos/inversiones.html')

def calcular_pago_mensual(monto, tasa_anual, plazo_meses):
    """Función auxiliar para calcular el pago mensual (Punto 1)."""
    tasa_mensual = float(tasa_anual) / 12 / 100
    if tasa_mensual == 0:
        return Decimal(monto) / plazo_meses
    pago = float(monto) * tasa_mensual * (1 + tasa_mensual) ** plazo_meses / ((1 + tasa_mensual) ** plazo_meses - 1)
    return Decimal(pago).quantize(Decimal('0.01'))

def calcular_plazo_meses(monto, tasa_anual, pago_mensual):
    """Función auxiliar para calcular el plazo en meses (Punto 2)."""
    tasa_mensual = float(tasa_anual) / 12 / 100
    if tasa_mensual == 0:
        return math.ceil(float(monto) / float(pago_mensual))
    if float(pago_mensual) <= float(monto) * tasa_mensual:
        raise ValueError("El pago mensual es insuficiente para cubrir los intereses.")
    plazo = math.log(float(pago_mensual) / (float(pago_mensual) - float(monto) * tasa_mensual)) / math.log(1 + tasa_mensual)
    return math.ceil(plazo)