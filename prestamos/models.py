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
from django.db import models, transaction
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
    """
    Una obligación con tabla de amortización. El campo `rol` distingue los dos
    sentidos del dinero sin duplicar la lógica financiera:

      - 'prestamo': dinero que yo presté y me deben (el caso original).
      - 'deuda':    dinero que yo debo por una compra a plazos (casa, terreno,
                    auto). Los movimientos de tipo 'pago' son entonces pagos
                    que yo realicé al acreedor.

    En ambos casos el saldo baja con los pagos y sube con los cargos de los
    períodos no cubiertos, así que `actualizar_saldo` y `get_amortizacion`
    sirven igual. Una deuda sin intereses es `tasa_interes_anual = 0`.
    """
    ROL_PRESTAMO = 'prestamo'
    ROL_DEUDA = 'deuda'
    ROL_CHOICES = [
        (ROL_PRESTAMO, 'Préstamo otorgado (me deben)'),
        (ROL_DEUDA, 'Deuda propia (yo debo)'),
    ]

    rol = models.CharField(max_length=10, choices=ROL_CHOICES, default=ROL_PRESTAMO)
    concepto = models.CharField(
        max_length=200, blank=True,
        help_text='Qué se compró o para qué fue el dinero. Ej: "Terreno en Misiones".'
    )
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
        etiqueta = 'Deuda con' if self.es_deuda else 'Préstamo de'
        if self.concepto:
            return f'{etiqueta} {self.nombre_cliente} — {self.concepto}'
        return f'{etiqueta} {self.nombre_cliente}'

    @property
    def es_deuda(self):
        return self.rol == self.ROL_DEUDA

    @property
    def titulo(self):
        """Nombre legible: el concepto si existe, si no la contraparte."""
        return self.concepto or self.nombre_cliente

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

        from .calculator import get_period_rate_and_delta, quantize_money

        with transaction.atomic():
            # DECISIÓN DE NEGOCIO (re-simulación retroactiva SIN perdones históricos):
            # se purgan TODOS los cargos de interés autogenerados y se regeneran desde
            # cero según la regla vigente. La simulación es la única fuente de verdad de
            # los interes_cargo (por eso ya no hay guarda de "fecha ya cargada").
            #
            # El interés de cada período se cobra sobre lo que FALTÓ por pagar, no sobre
            # la mensualidad completa: quien abona parte de su cuota sólo devenga interés
            # por el resto. El cargo se suma al capital, así que el saldo siguiente ya lo
            # incluye.
            self.movimientos.filter(tipo='interes_cargo').delete()

            balance = Decimal(self.monto_original)
            fecha_periodo_start = self.fecha_inicio

            delta, tasa_periodo = get_period_rate_and_delta(
                self.tasa_interes_anual, self.tipo_pago
            )

            # Movimientos reales tras la purga (pagos e incrementos; ya no hay interes_cargo).
            movimientos = list(self.movimientos.order_by('fecha'))

            mov_index = 0
            num_mov = len(movimientos)
            pago_minimo = self.pago_mensual or Decimal('0')

            # 0) Movimientos con fecha anterior o igual al inicio del préstamo.
            # Se aplican al balance pero no pertenecen a ningún período. Sin este
            # consumo previo el cursor se quedaba atascado en ellos —la condición
            # del bucle exige fecha > fecha_periodo_start—, de modo que ningún pago
            # posterior se contabilizaba y TODOS los períodos salían como no
            # cubiertos. Basta un movimiento con el año mal capturado para que un
            # préstamo al corriente devengue intereses en cada período.
            while mov_index < num_mov and movimientos[mov_index].fecha <= self.fecha_inicio:
                mov = movimientos[mov_index]
                if mov.tipo == 'pago':
                    balance -= mov.monto
                elif mov.tipo == 'incremento_capital':
                    balance += mov.monto
                mov_index += 1

            # 1) Avanzar período por período hasta la fecha objetivo
            while fecha_periodo_start < fecha_actual:
                fecha_esperada = fecha_periodo_start + delta
                suma_pagos_periodo = Decimal('0')

                # Aplicar movimientos ocurridos en este período, acumulando pagos
                while (mov_index < num_mov and
                       movimientos[mov_index].fecha <= fecha_esperada and
                       movimientos[mov_index].fecha > fecha_periodo_start):
                    mov = movimientos[mov_index]
                    if mov.tipo == 'pago':
                        balance -= mov.monto
                        suma_pagos_periodo += mov.monto
                    elif mov.tipo == 'incremento_capital':
                        balance += mov.monto
                    mov_index += 1

                # Interés sobre el faltante del período, no sobre la mensualidad
                # entera: si la cuota es 3,975 y se abonaron 3,000, el interés
                # corre sólo sobre los 975 restantes. Se suma al capital.
                # pago_minimo 0 (pago_mensual None/0) => faltante <= 0 => nunca cobra.
                if fecha_esperada <= fecha_actual:
                    faltante = pago_minimo - suma_pagos_periodo
                    intereses = quantize_money(faltante * tasa_periodo) if faltante > 0 else Decimal('0.00')
                    # Un cargo de 0 no aporta información y ensucia el historial:
                    # ocurre con tasa 0% o cuando el faltante redondea por debajo
                    # del centavo.
                    if intereses > 0:
                        balance += intereses
                        Movimiento.objects.create(
                            prestamo=self,
                            fecha=fecha_esperada,
                            monto=intereses,
                            tipo='interes_cargo',
                            descripcion=(
                                f'Interés sobre {faltante.quantize(Decimal("0.01"))} no cubierto'
                            ),
                        )

                fecha_periodo_start = fecha_esperada

            # 2) Aplicar cualquier movimiento restante hasta la fecha actual
            while mov_index < num_mov and movimientos[mov_index].fecha <= fecha_actual:
                mov = movimientos[mov_index]
                if mov.tipo == 'pago':
                    balance -= mov.monto
                elif mov.tipo == 'incremento_capital':
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

    def registrar_incremento(self, monto_incremento, fecha, descripcion=None):
        if monto_incremento > 0:
            Movimiento.objects.create(
                prestamo=self,
                fecha=fecha,
                monto=Decimal(monto_incremento),
                tipo='incremento_capital',
                descripcion=descripcion or (
                    'Cargo adicional a la deuda' if self.es_deuda
                    else 'Incremento de capital solicitado por cliente'
                ),
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


def prestamos_visibles(user):
    """Préstamos que el usuario puede ver: los suyos, o todos si es administrador."""
    qs = Prestamo.objects.all()
    return qs if getattr(user, 'is_superuser', False) else qs.filter(owner=user)


def movimientos_visibles(user):
    """Movimientos visibles: de sus préstamos, o de todos si es administrador."""
    qs = Movimiento.objects.all()
    return qs if getattr(user, 'is_superuser', False) else qs.filter(prestamo__owner=user)


def clientes_visibles(user):
    """Clientes visibles: los suyos, o todos si es administrador."""
    qs = Cliente.objects.all()
    return qs if getattr(user, 'is_superuser', False) else qs.filter(owner=user)


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



class Inversion(models.Model):
    """
    Una posición de inversión (CETES, un proyecto de Briq, un fondo…).

    No se conecta a ninguna plataforma: CETES y los instrumentos a tasa fija
    tienen rendimiento determinista desde la compra, así que con la operación
    registrada una vez se proyecta su valor a cualquier fecha. Los fondos no
    son proyectables y guardan su valor capturado a mano.

    La aritmética vive en prestamos/portafolio.py.
    """
    PLATAFORMA_CETESDIRECTO = 'cetesdirecto'
    PLATAFORMA_BRIQ = 'briq'
    PLATAFORMA_OTRA = 'otra'
    PLATAFORMA_CHOICES = [
        (PLATAFORMA_CETESDIRECTO, 'CetesDirecto'),
        (PLATAFORMA_BRIQ, 'Briq.mx'),
        (PLATAFORMA_OTRA, 'Otra'),
    ]

    TIPO_DESCUENTO = 'descuento'
    TIPO_TASA_FIJA = 'tasa_fija'
    TIPO_FONDO = 'fondo'
    TIPO_CHOICES = [
        (TIPO_DESCUENTO, 'A descuento (CETES, Bondes)'),
        (TIPO_TASA_FIJA, 'Tasa fija a plazo (Briq, pagarés)'),
        (TIPO_FONDO, 'Fondo de inversión (BONDDIA, ENERFIN)'),
    ]

    owner = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE,
        null=True, blank=True, related_name='inversiones',
        help_text='Usuario propietario. Aísla el portafolio entre cuentas.',
    )
    plataforma = models.CharField(max_length=20, choices=PLATAFORMA_CHOICES,
                                  default=PLATAFORMA_CETESDIRECTO)
    nombre = models.CharField(
        max_length=200,
        help_text='Ej: "CETES 28 días" o "Torre Guadalajara".',
    )
    tipo = models.CharField(max_length=20, choices=TIPO_CHOICES, default=TIPO_DESCUENTO)
    monto_invertido = models.DecimalField(max_digits=15, decimal_places=2)
    fecha_compra = models.DateField(default=datetime.date.today)
    tasa_anual = models.DecimalField(
        max_digits=6, decimal_places=3, default=Decimal('0'),
        help_text='Tasa nominal anual en %. No aplica a fondos.',
    )
    plazo_dias = models.IntegerField(
        default=0, help_text='Días al vencimiento. 0 en fondos (liquidez abierta).',
    )
    base_dias = models.IntegerField(
        default=360,
        help_text='360 para CETES (convención Banxico); 365 para el resto.',
    )
    valor_manual = models.DecimalField(
        max_digits=15, decimal_places=2, null=True, blank=True,
        help_text='Valor capturado. Obligatorio en fondos, que no se proyectan.',
    )
    fecha_valor = models.DateField(
        null=True, blank=True,
        help_text='Fecha a la que corresponde el valor capturado. Las aportaciones y '
                  'retiros posteriores se suman o restan sobre él. Por defecto, la de compra.',
    )
    activa = models.BooleanField(default=True)
    notas = models.TextField(blank=True)
    creada = models.DateTimeField(auto_now_add=True, null=True, blank=True)

    class Meta:
        ordering = ['-fecha_compra', '-id']

    def __str__(self):
        return f'{self.get_plataforma_display()} · {self.nombre}'

    @property
    def es_fondo(self):
        return self.tipo == self.TIPO_FONDO

    @property
    def fecha_vencimiento(self):
        """None en fondos: no vencen."""
        if self.es_fondo or not self.plazo_dias:
            return None
        return self.fecha_compra + datetime.timedelta(days=self.plazo_dias)

    def dias_devengados(self, hasta=None):
        from .portafolio import dias_transcurridos
        if self.es_fondo:
            return 0
        return dias_transcurridos(self.fecha_compra, hasta or datetime.date.today(),
                                  self.plazo_dias)

    def _suma(self, tipo):
        return sum((m.monto for m in self.movimientos.all() if m.tipo == tipo), Decimal('0.00'))

    @property
    def aportaciones(self):
        return self._suma(MovimientoInversion.TIPO_APORTACION)

    @property
    def retiros(self):
        return self._suma(MovimientoInversion.TIPO_RETIRO)

    @property
    def rendimientos_cobrados(self):
        return self._suma(MovimientoInversion.TIPO_RENDIMIENTO)

    @property
    def capital_invertido(self):
        """Dinero puesto de tu bolsillo: la compra inicial más aportaciones, menos retiros.

        Es distinto de `monto_invertido`, que sólo registra la operación de origen.
        """
        return self.monto_invertido + self.aportaciones - self.retiros

    def valor_estimado(self, hasta=None):
        """
        Valor de la posición a `hasta`, incluidas aportaciones y retiros.

        Con valor capturado (siempre en fondos, que no se proyectan) se parte de
        ese importe y se le aplican los movimientos POSTERIORES a `fecha_valor`:
        el corte ya refleja lo ocurrido hasta esa fecha, así que sumar de nuevo
        una aportación anterior la contaría dos veces.

        Sin valor capturado, cada aportación devenga por su cuenta desde su
        propia fecha: dinero que entró a mitad del plazo no puede rendir como si
        hubiera estado desde el inicio.
        """
        from .portafolio import valor_devengado, dias_transcurridos
        hasta = hasta or datetime.date.today()

        if self.valor_manual is not None:
            corte = self.fecha_valor or self.fecha_compra
            posteriores = Decimal('0.00')
            for mov in self.movimientos.all():
                if mov.fecha <= corte or mov.fecha > hasta:
                    continue
                if mov.tipo == MovimientoInversion.TIPO_APORTACION:
                    posteriores += mov.monto
                elif mov.tipo == MovimientoInversion.TIPO_RETIRO:
                    posteriores -= mov.monto
            return self.valor_manual + posteriores

        if self.es_fondo:
            # Sin captura no hay precio de mercado que valga; el capital es lo
            # único defendible.
            return self.capital_invertido

        valor = valor_devengado(self.monto_invertido, self.tasa_anual,
                                self.dias_devengados(hasta), self.base_dias)
        for mov in self.movimientos.all():
            if mov.fecha > hasta:
                continue
            if mov.tipo == MovimientoInversion.TIPO_APORTACION:
                dias = dias_transcurridos(mov.fecha, hasta, self.plazo_dias or 0)
                valor += valor_devengado(mov.monto, self.tasa_anual, dias, self.base_dias)
            elif mov.tipo == MovimientoInversion.TIPO_RETIRO:
                valor -= mov.monto
        return valor

    def valor_al_vencimiento(self):
        from .portafolio import valor_al_vencimiento
        if self.es_fondo or not self.plazo_dias:
            return None
        return valor_al_vencimiento(self.monto_invertido, self.tasa_anual,
                                    self.plazo_dias, self.base_dias)

    def rendimiento(self, hasta=None):
        """
        Ganancia sobre el capital realmente puesto, más lo ya cobrado.

        Se compara contra `capital_invertido` y no contra `monto_invertido`: una
        aportación es dinero tuyo, no ganancia, y restarla mal inflaría el
        rendimiento por el importe íntegro del aporte.
        """
        return self.valor_estimado(hasta) - self.capital_invertido + self.rendimientos_cobrados

    @property
    def vencida(self):
        vencimiento = self.fecha_vencimiento
        return bool(vencimiento and vencimiento <= datetime.date.today())

    def dias_para_vencer(self, hasta=None):
        vencimiento = self.fecha_vencimiento
        if not vencimiento:
            return None
        return (vencimiento - (hasta or datetime.date.today())).days


class MovimientoInversion(models.Model):
    """Aportaciones, retiros y rendimientos cobrados de una posición.

    Briq paga rendimientos periódicos que salen de la posición al bolsillo; sin
    registrarlos, el rendimiento total quedaría subestimado.
    """
    TIPO_APORTACION = 'aportacion'
    TIPO_RETIRO = 'retiro'
    TIPO_RENDIMIENTO = 'rendimiento'
    TIPO_CHOICES = [
        (TIPO_APORTACION, 'Aportación'),
        (TIPO_RETIRO, 'Retiro'),
        (TIPO_RENDIMIENTO, 'Rendimiento cobrado'),
    ]

    inversion = models.ForeignKey(Inversion, on_delete=models.CASCADE, related_name='movimientos')
    fecha = models.DateField(default=datetime.date.today)
    monto = models.DecimalField(max_digits=15, decimal_places=2)
    tipo = models.CharField(max_length=20, choices=TIPO_CHOICES)
    descripcion = models.CharField(max_length=200, blank=True)

    class Meta:
        ordering = ['fecha', 'id']

    def __str__(self):
        return f'{self.get_tipo_display()} {self.monto} ({self.fecha})'


def inversiones_visibles(user):
    """Posiciones visibles: las suyas, o todas si es administrador."""
    qs = Inversion.objects.all()
    return qs if getattr(user, 'is_superuser', False) else qs.filter(owner=user)
