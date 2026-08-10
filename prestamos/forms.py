from django import forms
from decimal import Decimal
from datetime import date

from .models import Cliente, Prestamo, Inversion, MovimientoInversion

class CalculatorForm(forms.Form):
    monto = forms.DecimalField(label='Monto del Préstamo', min_value=0)
    tasa = forms.DecimalField(label='Tasa de Interés Anual (%)', min_value=0)
    tipo_calculo = forms.ChoiceField(
        label='Tipo de Cálculo',
        choices=[('pago', 'Calcular Pago Mensual'), ('plazo', 'Calcular Plazo en Meses')]
    )
    pago_mensual = forms.DecimalField(label='Pago Mensual Deseado', min_value=0, required=False)
    plazo_meses = forms.IntegerField(label='Plazo Deseado (meses)', min_value=1, required=False)

    def clean(self):
        cleaned_data = super().clean()
        tipo = cleaned_data.get('tipo_calculo')
        pago = cleaned_data.get('pago_mensual')
        plazo = cleaned_data.get('plazo_meses')

        if tipo == 'pago' and not plazo:
            self.add_error('plazo_meses', 'Proporcione el plazo para calcular el pago.')
        if tipo == 'plazo' and not pago:
            self.add_error('pago_mensual', 'Proporcione el pago para calcular el plazo.')

        return cleaned_data


class RegistrationForm(forms.Form):
    nombre = forms.CharField(label='Nombre del Cliente', max_length=200)
    fecha_inicio = forms.DateField(label='Fecha del Préstamo', initial=date.today)
    monto = forms.DecimalField(widget=forms.HiddenInput())
    tasa = forms.DecimalField(widget=forms.HiddenInput())
    pago_mensual = forms.DecimalField(widget=forms.HiddenInput())
    plazo_meses = forms.IntegerField(widget=forms.HiddenInput())


class RegistrarPrestamoForm(forms.Form):
    """
    Alta de un préstamo otorgado o de una deuda propia.

    Sólo uno de `plazo_meses` / `pago_mensual` es obligatorio, según `modo`.
    Ninguno de los dos se prellena con 0: un 0 se lee como "campo ya lleno"
    pero no pasa la validación, que era la causa de que el alta no avanzara.
    """
    rol = forms.ChoiceField(
        label='Tipo de registro',
        choices=Prestamo.ROL_CHOICES,
        initial=Prestamo.ROL_PRESTAMO,
        help_text='"Deuda propia" es para lo que tú debes: una casa, un terreno, un auto.',
    )
    concepto = forms.CharField(
        label='Concepto', max_length=200, required=False,
        help_text='Opcional. Ej: "Terreno en Misiones".',
    )
    nombre = forms.CharField(
        label='Contraparte', max_length=200,
        help_text='El cliente que te debe, o el acreedor / vendedor al que le debes.',
    )
    telefono = forms.CharField(label='Teléfono', max_length=20, required=False)
    monto_original = forms.DecimalField(
        label='Monto Original',
        min_value=Decimal('0.01'), decimal_places=2, max_digits=15,
        error_messages={'min_value': 'El monto debe ser mayor que cero.'},
    )
    tasa_interes_anual = forms.DecimalField(
        label='Tasa de Interés Anual (%)',
        min_value=Decimal('0'), decimal_places=2, max_digits=5,
        help_text='Usa 0 si no genera intereses.',
    )
    tipo_pago = forms.ChoiceField(
        label='Frecuencia de Pago',
        choices=[('mensual', 'Mensual'), ('semanal', 'Semanal')]
    )
    fecha_inicio = forms.DateField(
        label='Fecha de Inicio', initial=date.today,
        widget=forms.DateInput(attrs={'type': 'date'}, format='%Y-%m-%d'),
    )
    modo = forms.ChoiceField(
        label='Modo',
        choices=[('fixed_term', 'Plazo Fijo — sé en cuántos períodos se liquida'),
                 ('fixed_payment', 'Pago Fijo — sé cuánto se paga cada período')],
    )
    plazo_meses = forms.IntegerField(label='Plazo en Períodos', min_value=1, required=False)
    pago_mensual = forms.DecimalField(
        label='Pago por Período',
        min_value=Decimal('0.01'), decimal_places=2, max_digits=15, required=False,
        error_messages={'min_value': 'El pago debe ser mayor que cero.'},
    )

    def clean(self):
        cleaned_data = super().clean()
        modo = cleaned_data.get('modo')
        plazo = cleaned_data.get('plazo_meses')
        pago = cleaned_data.get('pago_mensual')

        if modo == 'fixed_term' and not plazo:
            self.add_error('plazo_meses',
                           'En modo Plazo Fijo debes indicar en cuántos períodos se liquida.')
        if modo == 'fixed_payment' and not pago:
            self.add_error('pago_mensual',
                           'En modo Pago Fijo debes indicar cuánto se paga cada período.')

        return cleaned_data


# ============================================
# Forms para acciones manuales (Fase 2)
# Reemplazan el manejo crudo de request.POST.get()
# ============================================

class PagoForm(forms.Form):
    """Formulario para registrar un pago contra un préstamo."""
    monto = forms.DecimalField(
        label='Monto del Pago',
        min_value=Decimal('0.01'),
        decimal_places=2,
        max_digits=15,
    )
    fecha = forms.DateField(
        label='Fecha del Pago',
        initial=date.today,
        widget=forms.DateInput(attrs={'type': 'date'})
    )
    descripcion = forms.CharField(
        label='Descripción',
        required=False,
        max_length=200,
        initial='Pago registrado'
    )

    def clean_monto(self):
        monto = self.cleaned_data['monto']
        if monto <= 0:
            raise forms.ValidationError("El monto debe ser mayor que cero.")
        return monto


class IncrementoForm(forms.Form):
    """Formulario para registrar un incremento de capital."""
    monto = forms.DecimalField(
        label='Monto del Incremento',
        min_value=Decimal('0.01'),
        decimal_places=2,
        max_digits=15,
    )
    fecha = forms.DateField(
        label='Fecha del Incremento',
        initial=date.today,
        widget=forms.DateInput(attrs={'type': 'date'})
    )
    descripcion = forms.CharField(
        label='Descripción',
        required=False,
        max_length=200,
        initial='Incremento de capital'
    )

    def clean_monto(self):
        monto = self.cleaned_data['monto']
        if monto <= 0:
            raise forms.ValidationError("El monto debe ser mayor que cero.")
        return monto


class MovimientoForm(forms.Form):
    """Formulario para editar un movimiento existente (pago o incremento)."""
    monto = forms.DecimalField(
        label='Monto',
        min_value=Decimal('0.01'),
        decimal_places=2,
        max_digits=15,
    )
    fecha = forms.DateField(
        label='Fecha',
        widget=forms.DateInput(attrs={'type': 'date'})
    )
    descripcion = forms.CharField(
        label='Descripción',
        required=False,
        max_length=300,
        widget=forms.Textarea(attrs={'rows': 2})
    )

    def clean_monto(self):
        monto = self.cleaned_data['monto']
        if monto <= 0:
            raise forms.ValidationError("El monto debe ser mayor que cero.")
        return monto


class PrestamoEditForm(forms.Form):
    """Formulario simple para editar datos básicos de un préstamo."""
    monto_original = forms.DecimalField(
        label='Monto Original',
        min_value=Decimal('0.01'),
        decimal_places=2,
        max_digits=15,
    )
    tasa_interes_anual = forms.DecimalField(
        label='Tasa de Interés Anual (%)',
        min_value=Decimal('0'),
        decimal_places=2,
        max_digits=5,
    )
    tipo_pago = forms.ChoiceField(
        label='Frecuencia de Pago',
        choices=[('mensual', 'Mensual'), ('semanal', 'Semanal')],
    )

    def clean(self):
        cleaned = super().clean()
        monto = cleaned.get('monto_original')
        tasa = cleaned.get('tasa_interes_anual')
        if monto is not None and monto <= 0:
            self.add_error('monto_original', "El monto debe ser mayor que cero.")
        if tasa is not None and tasa < 0:
            self.add_error('tasa_interes_anual', "La tasa no puede ser negativa.")
        return cleaned


class CrearPrestamoSimpleForm(forms.Form):
    """Valida el formulario simple de creación de préstamo.

    Los nombres de los campos coinciden con los del template
    (crear_prestamo.html): 'cliente', 'monto', 'periodos_totales', etc.
    """
    cliente = forms.ModelChoiceField(
        label='Cliente',
        queryset=Cliente.objects.all(),
        error_messages={'invalid_choice': 'El cliente seleccionado no existe.'},
    )
    monto = forms.DecimalField(
        label='Monto', min_value=Decimal('0.01'), decimal_places=2, max_digits=15,
    )
    tasa_interes_anual = forms.DecimalField(
        label='Tasa de Interés Anual (%)', min_value=Decimal('0'), decimal_places=2, max_digits=5,
    )
    tipo_pago = forms.ChoiceField(
        label='Frecuencia de Pago',
        choices=[('mensual', 'Mensual'), ('semanal', 'Semanal')],
    )
    fecha_inicio = forms.DateField(label='Fecha de Inicio', initial=date.today)
    periodos_totales = forms.IntegerField(
        label='Plazo en Periodos', min_value=1, max_value=600, initial=36,
    )


class RegistrarInversionForm(forms.Form):
    """Valida los campos escalares de la calculadora de inversiones.

    Los nombres coinciden con los del template (inversiones.html).
    Los movimientos simulados (movimiento_*_{idx}) se procesan aparte.
    """
    inversionInicial = forms.DecimalField(
        label='Inversión Inicial', min_value=Decimal('0.01'), decimal_places=2, max_digits=15,
    )
    tasaDescuento = forms.DecimalField(
        label='Tasa de Descuento (%)', min_value=Decimal('0'), decimal_places=2, max_digits=5,
    )
    anos = forms.IntegerField(label='Años', min_value=1, max_value=100)
    fecha_inicio_simulacion = forms.DateField(
        label='Fecha de Inicio', required=False, initial=date.today,
    )


class InversionForm(forms.Form):
    """Alta y edición de una posición del portafolio.

    Los campos obligatorios dependen del tipo: un fondo no tiene plazo ni tasa
    proyectable y necesita valor capturado; los demás necesitan tasa y plazo
    para poder proyectarse.
    """
    plataforma = forms.ChoiceField(label='Plataforma', choices=Inversion.PLATAFORMA_CHOICES)
    nombre = forms.CharField(
        label='Instrumento', max_length=200,
        help_text='Ej: "CETES 28 días" o "Torre Guadalajara".',
    )
    tipo = forms.ChoiceField(
        label='Tipo de instrumento', choices=Inversion.TIPO_CHOICES,
        help_text='Los fondos no se proyectan: hay que capturar su valor.',
    )
    monto_invertido = forms.DecimalField(
        label='Monto invertido', min_value=Decimal('0.01'),
        max_digits=15, decimal_places=2,
        error_messages={'min_value': 'El monto debe ser mayor que cero.'},
    )
    fecha_compra = forms.DateField(
        label='Fecha de compra', initial=date.today,
        widget=forms.DateInput(attrs={'type': 'date'}, format='%Y-%m-%d'),
    )
    tasa_anual = forms.DecimalField(
        label='Tasa anual (%)', required=False, min_value=Decimal('0'),
        max_digits=6, decimal_places=3,
        help_text='La tasa nominal que te dieron al contratar.',
    )
    plazo_dias = forms.IntegerField(
        label='Plazo (días)', required=False, min_value=1,
        help_text='CETES: 28, 91, 182, 364… Briq: el plazo del proyecto.',
    )
    base_dias = forms.ChoiceField(
        label='Base de cálculo',
        choices=[(360, '360 días — CETES y mercado de dinero'), (365, '365 días — resto')],
        initial=360,
    )
    valor_manual = forms.DecimalField(
        label='Valor actual capturado', required=False, min_value=Decimal('0'),
        max_digits=15, decimal_places=2,
        help_text='Obligatorio en fondos. En los demás, sólo si quieres forzar un valor.',
    )
    fecha_valor = forms.DateField(
        label='Valor a la fecha', required=False,
        widget=forms.DateInput(attrs={'type': 'date'}, format='%Y-%m-%d'),
        help_text='A qué día corresponde el valor capturado. Las aportaciones y retiros '
                  'posteriores se suman o restan sobre él. Vacío = fecha de compra.',
    )
    notas = forms.CharField(label='Notas', required=False, widget=forms.Textarea(attrs={'rows': 2}))

    def clean(self):
        cleaned = super().clean()
        tipo = cleaned.get('tipo')

        if tipo == Inversion.TIPO_FONDO:
            if cleaned.get('valor_manual') is None:
                self.add_error('valor_manual',
                               'Un fondo no se puede proyectar: captura su valor actual.')
        else:
            if not cleaned.get('tasa_anual'):
                self.add_error('tasa_anual', 'Indica la tasa anual para poder proyectar el valor.')
            if not cleaned.get('plazo_dias'):
                self.add_error('plazo_dias', 'Indica el plazo en días para poder proyectar el valor.')

        return cleaned


class MovimientoInversionForm(forms.Form):
    """Aportación, retiro o rendimiento cobrado sobre una posición."""
    tipo = forms.ChoiceField(choices=MovimientoInversion.TIPO_CHOICES)
    monto = forms.DecimalField(min_value=Decimal('0.01'), max_digits=15, decimal_places=2,
                               error_messages={'min_value': 'El monto debe ser mayor que cero.'})
    fecha = forms.DateField(required=False, initial=date.today)
    descripcion = forms.CharField(max_length=200, required=False)

    def clean_fecha(self):
        return self.cleaned_data.get('fecha') or date.today()
