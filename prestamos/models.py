"""
Prestamos models.

Core business models:
- Cliente
- Prestamo (con dos modos: fixed_term / fixed_payment y frecuencia mensual/semanal)
- Movimiento (pagos, incrementos de capital, y cargos automáticos de interés)

La lógica de amortización pura vive en prestamos/calculator.py.
actualizar_saldo() es intencionalmente stateful (recalcula y persiste cargos de mora).
"""

from django.conf import settings
from django.db import models
from decimal import Decimal
import datetime  # Import corregido para datetime.date.today
from dateutil.relativedelta import relativedelta
from django.utils import timezone


class Cliente(models.Model):
    """
    Modelo para representar a un cliente que solicita un préstamo.
    """
    owner = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        null=True,
        blank=True,
        related_name='clientes',
        help_text='Usuario propietario. Aísla la PII del cliente entre cuentas.'
    )
    nombre = models.CharField(max_length=100)
    telefono = models.CharField(max_length=15, blank=True)

    def __str__(self):
        return self.nombre

class Prestamo(models.Model):
    owner = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        null=True,
        blank=True,
        related_name='prestamos',
        help_text='Usuario propietario del préstamo. Aísla los datos entre cuentas.'
    )
    cliente = models.ForeignKey(Cliente, on_delete=models.CASCADE, null=True,
                                blank=True)  # Relación opcional con Cliente
    nombre_cliente = models.CharField(max_length=200,
                                      default='Cliente Anónimo')  # Campo agregado/corrección con default para migración
    telefono = models.CharField(max_length=20, blank=True)
    monto_original = models.DecimalField(max_digits=15, decimal_places=2)
    tasa_interes_anual = models.DecimalField(max_digits=5, decimal_places=2)
    tipo_pago = models.CharField(max_length=20, default='mensual')
    fecha_inicio = models.DateField(default=datetime.date.today)
    saldo_actual = models.DecimalField(max_digits=15, decimal_places=2, default=0)
    pago_mensual = models.DecimalField(max_digits=15, decimal_places=2, null=True, blank=True)
    plazo_meses = models.IntegerField(null=True, blank=True)
    activo = models.BooleanField(default=True)
    ultimo_pago = models.DateField(null=True, blank=True)
    modo = models.CharField(
        max_length=20,
        choices=[('fixed_term', 'Fixed Term'), ('fixed_payment', 'Fixed Payment')],
        default='fixed_payment'
    )
    def __str__(self):
        return f'Préstamo de {self.nombre_cliente}'

    def save(self, *args, **kwargs):
        if not self.pk and not self.saldo_actual:
            self.saldo_actual = self.monto_original
        super().save(*args, **kwargs)

    def actualizar_saldo(self, fecha_actual=None):
        """
        Recalcula el saldo_actual del préstamo hasta 'fecha_actual' (o hoy).

        Comportamiento:
        - Simula período por período (mensual o semanal según tipo_pago).
        - Aplica todos los Movimientos (pagos, incrementos, cargos previos) en orden.
        - Si un período completo transcurrió SIN ningún pago, genera automáticamente
          un Movimiento de tipo 'interes_cargo' (evita duplicados).
        - Actualiza saldo_actual, activo (si saldo <= 0), y ultimo_pago.
        - Persiste los cambios (y los nuevos cargos de interés).

        Este método tiene side-effects intencionales (crea registros de interés
        y hace save). Se llama desde vistas de lista/detalle y acciones de pago.
        """
        if not self.activo:
            return self.saldo_actual

        if fecha_actual is None:
            fecha_actual = datetime.date.today()

        from .calculator import get_period_rate_and_delta

        balance = Decimal(self.monto_original)
        fecha_periodo_start = self.fecha_inicio

        delta, tasa_periodo = get_period_rate_and_delta(
            self.tasa_interes_anual, self.tipo_pago
        )

        movimientos = list(self.movimientos.order_by('fecha'))

        # Pre-cargar para evitar queries N+1 al decidir si crear cargo
        fechas_cargo_existentes = set(
            self.movimientos.filter(tipo='interes_cargo').values_list('fecha', flat=True)
        )

        mov_index = 0
        num_mov = len(movimientos)

        # 1) Avanzar período por período hasta la fecha objetivo
        while fecha_periodo_start < fecha_actual:
            fecha_esperada = fecha_periodo_start + delta
            pago_en_periodo = False

            # Aplicar movimientos ocurridos en este período
            while (mov_index < num_mov and
                   movimientos[mov_index].fecha <= fecha_esperada and
                   movimientos[mov_index].fecha > fecha_periodo_start):
                mov = movimientos[mov_index]
                if mov.tipo == 'pago':
                    balance -= mov.monto
                    pago_en_periodo = True
                elif mov.tipo == 'incremento_capital':
                    balance += mov.monto
                elif mov.tipo == 'interes_cargo':
                    balance += mov.monto
                mov_index += 1

            # Cargo automático de interés si el período se completó sin pagos
            if fecha_esperada <= fecha_actual and not pago_en_periodo:
                if fecha_esperada not in fechas_cargo_existentes:
                    intereses = balance * tasa_periodo
                    balance += intereses
                    Movimiento.objects.create(
                        prestamo=self,
                        fecha=fecha_esperada,
                        monto=intereses,
                        tipo='interes_cargo',
                        descripcion='Cargo por período no pagado'
                    )
                    fechas_cargo_existentes.add(fecha_esperada)

            fecha_periodo_start = fecha_esperada

        # 2) Aplicar cualquier movimiento restante hasta la fecha actual
        while mov_index < num_mov and movimientos[mov_index].fecha <= fecha_actual:
            mov = movimientos[mov_index]
            if mov.tipo == 'pago':
                balance -= mov.monto
            elif mov.tipo == 'incremento_capital':
                balance += mov.monto
            elif mov.tipo == 'interes_cargo':
                balance += mov.monto
            mov_index += 1

        # 3) Persistir estado final
        self.saldo_actual = max(balance, Decimal('0.00'))

        pagos = [
            mov.fecha for mov in movimientos
            if mov.tipo == 'pago' and mov.fecha <= fecha_actual
        ]
        self.ultimo_pago = max(pagos) if pagos else None

        if self.saldo_actual <= Decimal('0.00'):
            self.activo = False

        super().save()
        return self.saldo_actual

    def get_amortizacion(self):
        """Delegates to the centralized pure calculator (see prestamos/calculator.py)."""
        from .calculator import build_amortization_schedule
        return build_amortization_schedule(
            monto=self.monto_original,
            tasa_anual=self.tasa_interes_anual,
            modo=self.modo,
            tipo_pago=self.tipo_pago,
            plazo=self.plazo_meses,
            pago_fijo=self.pago_mensual,
            fecha_inicio=self.fecha_inicio,
        )

    def registrar_incremento(self, monto_incremento, fecha):
        if monto_incremento > 0:
            Movimiento.objects.create(
                prestamo=self,
                fecha=fecha,
                monto=Decimal(monto_incremento),
                tipo='incremento_capital',
                descripcion='Incremento de capital solicitado por cliente'
            )
            self.actualizar_saldo(fecha)

class RegistroAuditoria(models.Model):
    """Bitácora de acciones financieras: quién hizo qué y cuándo.

    Se escribe desde las vistas mediante `registrar_auditoria()`. El objeto
    afectado se guarda de forma laxa (modelo + id + descripción) para que el
    registro sobreviva aunque el objeto original se elimine (on_delete=SET_NULL
    en el usuario, sin FK dura al objeto)."""
    ACCIONES = [
        ('crear', 'Crear'),
        ('editar', 'Editar'),
        ('borrar', 'Borrar'),
        ('pago', 'Registrar pago'),
        ('incremento', 'Registrar incremento'),
    ]
    usuario = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True,
        related_name='auditorias'
    )
    usuario_nombre = models.CharField(max_length=150, blank=True)  # copia por si se borra el user
    accion = models.CharField(max_length=20, choices=ACCIONES)
    modelo = models.CharField(max_length=50)          # p.ej. 'Prestamo', 'Movimiento'
    objeto_id = models.IntegerField(null=True, blank=True)
    detalle = models.CharField(max_length=255, blank=True)
    fecha = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-fecha']

    def __str__(self):
        return f"[{self.fecha:%Y-%m-%d %H:%M}] {self.usuario_nombre} {self.accion} {self.modelo}#{self.objeto_id}"


def registrar_auditoria(user, accion, modelo, objeto_id=None, detalle=''):
    """Crea una entrada de auditoría de forma segura (nunca rompe la vista)."""
    try:
        RegistroAuditoria.objects.create(
            usuario=user if getattr(user, 'is_authenticated', False) else None,
            usuario_nombre=getattr(user, 'username', '') or '',
            accion=accion,
            modelo=modelo,
            objeto_id=objeto_id,
            detalle=str(detalle)[:255],
        )
    except Exception:  # la auditoría no debe tumbar la operación principal
        import logging
        logging.getLogger('prestamos').exception("No se pudo registrar auditoría")


class Movimiento(models.Model):
    prestamo = models.ForeignKey(Prestamo, on_delete=models.CASCADE, related_name='movimientos')
    fecha = models.DateField(default=datetime.date.today)  # Corregido con datetime
    monto = models.DecimalField(max_digits=15, decimal_places=2)
    tipo = models.CharField(
        max_length=20,
        choices=[('pago', 'Pago'), ('incremento_capital', 'Incremento de Capital'), ('interes_cargo', 'Cargo de Interés')]
    )
    descripcion = models.TextField(blank=True)

    def __str__(self):
        return f"{self.tipo.capitalize()} {self.id} - {self.monto} ({self.fecha})"

