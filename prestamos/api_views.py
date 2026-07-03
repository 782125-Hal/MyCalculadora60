from decimal import Decimal

from django.db import transaction
from django.utils import timezone
from rest_framework import viewsets, status
from rest_framework.decorators import action
from rest_framework.exceptions import PermissionDenied
from rest_framework.response import Response

from .models import Cliente, Prestamo, Movimiento
from .calculator import calculate_payment_for_term, calculate_term_for_payment
from .serializers import (
    ClienteSerializer,
    PrestamoSerializer,
    PrestamoListSerializer,
    MovimientoSerializer,
    RegistrarPagoSerializer,
    RegistrarIncrementoSerializer,
)


class ClienteViewSet(viewsets.ModelViewSet):
    """
    CRUD completo de clientes.
    GET    /api/clientes/          → lista
    POST   /api/clientes/          → crear
    GET    /api/clientes/{id}/     → detalle
    PUT    /api/clientes/{id}/     → actualizar
    DELETE /api/clientes/{id}/     → eliminar
    GET    /api/clientes/{id}/prestamos/ → préstamos del cliente
    """
    queryset = Cliente.objects.all().order_by('nombre')
    serializer_class = ClienteSerializer

    def get_queryset(self):
        # Aislamiento por usuario de la PII de clientes.
        return Cliente.objects.filter(owner=self.request.user).order_by('nombre')

    def perform_create(self, serializer):
        serializer.save(owner=self.request.user)

    @action(detail=True, methods=['get'])
    def prestamos(self, request, pk=None):
        cliente = self.get_object()
        prestamos = cliente.prestamo_set.filter(owner=request.user)
        serializer = PrestamoListSerializer(prestamos, many=True)
        return Response(serializer.data)


class PrestamoViewSet(viewsets.ModelViewSet):
    """
    CRUD completo de préstamos + acciones especiales.

    GET    /api/prestamos/                          → lista (ligera)
    POST   /api/prestamos/                          → crear
    GET    /api/prestamos/{id}/                     → detalle con amortización y movimientos
    PUT    /api/prestamos/{id}/                     → actualizar
    DELETE /api/prestamos/{id}/                     → eliminar

    POST   /api/prestamos/{id}/registrar_pago/      → registrar un pago
    POST   /api/prestamos/{id}/registrar_incremento/→ registrar incremento de capital
    GET    /api/prestamos/{id}/amortizacion/        → tabla de amortización
    POST   /api/prestamos/calcular/                 → calcular pago o plazo sin guardar
    """
    queryset = Prestamo.objects.all().order_by('-fecha_inicio')

    def get_queryset(self):
        # Aislamiento por usuario: cada quien solo ve/edita sus préstamos.
        return Prestamo.objects.filter(owner=self.request.user).order_by('-fecha_inicio')

    def perform_create(self, serializer):
        serializer.save(owner=self.request.user)

    def get_serializer_class(self):
        if self.action == 'list':
            return PrestamoListSerializer
        return PrestamoSerializer

    def retrieve(self, request, *args, **kwargs):
        prestamo = self.get_object()
        prestamo.actualizar_saldo(timezone.now().date())
        serializer = self.get_serializer(prestamo)
        return Response(serializer.data)

    @action(detail=True, methods=['post'])
    def registrar_pago(self, request, pk=None):
        prestamo = self.get_object()
        serializer = RegistrarPagoSerializer(data=request.data)
        if not serializer.is_valid():
            return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)

        with transaction.atomic():
            Movimiento.objects.create(
                prestamo=prestamo,
                fecha=serializer.validated_data['fecha'],
                monto=serializer.validated_data['monto'],
                tipo='pago',
                descripcion=serializer.validated_data['descripcion'],
            )
            saldo = prestamo.actualizar_saldo(serializer.validated_data['fecha'])

        return Response({'saldo_actual': str(saldo)}, status=status.HTTP_201_CREATED)

    @action(detail=True, methods=['post'])
    def registrar_incremento(self, request, pk=None):
        prestamo = self.get_object()
        serializer = RegistrarIncrementoSerializer(data=request.data)
        if not serializer.is_valid():
            return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)

        with transaction.atomic():
            fecha = serializer.validated_data['fecha']
            prestamo.registrar_incremento(serializer.validated_data['monto'], fecha)

        return Response({'saldo_actual': str(prestamo.saldo_actual)}, status=status.HTTP_201_CREATED)

    @action(detail=True, methods=['get'])
    def amortizacion(self, request, pk=None):
        prestamo = self.get_object()
        tabla = prestamo.get_amortizacion()
        return Response(tabla)

    @action(detail=False, methods=['post'])
    def calcular(self, request):
        """
        Calcula pago mensual o plazo sin guardar nada en la BD.

        Body esperado:
          { "monto": 100000, "tasa": 12.0, "tipo_calculo": "pago", "plazo_meses": 24 }
          { "monto": 100000, "tasa": 12.0, "tipo_calculo": "plazo", "pago_mensual": 5000 }
        """
        monto = request.data.get('monto')
        tasa = request.data.get('tasa')
        tipo_calculo = request.data.get('tipo_calculo')

        if not all([monto, tasa, tipo_calculo]):
            return Response(
                {'error': 'Se requieren monto, tasa y tipo_calculo.'},
                status=status.HTTP_400_BAD_REQUEST,
            )

        try:
            monto = Decimal(str(monto))
            tasa = Decimal(str(tasa))
        except Exception:
            return Response({'error': 'Valores numéricos inválidos.'}, status=status.HTTP_400_BAD_REQUEST)

        # Optional: support 'tipo_pago' ('mensual' | 'semanal') for full parity with saved loans
        tipo_pago = request.data.get('tipo_pago', 'mensual')

        if tipo_calculo == 'pago':
            n = request.data.get('plazo_meses')
            if not n:
                return Response({'error': 'Se requiere plazo_meses.'}, status=status.HTTP_400_BAD_REQUEST)
            try:
                n = int(n)
                pago = calculate_payment_for_term(monto, tasa, n, tipo_pago=tipo_pago)
            except Exception as e:
                return Response({'error': str(e)}, status=status.HTTP_400_BAD_REQUEST)
            return Response({
                'tipo_calculo': 'pago',
                'pago_mensual': str(pago),
                'plazo_meses': n,
                'tipo_pago': tipo_pago,
            })

        elif tipo_calculo == 'plazo':
            pago = request.data.get('pago_mensual')
            if not pago:
                return Response({'error': 'Se requiere pago_mensual.'}, status=status.HTTP_400_BAD_REQUEST)
            try:
                pago_dec = Decimal(str(pago))
                plazo = calculate_term_for_payment(monto, tasa, pago_dec, tipo_pago=tipo_pago)
            except ValueError as e:
                return Response({'error': str(e)}, status=status.HTTP_400_BAD_REQUEST)
            except Exception as e:
                return Response({'error': str(e)}, status=status.HTTP_400_BAD_REQUEST)
            return Response({
                'tipo_calculo': 'plazo',
                'plazo_meses': plazo,
                'pago_mensual': str(pago),
                'tipo_pago': tipo_pago,
            })

        return Response({'error': 'tipo_calculo debe ser "pago" o "plazo".'}, status=status.HTTP_400_BAD_REQUEST)


class MovimientoViewSet(viewsets.ModelViewSet):
    """
    CRUD de movimientos.
    GET    /api/movimientos/?prestamo={id} → filtrar por préstamo
    POST   /api/movimientos/               → crear movimiento
    PUT    /api/movimientos/{id}/          → editar
    DELETE /api/movimientos/{id}/          → borrar (recalcula saldo automáticamente)
    """
    queryset = Movimiento.objects.all().order_by('fecha')
    serializer_class = MovimientoSerializer

    def get_queryset(self):
        # Solo movimientos de préstamos propiedad del usuario autenticado.
        qs = Movimiento.objects.filter(prestamo__owner=self.request.user).order_by('fecha')
        prestamo_id = self.request.query_params.get('prestamo')
        if prestamo_id:
            qs = qs.filter(prestamo_id=prestamo_id)
        return qs

    def perform_create(self, serializer):
        # Impide crear movimientos sobre préstamos ajenos.
        prestamo = serializer.validated_data.get('prestamo')
        if prestamo is None or prestamo.owner_id != self.request.user.id:
            raise PermissionDenied('No puede registrar movimientos en este préstamo.')
        serializer.save()

    def perform_destroy(self, instance):
        prestamo = instance.prestamo
        instance.delete()
        prestamo.actualizar_saldo()
