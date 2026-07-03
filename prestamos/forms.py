from django import forms
from decimal import Decimal
from datetime import date

from .models import Cliente

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
    nombre = forms.CharField(label='Nombre del Cliente', max_length=200)
    telefono = forms.CharField(label='Teléfono', max_length=20, required=False)
    monto_original = forms.DecimalField(label='Monto Original', min_value=0)
    tasa_interes_anual = forms.DecimalField(label='Tasa de Interés Anual (%)', min_value=0)
    tipo_pago = forms.ChoiceField(
        label='Frecuencia de Pago',
        choices=[('mensual', 'Mensual'), ('semanal', 'Semanal')]
    )
    fecha_inicio = forms.DateField(label='Fecha de Inicio', initial=date.today)
    modo = forms.ChoiceField(label='Modo', choices=[('fixed_term', 'Plazo Fijo'), ('fixed_payment', 'Pago Fijo')])
    plazo_meses = forms.IntegerField(label='Plazo en Periodos', min_value=1, required=False)
    pago_mensual = forms.DecimalField(label='Pago Fijo', min_value=0, required=False)

    def clean(self):
        cleaned_data = super().clean()
        modo = cleaned_data.get('modo')
        plazo = cleaned_data.get('plazo_meses')
        pago = cleaned_data.get('pago_mensual')

        if modo == 'fixed_term' and not plazo:
            self.add_error('plazo_meses', 'Debe proporcionar el plazo para el préstamo.')
        if modo == 'fixed_payment' and not pago:
            self.add_error('pago_mensual', 'Debe proporcionar el pago mensual fijo.')

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
