from django import forms
from decimal import Decimal
from datetime import date

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
