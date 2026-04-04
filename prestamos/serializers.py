from decimal import Decimal

from rest_framework import serializers
from .models import Cliente, Prestamo, Movimiento


class ClienteSerializer(serializers.ModelSerializer):
    class Meta:
        model = Cliente
        fields = ['id', 'nombre', 'telefono']


class MovimientoSerializer(serializers.ModelSerializer):
    class Meta:
        model = Movimiento
        fields = ['id', 'prestamo', 'fecha', 'monto', 'tipo', 'descripcion']
        read_only_fields = ['id']


class PrestamoSerializer(serializers.ModelSerializer):
    movimientos = MovimientoSerializer(many=True, read_only=True)
    amortizacion = serializers.SerializerMethodField()

    class Meta:
        model = Prestamo
        fields = [
            'id', 'cliente', 'nombre_cliente', 'telefono',
            'monto_original', 'tasa_interes_anual', 'tipo_pago',
            'fecha_inicio', 'saldo_actual', 'pago_mensual',
            'plazo_meses', 'activo', 'ultimo_pago', 'modo',
            'movimientos', 'amortizacion',
        ]
        read_only_fields = ['id', 'saldo_actual', 'ultimo_pago', 'movimientos', 'amortizacion']

    def get_amortizacion(self, obj):
        return obj.get_amortizacion()


class PrestamoListSerializer(serializers.ModelSerializer):
    """Serializer ligero para listados (sin amortización ni movimientos)."""
    class Meta:
        model = Prestamo
        fields = [
            'id', 'nombre_cliente', 'telefono', 'monto_original',
            'tasa_interes_anual', 'tipo_pago', 'fecha_inicio',
            'saldo_actual', 'activo', 'modo',
        ]


class RegistrarPagoSerializer(serializers.Serializer):
    monto = serializers.DecimalField(max_digits=15, decimal_places=2, min_value=Decimal('0.01'))
    fecha = serializers.DateField()
    descripcion = serializers.CharField(required=False, default='Pago registrado')


class RegistrarIncrementoSerializer(serializers.Serializer):
    monto = serializers.DecimalField(max_digits=15, decimal_places=2, min_value=Decimal('0.01'))
    fecha = serializers.DateField()
    descripcion = serializers.CharField(required=False, default='Incremento de capital')
