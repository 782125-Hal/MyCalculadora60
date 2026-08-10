"""
Tests básicos para la lógica financiera central (Fase 1).

Cubre:
- Funciones puras en calculator.py
- Prestamo.get_amortizacion()
- Prestamo.actualizar_saldo() + generación de cargos automáticos de interés
- Modos: fixed_term / fixed_payment
- Frecuencias: mensual / semanal
- Pagos, incrementos y saldo cero → inactivación
"""

from decimal import Decimal, localcontext
from datetime import date, timedelta
from dateutil.relativedelta import relativedelta

from io import BytesIO, StringIO

from django.contrib.auth.models import User
from django.core.management import call_command
from django.core.management.base import CommandError
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from .models import (
    Cliente, Prestamo, Movimiento, RegistroAuditoria,
    Inversion, MovimientoInversion,
)
from .calculator import (
    calculate_payment_for_term,
    calculate_term_for_payment,
    build_amortization_schedule,
    quantize_money,
    quantize_payment,
)
from .importador import leer_tabla, procesar, a_fecha, a_decimal
from .portafolio import (
    precio_cete,
    valor_devengado,
    rendimiento_esperado,
    dias_transcurridos,
    tasa_efectiva_anual,
)


class CalculatorPureFunctionsTest(TestCase):
    """Pruebas de las funciones puras (sin DB)."""

    def test_calculate_payment_for_term_basic(self):
        """Préstamo clásico: 1M a 12% anual, 12 meses."""
        pago = calculate_payment_for_term(
            monto=Decimal('1000000'),
            tasa_anual=Decimal('12'),
            plazo=12,
            tipo_pago='mensual'
        )
        # Valor aproximado conocido ~ 88,848.79 (redondeado)
        self.assertGreater(pago, Decimal('88800'))
        self.assertLess(pago, Decimal('88900'))

    def test_calculate_payment_zero_rate(self):
        pago = calculate_payment_for_term(Decimal('120000'), Decimal('0'), 12)
        self.assertEqual(pago, Decimal('10000.00'))

    def test_calculate_term_for_payment_basic(self):
        plazo = calculate_term_for_payment(
            monto=Decimal('1000000'),
            tasa_anual=Decimal('12'),
            pago_deseado=Decimal('88849'),
            tipo_pago='mensual'
        )
        self.assertGreaterEqual(plazo, 11)
        self.assertLessEqual(plazo, 13)

    def test_calculate_term_insufficient_payment_raises(self):
        with self.assertRaises(ValueError):
            calculate_term_for_payment(
                monto=Decimal('100000'),
                tasa_anual=Decimal('24'),
                pago_deseado=Decimal('100'),  # menor que el interés del primer período
            )

    def test_build_amortization_fixed_term(self):
        tabla = build_amortization_schedule(
            monto=Decimal('100000'),
            tasa_anual=Decimal('12'),
            modo='fixed_term',
            tipo_pago='mensual',
            plazo=6,
            fecha_inicio=date(2025, 1, 1),
        )
        self.assertEqual(len(tabla), 6)
        self.assertEqual(tabla[0]['periodo'], 1)
        self.assertIn('pago', tabla[0])
        self.assertIn('interes', tabla[0])
        # Último saldo debe ser cercano a cero
        self.assertLessEqual(tabla[-1]['saldo'], 0.01)

    def test_build_amortization_fixed_payment(self):
        tabla = build_amortization_schedule(
            monto=Decimal('50000'),
            tasa_anual=Decimal('0'),
            modo='fixed_payment',
            tipo_pago='mensual',
            pago_fijo=Decimal('5000'),
        )
        self.assertEqual(len(tabla), 10)
        self.assertEqual(tabla[-1]['saldo'], 0.0)

    def test_build_amortization_semanal(self):
        tabla = build_amortization_schedule(
            monto=Decimal('10000'),
            tasa_anual=Decimal('12'),
            modo='fixed_term',
            tipo_pago='semanal',
            plazo=4,
        )
        self.assertEqual(len(tabla), 4)


class PrestamoAmortizacionTest(TestCase):
    """Pruebas que usan el modelo Prestamo.get_amortizacion()."""

    def setUp(self):
        self.cliente = Cliente.objects.create(nombre="Test Cliente")

    def test_get_amortizacion_fixed_term_mensual(self):
        prestamo = Prestamo.objects.create(
            cliente=self.cliente,
            nombre_cliente="Test",
            monto_original=Decimal('120000'),
            tasa_interes_anual=Decimal('0'),
            tipo_pago='mensual',
            modo='fixed_term',
            plazo_meses=12,
            pago_mensual=Decimal('10000'),
            saldo_actual=Decimal('120000'),
            fecha_inicio=date.today(),
        )
        tabla = prestamo.get_amortizacion()
        self.assertEqual(len(tabla), 12)
        self.assertAlmostEqual(tabla[-1]['saldo'], 0.0, places=2)

    def test_get_amortizacion_fixed_payment(self):
        prestamo = Prestamo.objects.create(
            cliente=self.cliente,
            nombre_cliente="Test2",
            monto_original=Decimal('36000'),
            tasa_interes_anual=Decimal('0'),
            tipo_pago='mensual',
            modo='fixed_payment',
            pago_mensual=Decimal('3000'),
            saldo_actual=Decimal('36000'),
            fecha_inicio=date.today(),
        )
        tabla = prestamo.get_amortizacion()
        self.assertGreater(len(tabla), 10)
        self.assertLessEqual(tabla[-1]['saldo'], 0.01)


class PrestamoActualizarSaldoTest(TestCase):
    """Pruebas del motor de saldo + cargos automáticos de interés."""

    def setUp(self):
        self.cliente = Cliente.objects.create(nombre="Saldo Test")
        self.hoy = timezone.now().date()

    def _crear_prestamo_simple(self, monto=Decimal('10000'), tasa=Decimal('12'), modo='fixed_payment', pago_fijo=Decimal('1000')):
        return Prestamo.objects.create(
            cliente=self.cliente,
            nombre_cliente="Test Saldo",
            monto_original=monto,
            tasa_interes_anual=tasa,
            tipo_pago='mensual',
            modo=modo,
            pago_mensual=pago_fijo if modo == 'fixed_payment' else None,
            plazo_meses=24 if modo == 'fixed_payment' else 12,
            saldo_actual=monto,
            fecha_inicio=self.hoy - timedelta(days=40),  # para que haya varios períodos
        )

    def test_actualizar_saldo_genera_intereses_si_no_hay_pagos(self):
        prestamo = self._crear_prestamo_simple()
        saldo_antes = prestamo.saldo_actual
        nuevo_saldo = prestamo.actualizar_saldo(self.hoy)
        self.assertGreater(nuevo_saldo, saldo_antes)  # se cargaron intereses

        # Debe haber creado al menos un movimiento de interes_cargo
        cargos = prestamo.movimientos.filter(tipo='interes_cargo')
        self.assertGreater(cargos.count(), 0)

    def test_actualizar_saldo_con_pago(self):
        prestamo = self._crear_prestamo_simple(monto=Decimal('5000'), pago_fijo=Decimal('2000'))
        # Registrar un pago
        Movimiento.objects.create(
            prestamo=prestamo,
            fecha=self.hoy - timedelta(days=5),
            monto=Decimal('1500'),
            tipo='pago',
            descripcion='Pago de prueba'
        )
        prestamo.actualizar_saldo(self.hoy)
        # El saldo debe haber bajado respecto al original + intereses
        self.assertLess(prestamo.saldo_actual, Decimal('5000') + Decimal('100'))  # algún interés pequeño

    def test_actualizar_saldo_incremento_capital(self):
        prestamo = self._crear_prestamo_simple(monto=Decimal('1000'))
        prestamo.registrar_incremento(Decimal('500'), self.hoy - timedelta(days=10))
        prestamo.actualizar_saldo(self.hoy)
        self.assertGreater(prestamo.saldo_actual, Decimal('1000'))  # subió por el incremento (más posible interés)

    def test_prestamo_se_desactiva_al_llegar_a_cero(self):
        prestamo = Prestamo.objects.create(
            cliente=self.cliente,
            nombre_cliente="Cero",
            monto_original=Decimal('1000'),
            tasa_interes_anual=Decimal('0'),
            tipo_pago='mensual',
            modo='fixed_payment',
            pago_mensual=Decimal('1000'),
            saldo_actual=Decimal('1000'),
            fecha_inicio=self.hoy,
        )
        Movimiento.objects.create(
            prestamo=prestamo,
            fecha=self.hoy,
            monto=Decimal('1000'),
            tipo='pago',
        )
        prestamo.actualizar_saldo(self.hoy + timedelta(days=1))
        self.assertFalse(prestamo.activo)
        self.assertLessEqual(prestamo.saldo_actual, Decimal('0.00'))

    def test_actualizar_saldo_semanal(self):
        prestamo = Prestamo.objects.create(
            cliente=self.cliente,
            nombre_cliente="Semanal",
            monto_original=Decimal('2000'),
            tasa_interes_anual=Decimal('0'),
            tipo_pago='semanal',
            modo='fixed_payment',
            pago_mensual=Decimal('500'),
            saldo_actual=Decimal('2000'),
            fecha_inicio=self.hoy - timedelta(weeks=3),
        )
        prestamo.actualizar_saldo(self.hoy)
        # Con tasa 0 y sin pagos, el saldo no debe haber crecido
        self.assertEqual(prestamo.saldo_actual, Decimal('2000.00'))

    # --- Nueva regla de negocio: interés = pago_mensual * tasa_periodo (plano) ---

    def test_interes_es_pago_mensual_por_tasa_plano(self):
        """(a) Un mes vencido sin pago cobra pago_mensual * tasa_periodo, no balance * tasa."""
        # monto grande para probar que el interés NO depende del balance.
        prestamo = Prestamo.objects.create(
            cliente=self.cliente, nombre_cliente="Plano 1",
            monto_original=Decimal('100000'), tasa_interes_anual=Decimal('12'),  # tasa_periodo mensual = 0.01
            tipo_pago='mensual', modo='fixed_payment', pago_mensual=Decimal('1000'),
            saldo_actual=Decimal('100000'), fecha_inicio=self.hoy - relativedelta(months=1),
        )
        prestamo.actualizar_saldo(self.hoy)
        cargos = prestamo.movimientos.filter(tipo='interes_cargo')
        self.assertEqual(cargos.count(), 1)
        # 1000 * (12%/12) = 10.00 — independiente del balance de 100000.
        self.assertEqual(cargos.first().monto, Decimal('10.00'))
        self.assertEqual(prestamo.saldo_actual, Decimal('100010.00'))

    def test_varios_meses_sin_pago_cobran_una_mensualidad_cada_uno(self):
        """(b) 5 meses vencidos → 5 cargos iguales (flat), sin acumular sobre el saldo."""
        prestamo = Prestamo.objects.create(
            cliente=self.cliente, nombre_cliente="Plano 5",
            monto_original=Decimal('100000'), tasa_interes_anual=Decimal('12'),
            tipo_pago='mensual', modo='fixed_payment', pago_mensual=Decimal('1000'),
            saldo_actual=Decimal('100000'), fecha_inicio=self.hoy - relativedelta(months=5),
        )
        prestamo.actualizar_saldo(self.hoy)
        cargos = list(prestamo.movimientos.filter(tipo='interes_cargo').order_by('fecha'))
        self.assertEqual(len(cargos), 5)
        # Cada cargo es exactamente una mensualidad de interés; todos iguales (no crece).
        for c in cargos:
            self.assertEqual(c.monto, Decimal('10.00'))
        # Saldo = principal + 5 * 10 (lineal, no compuesto).
        self.assertEqual(prestamo.saldo_actual, Decimal('100050.00'))

    def test_pago_mensual_cero_no_cobra_interes(self):
        """(c) pago_mensual = 0 → cargo 0; el saldo no crece por interés."""
        prestamo = Prestamo.objects.create(
            cliente=self.cliente, nombre_cliente="Cero PM",
            monto_original=Decimal('50000'), tasa_interes_anual=Decimal('15'),
            tipo_pago='mensual', modo='fixed_payment', pago_mensual=Decimal('0'),
            saldo_actual=Decimal('50000'), fecha_inicio=self.hoy - relativedelta(months=3),
        )
        prestamo.actualizar_saldo(self.hoy)
        total_cargos = sum((c.monto for c in prestamo.movimientos.filter(tipo='interes_cargo')), Decimal('0'))
        self.assertEqual(total_cargos, Decimal('0'))
        self.assertEqual(prestamo.saldo_actual, Decimal('50000.00'))  # sin interés

    def test_pago_mensual_none_no_lanza_y_cobra_cero(self):
        """(d) pago_mensual = None → cargo 0 sin excepción (regla literal, sin fallback)."""
        prestamo = Prestamo.objects.create(
            cliente=self.cliente, nombre_cliente="None PM",
            monto_original=Decimal('50000'), tasa_interes_anual=Decimal('15'),
            tipo_pago='mensual', modo='fixed_term', pago_mensual=None, plazo_meses=None,
            saldo_actual=Decimal('50000'), fecha_inicio=self.hoy - relativedelta(months=3),
        )
        prestamo.actualizar_saldo(self.hoy)  # no debe lanzar TypeError
        total_cargos = sum((c.monto for c in prestamo.movimientos.filter(tipo='interes_cargo')), Decimal('0'))
        self.assertEqual(total_cargos, Decimal('0'))
        self.assertEqual(prestamo.saldo_actual, Decimal('50000.00'))


class PrestamoInteresRetroactivoTest(TestCase):
    """Regla retroactiva: el interés corre sobre el faltante del período (cuota
    menos lo abonado), no sobre la cuota entera. Purga+regenera cargos
    (idempotente, sin perdones)."""

    def setUp(self):
        self.cliente = Cliente.objects.create(nombre="Retro Test")
        self.hoy = timezone.now().date()

    def _prestamo(self, pago_mensual=Decimal('10000'), tasa=Decimal('12'), meses=1):
        # tasa 12% mensual => tasa_periodo 0.01 => cargo pleno = pago_mensual * 0.01
        return Prestamo.objects.create(
            cliente=self.cliente, nombre_cliente="Retro",
            monto_original=Decimal('100000'), tasa_interes_anual=tasa,
            tipo_pago='mensual', modo='fixed_payment', pago_mensual=pago_mensual,
            saldo_actual=Decimal('100000'), fecha_inicio=self.hoy - relativedelta(months=meses),
        )

    def _pago(self, prestamo, monto, dias_atras=10):
        Movimiento.objects.create(
            prestamo=prestamo, fecha=self.hoy - timedelta(days=dias_atras),
            monto=Decimal(monto), tipo='pago', descripcion='pago test',
        )

    def test_a_pagos_cubren_mensualidad_sin_cargo(self):
        """(a) Un pago que iguala la mensualidad → sin cargo."""
        p = self._prestamo()
        self._pago(p, '10000')
        p.actualizar_saldo(self.hoy)
        self.assertEqual(p.movimientos.filter(tipo='interes_cargo').count(), 0)
        self.assertEqual(p.saldo_actual, Decimal('90000.00'))  # 100000 - 10000, sin interés

    def test_b_pago_parcial_genera_cargo_sobre_el_faltante(self):
        """(b) Pago parcial → interés sólo sobre lo que faltó, no sobre la cuota."""
        p = self._prestamo()
        self._pago(p, '3000')  # abona 3000 de 10000; faltan 7000
        p.actualizar_saldo(self.hoy)
        cargos = p.movimientos.filter(tipo='interes_cargo')
        self.assertEqual(cargos.count(), 1)
        self.assertEqual(cargos.first().monto, Decimal('70.00'))  # 7000 * 0.01
        self.assertEqual(p.saldo_actual, Decimal('97070.00'))  # 100000 - 3000 + 70

    def test_c_dos_pagos_que_suman_mensualidad_sin_cargo(self):
        """(c) Dos pagos en el mismo período que juntos cubren la mensualidad → sin cargo."""
        p = self._prestamo()
        self._pago(p, '5000', dias_atras=12)
        self._pago(p, '5000', dias_atras=8)  # 5000 + 5000 = 10000 >= mensualidad
        p.actualizar_saldo(self.hoy)
        self.assertEqual(p.movimientos.filter(tipo='interes_cargo').count(), 0)
        self.assertEqual(p.saldo_actual, Decimal('90000.00'))

    def test_d_pago_de_un_peso_genera_cargo(self):
        """(d) $1.00 NO es perdón: sigue habiendo cargo, sobre los 9,999 faltantes."""
        p = self._prestamo()
        self._pago(p, '1.00')
        p.actualizar_saldo(self.hoy)
        cargos = p.movimientos.filter(tipo='interes_cargo')
        self.assertEqual(cargos.count(), 1)
        self.assertEqual(cargos.first().monto, Decimal('99.99'))  # 9999 * 0.01
        self.assertEqual(p.saldo_actual, Decimal('100098.99'))  # 100000 - 1 + 99.99

    def test_f_idempotencia_dos_corridas(self):
        """(f) Dos corridas seguidas → mismo saldo y sin cargos duplicados (purga+regenera)."""
        p = self._prestamo(meses=3)  # 3 meses sin pago → 3 cargos plenos
        saldo_1 = p.actualizar_saldo(self.hoy)
        count_1 = p.movimientos.filter(tipo='interes_cargo').count()
        saldo_2 = p.actualizar_saldo(self.hoy)
        count_2 = p.movimientos.filter(tipo='interes_cargo').count()
        self.assertEqual(count_1, 3)
        self.assertEqual(count_2, 3)          # no se duplican
        self.assertEqual(saldo_1, saldo_2)    # idempotente
        self.assertEqual(p.saldo_actual, Decimal('100300.00'))  # 100000 + 3 * 100


class IntegrationSmokeTest(TestCase):
    """Prueba rápida de que todo el flujo de un préstamo funciona junto."""

    def test_full_flow_fixed_term(self):
        cliente = Cliente.objects.create(nombre="Integration")
        prestamo = Prestamo.objects.create(
            cliente=cliente,
            nombre_cliente="Integration",
            monto_original=Decimal('24000'),
            tasa_interes_anual=Decimal('0'),
            tipo_pago='mensual',
            modo='fixed_term',
            plazo_meses=12,
            saldo_actual=Decimal('24000'),
            fecha_inicio=date.today(),
        )
        # La tabla debe tener exactamente 12 renglones
        tabla = prestamo.get_amortizacion()
        self.assertEqual(len(tabla), 12)

        # Registrar dos pagos
        Movimiento.objects.create(prestamo=prestamo, fecha=date.today(), monto=Decimal('2000'), tipo='pago')
        Movimiento.objects.create(prestamo=prestamo, fecha=date.today() + timedelta(days=10), monto=Decimal('2000'), tipo='pago')

        prestamo.actualizar_saldo(date.today() + timedelta(days=40))
        self.assertLess(prestamo.saldo_actual, Decimal('24000'))


# ============================================================
# Fase 2: Tests de formularios nuevos y humo de autenticación
# ============================================================

from .forms import PagoForm, IncrementoForm, MovimientoForm, PrestamoEditForm


class Fase2FormsTest(TestCase):
    """Validación de los nuevos formularios introducidos en Fase 2."""

    def test_pago_form_valido(self):
        form = PagoForm({
            'monto': '1500.50',
            'fecha': '2025-06-01',
            'descripcion': 'Pago parcial'
        })
        self.assertTrue(form.is_valid())

    def test_pago_form_monto_invalido(self):
        form = PagoForm({'monto': '0', 'fecha': '2025-06-01'})
        self.assertFalse(form.is_valid())
        self.assertIn('monto', form.errors)

    def test_incremento_form_valido(self):
        form = IncrementoForm({
            'monto': '2500',
            'fecha': date.today().isoformat()
        })
        self.assertTrue(form.is_valid())

    def test_movimiento_form_valido(self):
        form = MovimientoForm({
            'monto': '800.00',
            'fecha': '2025-05-15',
            'descripcion': 'Ajuste'
        })
        self.assertTrue(form.is_valid())

    def test_prestamo_edit_form_valida_tasa_negativa(self):
        form = PrestamoEditForm({
            'monto_original': '100000',
            'tasa_interes_anual': '-5',
            'tipo_pago': 'mensual'
        })
        self.assertFalse(form.is_valid())


class Fase2AuthSmokeTest(TestCase):
    """Pruebas básicas de que las vistas ahora requieren autenticación."""

    def test_home_redirects_when_not_logged_in(self):
        response = self.client.get('/')
        self.assertEqual(response.status_code, 302)  # redirect a login
        self.assertIn('login', response['Location'])

    def test_lista_prestamos_requires_login(self):
        response = self.client.get('/prestamos/lista-prestamos/')
        self.assertEqual(response.status_code, 302)


class Fase3ExportsAndDashboardTest(TestCase):
    """Humo para dashboard (home) y exports (requieren login)."""

    def setUp(self):
        from django.contrib.auth.models import User
        self.user = User.objects.create_user('testuser', password='testpass')
        self.cliente = Cliente.objects.create(nombre="Export Test")
        self.prestamo = Prestamo.objects.create(
            owner=self.user,
            cliente=self.cliente,
            nombre_cliente="Export Test",
            monto_original=Decimal('50000'),
            tasa_interes_anual=Decimal('10'),
            tipo_pago='mensual',
            modo='fixed_payment',
            pago_mensual=Decimal('4500'),
            saldo_actual=Decimal('50000'),
            fecha_inicio=date.today(),
        )

    def test_dashboard_shows_kpis_when_logged_in(self):
        self.client.login(username='testuser', password='testpass')

        response = self.client.get('/')
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'Dashboard')
        # KPIs separados por rol: "Total Original" se dividió en prestado / adquirido.
        self.assertContains(response, 'Total Prestado')
        self.assertContains(response, 'Total Adquirido')

    def test_export_csv_requires_login(self):
        response = self.client.get('/prestamos/export/prestamos/')
        self.assertEqual(response.status_code, 302)  # redirect to login

    def test_export_prestamo_csv(self):
        self.client.login(username='testuser', password='testpass')

        response = self.client.get(f'/prestamos/prestamo/{self.prestamo.pk}/export/')
        self.assertEqual(response.status_code, 200)
        self.assertIn('text/csv', response['Content-Type'])
        self.assertIn(b'MOVIMIENTOS', response.content)

    def test_otro_usuario_no_ve_prestamo_ajeno(self):
        """Regresión de aislamiento (IDOR): un usuario no puede acceder al
        préstamo de otro; debe recibir 404, no 200."""
        from django.contrib.auth.models import User
        User.objects.create_user('intruso', password='testpass')
        self.client.login(username='intruso', password='testpass')

        # Detalle ajeno → 404
        self.assertEqual(
            self.client.get(f'/prestamos/prestamo/{self.prestamo.pk}/').status_code, 404
        )
        # Export ajeno → 404
        self.assertEqual(
            self.client.get(f'/prestamos/prestamo/{self.prestamo.pk}/export/').status_code, 404
        )
        # El dashboard del intruso no suma el monto del préstamo ajeno
        home = self.client.get('/')
        self.assertNotContains(home, '50,000')


class HardeningFixesTest(TestCase):
    """Cubre los fixes medios: crear_prestamo con Form y saneo CSV."""

    def setUp(self):
        from django.contrib.auth.models import User
        self.user = User.objects.create_user('operador', password='testpass')
        self.client.login(username='operador', password='testpass')
        self.cliente = Cliente.objects.create(owner=self.user, nombre="Cliente CSV")

    def test_crear_prestamo_funciona_con_form(self):
        """Regresión: antes la vista leía 'monto_original'/'plazo_periodos' que el
        template nunca enviaba (enviaba 'monto'/'periodos_totales')."""
        resp = self.client.post('/prestamos/crear-prestamo/', {
            'cliente': self.cliente.id,
            'monto': '25000',
            'tipo_pago': 'mensual',
            'fecha_inicio': date.today().isoformat(),
            'tasa_interes_anual': '10',
            'periodos_totales': '24',
        })
        self.assertEqual(resp.status_code, 302)  # redirige al detalle
        p = Prestamo.objects.get(cliente=self.cliente)
        self.assertEqual(p.monto_original, Decimal('25000'))
        self.assertEqual(p.plazo_meses, 24)
        self.assertEqual(p.owner, self.user)

    def test_crear_prestamo_rechaza_datos_invalidos(self):
        """Monto negativo y periodos 0 no deben crear nada."""
        resp = self.client.post('/prestamos/crear-prestamo/', {
            'cliente': self.cliente.id,
            'monto': '-5',
            'tipo_pago': 'mensual',
            'fecha_inicio': date.today().isoformat(),
            'tasa_interes_anual': '10',
            'periodos_totales': '0',
        })
        self.assertEqual(resp.status_code, 200)  # re-render con errores
        self.assertFalse(Prestamo.objects.filter(cliente=self.cliente).exists())

    def test_export_csv_neutraliza_formulas(self):
        """Un nombre que empieza con '=' debe salir prefijado con apóstrofo."""
        Prestamo.objects.create(
            owner=self.user,
            cliente=self.cliente,
            nombre_cliente='=HYPERLINK("http://evil")',
            monto_original=Decimal('1000'),
            tasa_interes_anual=Decimal('5'),
            tipo_pago='mensual',
            modo='fixed_payment',
            saldo_actual=Decimal('1000'),
            fecha_inicio=date.today(),
        )
        resp = self.client.get('/prestamos/export/prestamos/')
        self.assertEqual(resp.status_code, 200)
        self.assertIn(b"'=HYPERLINK", resp.content)   # neutralizado
        self.assertNotIn(b',=HYPERLINK', resp.content)  # no queda como fórmula activa


class ClienteAislamientoTest(TestCase):
    """Aislamiento de la PII de clientes por usuario (web y API)."""

    def setUp(self):
        from django.contrib.auth.models import User
        self.ana = User.objects.create_user('ana', password='x')
        self.beto = User.objects.create_user('beto', password='x')
        self.cliente_ana = Cliente.objects.create(owner=self.ana, nombre="Cliente de Ana", telefono="555-1")

    def test_crear_prestamo_no_lista_clientes_ajenos(self):
        self.client.login(username='beto', password='x')
        resp = self.client.get('/prestamos/crear-prestamo/')
        self.assertEqual(resp.status_code, 200)
        self.assertNotContains(resp, "Cliente de Ana")  # Beto no ve el cliente de Ana

    def test_no_se_puede_crear_prestamo_con_cliente_ajeno(self):
        self.client.login(username='beto', password='x')
        resp = self.client.post('/prestamos/crear-prestamo/', {
            'cliente': self.cliente_ana.id,   # cliente de Ana
            'monto': '1000',
            'tipo_pago': 'mensual',
            'fecha_inicio': date.today().isoformat(),
            'tasa_interes_anual': '10',
            'periodos_totales': '12',
        })
        self.assertEqual(resp.status_code, 200)  # rechazado, no redirige
        self.assertFalse(Prestamo.objects.filter(cliente=self.cliente_ana).exists())

    def test_api_clientes_solo_devuelve_propios(self):
        Cliente.objects.create(owner=self.beto, nombre="Cliente de Beto")
        self.client.login(username='beto', password='x')
        resp = self.client.get('/api/clientes/')
        self.assertEqual(resp.status_code, 200)
        nombres = [c['nombre'] for c in resp.json()['results']]
        self.assertIn("Cliente de Beto", nombres)
        self.assertNotIn("Cliente de Ana", nombres)


class ApiPrestamoAislamientoTest(TestCase):
    """La API DRF de préstamos respeta el owner."""

    def setUp(self):
        from django.contrib.auth.models import User
        self.ana = User.objects.create_user('ana', password='x')
        self.beto = User.objects.create_user('beto', password='x')
        self.prestamo_ana = Prestamo.objects.create(
            owner=self.ana, nombre_cliente="Ana", monto_original=Decimal('1000'),
            tasa_interes_anual=Decimal('5'), tipo_pago='mensual', modo='fixed_payment',
            saldo_actual=Decimal('1000'), fecha_inicio=date.today(),
        )

    def test_lista_api_no_incluye_ajenos(self):
        self.client.login(username='beto', password='x')
        resp = self.client.get('/api/prestamos/')
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()['count'], 0)

    def test_detalle_api_ajeno_es_404(self):
        self.client.login(username='beto', password='x')
        resp = self.client.get(f'/api/prestamos/{self.prestamo_ana.id}/')
        self.assertEqual(resp.status_code, 404)

    def test_api_requiere_autenticacion(self):
        resp = self.client.get('/api/prestamos/')
        self.assertIn(resp.status_code, (401, 403))


class AdminVeTodoTest(TestCase):
    """Un superusuario ve los préstamos de todos (web y API); el aislamiento
    sigue aplicando a usuarios normales."""

    def setUp(self):
        from django.contrib.auth.models import User
        self.dueno = User.objects.create_user('dueno', password='x')
        self.admin = User.objects.create_superuser('jefe', 'jefe@x.com', 'x')
        self.prestamo = Prestamo.objects.create(
            owner=self.dueno, nombre_cliente="Cliente de Dueño",
            monto_original=Decimal('7777'), tasa_interes_anual=Decimal('5'),
            tipo_pago='mensual', modo='fixed_payment', saldo_actual=Decimal('7777'),
            fecha_inicio=date.today(),
        )

    def test_admin_ve_prestamo_ajeno_en_web(self):
        self.client.login(username='jefe', password='x')
        # Detalle de un préstamo que no es suyo → 200 (no 404)
        self.assertEqual(
            self.client.get(f'/prestamos/prestamo/{self.prestamo.id}/').status_code, 200
        )
        # Aparece en el listado
        lista = self.client.get('/prestamos/lista-prestamos/')
        self.assertContains(lista, "Cliente de Dueño")

    def test_admin_ve_prestamo_ajeno_en_api(self):
        self.client.login(username='jefe', password='x')
        resp = self.client.get('/api/prestamos/')
        self.assertEqual(resp.json()['count'], 1)

    def test_usuario_normal_sigue_aislado(self):
        # Un tercer usuario normal NO ve el préstamo de 'dueno'
        from django.contrib.auth.models import User
        User.objects.create_user('otro', password='x')
        self.client.login(username='otro', password='x')
        self.assertEqual(
            self.client.get(f'/prestamos/prestamo/{self.prestamo.id}/').status_code, 404
        )


class AuditoriaTest(TestCase):
    """Las acciones financieras dejan rastro en RegistroAuditoria."""

    def setUp(self):
        from django.contrib.auth.models import User
        self.user = User.objects.create_user('operador', password='x')
        self.client.login(username='operador', password='x')
        self.cliente = Cliente.objects.create(owner=self.user, nombre="Cliente Aud")
        self.prestamo = Prestamo.objects.create(
            owner=self.user, cliente=self.cliente, nombre_cliente="Cliente Aud",
            monto_original=Decimal('10000'), tasa_interes_anual=Decimal('5'),
            tipo_pago='mensual', modo='fixed_payment', saldo_actual=Decimal('10000'),
            fecha_inicio=date.today(),
        )

    def test_registrar_pago_genera_auditoria(self):
        self.client.post(f'/prestamos/prestamo/{self.prestamo.id}/registrar-pago/', {
            'monto': '500', 'fecha': date.today().isoformat(), 'descripcion': 'abono',
        })
        reg = RegistroAuditoria.objects.filter(accion='pago', objeto_id=self.prestamo.id).first()
        self.assertIsNotNone(reg)
        self.assertEqual(reg.usuario, self.user)
        self.assertEqual(reg.usuario_nombre, 'operador')

    def test_borrar_prestamo_genera_auditoria(self):
        pid = self.prestamo.id
        self.client.post(f'/prestamos/prestamo/{pid}/borrar/')
        self.assertTrue(
            RegistroAuditoria.objects.filter(accion='borrar', modelo='Prestamo', objeto_id=pid).exists()
        )


class RegistrarPrestamoViewTests(TestCase):
    """Alta manual de préstamos y deudas propias."""

    @classmethod
    def setUpTestData(cls):
        cls.user = User.objects.create_user(username='alta_tester', password='pw')

    def setUp(self):
        self.client.login(username='alta_tester', password='pw')

    def _datos(self, **overrides):
        datos = {
            'rol': 'prestamo',
            'concepto': '',
            'nombre': 'Juan Pérez',
            'telefono': '555-1234',
            'monto_original': '100000',
            'tasa_interes_anual': '12',
            'tipo_pago': 'mensual',
            'fecha_inicio': '2025-01-01',
            'modo': 'fixed_term',
            'plazo_meses': '24',
            'pago_mensual': '',
        }
        datos.update(overrides)
        return datos

    def test_alta_plazo_fijo_calcula_el_pago(self):
        """En modo Plazo Fijo la cuota se deriva. Antes quedaba en None, y como
        actualizar_saldo() cobra pago_mensual x tasa, el préstamo nunca generaba
        intereses."""
        response = self.client.post(reverse('prestamos:registrar_prestamo'), self._datos())
        self.assertEqual(response.status_code, 302)
        prestamo = Prestamo.objects.get(nombre_cliente='Juan Pérez')
        self.assertEqual(prestamo.plazo_meses, 24)
        self.assertEqual(
            prestamo.pago_mensual,
            calculate_payment_for_term(Decimal('100000'), Decimal('12'), 24, 'mensual'),
        )

    def test_alta_pago_fijo_calcula_el_plazo(self):
        response = self.client.post(reverse('prestamos:registrar_prestamo'), self._datos(
            modo='fixed_payment', plazo_meses='', pago_mensual='4707.35',
        ))
        self.assertEqual(response.status_code, 302)
        prestamo = Prestamo.objects.get(nombre_cliente='Juan Pérez')
        self.assertEqual(prestamo.pago_mensual, Decimal('4707.35'))
        self.assertEqual(prestamo.plazo_meses, 24)

    def test_pago_fijo_que_no_cubre_intereses_se_registra_sin_plazo(self):
        """calculate_term_for_payment lanza ValueError; no debe romper el alta."""
        response = self.client.post(reverse('prestamos:registrar_prestamo'), self._datos(
            modo='fixed_payment', plazo_meses='', pago_mensual='1',
        ))
        self.assertEqual(response.status_code, 302)
        prestamo = Prestamo.objects.get(nombre_cliente='Juan Pérez')
        self.assertIsNone(prestamo.plazo_meses)

    def test_plazo_fijo_sin_plazo_no_registra(self):
        response = self.client.post(reverse('prestamos:registrar_prestamo'),
                                    self._datos(plazo_meses=''))
        self.assertEqual(response.status_code, 200)
        self.assertFalse(Prestamo.objects.filter(nombre_cliente='Juan Pérez').exists())
        self.assertIn('plazo_meses', response.context['form'].errors)

    def test_pago_fijo_con_cero_no_registra(self):
        """El 0 que antes venía prellenado no es un pago válido."""
        response = self.client.post(reverse('prestamos:registrar_prestamo'), self._datos(
            modo='fixed_payment', plazo_meses='', pago_mensual='0',
        ))
        self.assertEqual(response.status_code, 200)
        self.assertFalse(Prestamo.objects.filter(nombre_cliente='Juan Pérez').exists())
        self.assertIn('pago_mensual', response.context['form'].errors)

    def test_formulario_nuevo_no_prellena_pago_con_cero(self):
        response = self.client.get(reverse('prestamos:registrar_prestamo'))
        self.assertFalse(response.context['form'].initial.get('pago_mensual'))

    def test_rol_llega_desde_el_query_string(self):
        response = self.client.get(reverse('prestamos:registrar_prestamo'), {'rol': 'deuda'})
        self.assertEqual(response.context['form'].initial['rol'], 'deuda')

    def test_alta_asigna_owner(self):
        self.client.post(reverse('prestamos:registrar_prestamo'), self._datos())
        prestamo = Prestamo.objects.get(nombre_cliente='Juan Pérez')
        self.assertEqual(prestamo.owner, self.user)

    def test_alta_de_deuda_propia(self):
        response = self.client.post(reverse('prestamos:registrar_prestamo'), self._datos(
            rol='deuda', concepto='Terreno en Misiones', nombre='Inmobiliaria del Sur',
        ))
        self.assertEqual(response.status_code, 302)
        deuda = Prestamo.objects.get(concepto='Terreno en Misiones')
        self.assertTrue(deuda.es_deuda)
        self.assertEqual(deuda.rol, Prestamo.ROL_DEUDA)
        self.assertEqual(deuda.saldo_actual, Decimal('100000'))


class CalculadoraPrellenadoTests(TestCase):
    """La calculadora debe dejar sus resultados listos para el alta."""

    @classmethod
    def setUpTestData(cls):
        cls.user = User.objects.create_user(username='calc_tester', password='pw')

    def setUp(self):
        self.client.login(username='calc_tester', password='pw')

    def test_calculo_deja_datos_en_sesion(self):
        self.client.post(reverse('prestamos:calculadora_financiera'), {
            'monto': '100000', 'tasa': '12', 'tipo_calculo': 'pago', 'plazo_meses': '24',
        })
        datos = self.client.session['calculadora_data']
        self.assertEqual(datos['plazo_meses'], 24)
        self.assertEqual(datos['modo'], 'fixed_term')

    def test_registro_llega_prellenado_tras_calcular(self):
        self.client.post(reverse('prestamos:calculadora_financiera'), {
            'monto': '100000', 'tasa': '12', 'tipo_calculo': 'pago', 'plazo_meses': '24',
        })
        response = self.client.get(reverse('prestamos:registrar_prestamo'))
        self.assertTrue(response.context['viene_de_calculadora'])
        self.assertEqual(response.context['form'].initial['plazo_meses'], 24)


class DeudaPropiaTests(TestCase):
    """Registro de pagos sobre una deuda propia (casa, terreno)."""

    @classmethod
    def setUpTestData(cls):
        cls.user = User.objects.create_user(username='deuda_tester', password='pw')
        cls.deuda = Prestamo.objects.create(
            owner=cls.user,
            rol=Prestamo.ROL_DEUDA,
            concepto='Casa en Cuernavaca',
            nombre_cliente='Banco Hipotecario',
            monto_original=Decimal('500000'),
            tasa_interes_anual=Decimal('0'),
            tipo_pago='mensual',
            fecha_inicio=date(2025, 1, 1),
            saldo_actual=Decimal('500000'),
            modo='fixed_payment',
            pago_mensual=Decimal('10000'),
        )

    def setUp(self):
        self.client.login(username='deuda_tester', password='pw')

    def test_pago_reduce_el_saldo_de_la_deuda(self):
        self.client.post(
            reverse('prestamos:registrar_pago', args=[self.deuda.id]),
            {'monto': '10000', 'fecha': '2025-02-01', 'descripcion': 'Mensualidad febrero'},
        )
        self.deuda.refresh_from_db()
        self.assertEqual(self.deuda.saldo_actual, Decimal('490000.00'))
        self.assertEqual(self.deuda.movimientos.filter(tipo='pago').count(), 1)

    def test_cargo_adicional_conserva_la_descripcion(self):
        self.client.post(
            reverse('prestamos:registrar_incremento', args=[self.deuda.id]),
            {'monto': '5000', 'fecha': '2025-02-01', 'descripcion': 'Comisión por apertura'},
        )
        movimiento = self.deuda.movimientos.get(tipo='incremento_capital')
        self.assertEqual(movimiento.descripcion, 'Comisión por apertura')

    def test_detalle_suma_lo_pagado(self):
        for fecha in ('2025-02-01', '2025-03-01'):
            self.client.post(reverse('prestamos:registrar_pago', args=[self.deuda.id]),
                             {'monto': '10000', 'fecha': fecha})
        response = self.client.get(reverse('prestamos:detalle_prestamo', args=[self.deuda.id]))
        self.assertEqual(response.context['total_pagado'], Decimal('20000.00'))

    def test_filtro_por_rol_separa_deudas_de_prestamos(self):
        Prestamo.objects.create(
            owner=self.user,
            nombre_cliente='Cliente que me debe',
            monto_original=Decimal('1000'), tasa_interes_anual=Decimal('10'),
            tipo_pago='mensual', fecha_inicio=date(2025, 1, 1), saldo_actual=Decimal('1000'),
        )
        deudas = self.client.get(reverse('prestamos:lista_prestamos'), {'rol': 'deuda'})
        prestamos = self.client.get(reverse('prestamos:lista_prestamos'), {'rol': 'prestamo'})
        self.assertEqual(len(deudas.context['prestamos']), 1)
        self.assertEqual(len(prestamos.context['prestamos']), 1)
        self.assertTrue(deudas.context['es_vista_deudas'])

    def test_busqueda_por_concepto(self):
        response = self.client.get(reverse('prestamos:lista_prestamos'), {'q': 'Cuernavaca'})
        self.assertEqual(len(response.context['prestamos']), 1)

    def test_dashboard_separa_lo_que_debo_de_lo_que_me_deben(self):
        Prestamo.objects.create(
            owner=self.user,
            nombre_cliente='Cliente que me debe',
            monto_original=Decimal('1000'), tasa_interes_anual=Decimal('0'),
            tipo_pago='mensual', fecha_inicio=date(2025, 1, 1), saldo_actual=Decimal('1000'),
        )
        response = self.client.get(reverse('prestamos:home'))
        self.assertEqual(response.context['total_original'], Decimal('1000'))
        self.assertEqual(response.context['total_deuda_original'], Decimal('500000'))

    def test_deuda_ajena_no_es_visible(self):
        """El aislamiento por owner también aplica a las deudas."""
        otro = User.objects.create_user(username='ajeno', password='pw')
        self.client.force_login(otro)
        response = self.client.get(reverse('prestamos:detalle_prestamo', args=[self.deuda.id]))
        self.assertEqual(response.status_code, 404)


class ListaPaginacionTests(TestCase):
    """Paginación, filtros y orden en la lista.

    Lo importante no es sólo el corte visual: actualizar_saldo() purga y
    regenera los cargos de interés de cada préstamo, así que recorrer el
    queryset entero en un GET significaba cientos de escrituras.
    """

    @classmethod
    def setUpTestData(cls):
        cls.user = User.objects.create_user(username='pag_tester', password='pw')
        for i in range(25):
            Prestamo.objects.create(
                owner=cls.user,
                rol=Prestamo.ROL_DEUDA if i % 5 == 0 else Prestamo.ROL_PRESTAMO,
                nombre_cliente=f'Cliente {i:02d}',
                telefono=f'555-{i:04d}',
                monto_original=Decimal('1000') + i,
                tasa_interes_anual=Decimal('10'),
                tipo_pago='mensual',
                fecha_inicio=date(2025, 1, 1),
                saldo_actual=Decimal('1000') + i,
                activo=(i % 2 == 0),
                modo='fixed_payment',
                pago_mensual=Decimal('100'),
            )

    def setUp(self):
        self.client.login(username='pag_tester', password='pw')

    def test_primera_pagina_muestra_20(self):
        response = self.client.get(reverse('prestamos:lista_prestamos'))
        self.assertEqual(len(response.context['prestamos']), 20)
        self.assertEqual(response.context['total'], 25)

    def test_segunda_pagina_muestra_el_resto(self):
        response = self.client.get(reverse('prestamos:lista_prestamos'), {'page': 2})
        self.assertEqual(len(response.context['prestamos']), 5)

    def test_solo_se_recalcula_la_pagina_visible(self):
        """Pedir la página 1 no debe escribir sobre los préstamos de la página 2.

        Se marca un préstamo de la página 2 con un saldo imposible: si la vista
        recorriera el queryset completo, actualizar_saldo() lo corregiría.
        """
        # Con orden=monto_desc la página 1 son los 20 montos mayores; el de
        # monto 1000 (i=0) cae en la página 2.
        rezagado = Prestamo.objects.get(monto_original=Decimal('1000'))
        Prestamo.objects.filter(pk=rezagado.pk).update(saldo_actual=Decimal('99999.99'))

        self.client.get(reverse('prestamos:lista_prestamos'), {'orden': 'monto_desc'})

        rezagado.refresh_from_db()
        self.assertEqual(rezagado.saldo_actual, Decimal('99999.99'))

        # Y al pedir la página 2 sí se recalcula.
        self.client.get(reverse('prestamos:lista_prestamos'), {'orden': 'monto_desc', 'page': 2})
        rezagado.refresh_from_db()
        self.assertNotEqual(rezagado.saldo_actual, Decimal('99999.99'))

    def test_filtro_estado_activos(self):
        response = self.client.get(reverse('prestamos:lista_prestamos'), {'estado': 'activos'})
        self.assertEqual(response.context['total'], 13)  # i par: 0,2,...,24

    def test_filtro_rol_y_estado_combinados(self):
        response = self.client.get(reverse('prestamos:lista_prestamos'),
                                   {'rol': 'deuda', 'estado': 'activos'})
        # i múltiplo de 5 → deudas: 0,5,10,15,20. De esos, activos (par): 0,10,20
        self.assertEqual(response.context['total'], 3)

    def test_orden_por_monto(self):
        response = self.client.get(reverse('prestamos:lista_prestamos'), {'orden': 'monto_desc'})
        montos = [p.monto_original for p in response.context['prestamos']]
        self.assertEqual(montos, sorted(montos, reverse=True))

    def test_orden_invalido_cae_al_default(self):
        response = self.client.get(reverse('prestamos:lista_prestamos'), {'orden': '../../etc/passwd'})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(response.context['prestamos']), 20)

    def test_pagina_invalida_no_rompe(self):
        response = self.client.get(reverse('prestamos:lista_prestamos'), {'page': 'abc'})
        self.assertEqual(response.status_code, 200)

    def test_suma_de_saldos_es_solo_de_la_pagina(self):
        response = self.client.get(reverse('prestamos:lista_prestamos'))
        esperado = sum(p.saldo_actual for p in response.context['prestamos'])
        self.assertEqual(response.context['suma_saldos'], esperado)

    def test_lista_no_muestra_prestamos_ajenos(self):
        otro = User.objects.create_user(username='otro_pag', password='pw')
        self.client.force_login(otro)
        response = self.client.get(reverse('prestamos:lista_prestamos'))
        self.assertEqual(response.context['total'], 0)


class CerrarPeriodosCommandTests(TestCase):
    """Management command para cerrar períodos sin depender de visitas web."""

    def setUp(self):
        self.hoy = timezone.now().date()
        self.prestamo = Prestamo.objects.create(
            nombre_cliente='Deudor',
            monto_original=Decimal('10000'),
            tasa_interes_anual=Decimal('12'),
            tipo_pago='mensual',
            modo='fixed_payment',
            pago_mensual=Decimal('1000'),
            saldo_actual=Decimal('10000'),
            fecha_inicio=self.hoy - timedelta(days=90),
        )

    def _run(self, **kwargs):
        salida = StringIO()
        call_command('cerrar_periodos', stdout=salida, stderr=StringIO(), **kwargs)
        return salida.getvalue()

    def test_genera_cargos_sin_visitar_ninguna_pagina(self):
        self.assertEqual(self.prestamo.movimientos.filter(tipo='interes_cargo').count(), 0)
        self._run()
        self.assertGreater(self.prestamo.movimientos.filter(tipo='interes_cargo').count(), 0)

    def test_es_idempotente(self):
        self._run()
        cargos_1 = self.prestamo.movimientos.filter(tipo='interes_cargo').count()
        self.prestamo.refresh_from_db()
        saldo_1 = self.prestamo.saldo_actual

        self._run()
        cargos_2 = self.prestamo.movimientos.filter(tipo='interes_cargo').count()
        self.prestamo.refresh_from_db()

        self.assertEqual(cargos_1, cargos_2)
        self.assertEqual(saldo_1, self.prestamo.saldo_actual)

    def test_fecha_invalida_es_error_claro(self):
        with self.assertRaises(CommandError):
            call_command('cerrar_periodos', hasta='31-12-2025', stdout=StringIO())

    def test_hasta_acota_los_cargos(self):
        salida = self._run(hasta=(self.hoy - timedelta(days=60)).isoformat())
        self.assertIn('Revisados 1', salida)
        cargos_acotado = self.prestamo.movimientos.filter(tipo='interes_cargo').count()
        self._run()
        self.assertGreater(
            self.prestamo.movimientos.filter(tipo='interes_cargo').count(), cargos_acotado
        )

    def test_filtro_por_rol(self):
        Prestamo.objects.create(
            rol=Prestamo.ROL_DEUDA,
            nombre_cliente='Banco',
            monto_original=Decimal('5000'),
            tasa_interes_anual=Decimal('12'),
            tipo_pago='mensual',
            modo='fixed_payment',
            pago_mensual=Decimal('500'),
            saldo_actual=Decimal('5000'),
            fecha_inicio=self.hoy - timedelta(days=90),
        )
        self.assertIn('Revisados 1', self._run(rol='deuda'))
        self.assertIn('Revisados 2', self._run())

    def test_omite_inactivos(self):
        Prestamo.objects.filter(pk=self.prestamo.pk).update(activo=False)
        self.assertIn('Revisados 0', self._run())


class RedondeoDeCuotaTests(TestCase):
    """La cuota se redondea hacia arriba (ROUND_UP), no al más cercano.

    Redondear la cuota hacia abajo la deja insuficiente: tras n períodos faltan
    centavos y el préstamo no liquida dentro del plazo pactado.
    """

    # Casos verificados donde la fracción de centavo cae por debajo de 0.005:
    # ROUND_HALF_UP redondearía hacia ABAJO y ROUND_UP hacia arriba, así que
    # distinguen de verdad los dos modos. Un caso que redondee igual en ambos
    # no probaría nada sobre este cambio.
    CASOS = [
        (Decimal('37500'), Decimal('18'), 8),    # exacta 5009.4009 → 5009.40 vs 5009.41
        (Decimal('37500'), Decimal('18'), 13),   # exacta 3196.5134 → 3196.51 vs 3196.52
        (Decimal('37500'), Decimal('18'), 23),   # exacta 1939.9032 → 1939.90 vs 1939.91
        (Decimal('37500'), Decimal('18'), 16),   # exacta 2653.6904 → 2653.69 vs 2653.70
    ]

    def _cuota_exacta(self, monto, tasa, plazo):
        """Cuota sin redondear, a 50 dígitos, para comparar contra la redondeada."""
        with localcontext() as ctx:
            ctx.prec = 50
            r = Decimal(tasa) / Decimal('100') / Decimal('12')
            tmp = (Decimal(1) + r) ** plazo
            return Decimal(monto) * r * tmp / (tmp - Decimal(1))

    def test_la_cuota_nunca_queda_por_debajo_de_la_exacta(self):
        """Esta es la propiedad de ROUND_UP; con ROUND_HALF_UP fallaría."""
        for monto, tasa, plazo in self.CASOS:
            with self.subTest(monto=monto, tasa=tasa, plazo=plazo):
                cuota = calculate_payment_for_term(monto, tasa, plazo)
                exacta = self._cuota_exacta(monto, tasa, plazo)
                self.assertGreaterEqual(cuota, exacta)
                # Y no se pasa de un centavo: sigue siendo el redondeo mínimo suficiente.
                self.assertLess(cuota - exacta, Decimal('0.01'))

    def test_la_tabla_liquida_dentro_del_plazo(self):
        """Con la cuota calculada, el saldo llega a 0 en el plazo pactado."""
        for monto, tasa, plazo in self.CASOS:
            with self.subTest(monto=monto, tasa=tasa, plazo=plazo):
                tabla = build_amortization_schedule(
                    monto=monto, tasa_anual=tasa, modo='fixed_term',
                    tipo_pago='mensual', plazo=plazo, fecha_inicio=date(2025, 1, 1),
                )
                self.assertEqual(len(tabla), plazo)
                self.assertEqual(tabla[-1]['saldo'], 0.0)

    def test_el_ultimo_pago_absorbe_la_diferencia(self):
        """El exceso del redondeo hacia arriba sale del último pago, que es menor."""
        tabla = build_amortization_schedule(
            monto=Decimal('100000'), tasa_anual=Decimal('12'), modo='fixed_term',
            tipo_pago='mensual', plazo=7, fecha_inicio=date(2025, 1, 1),
        )
        self.assertLessEqual(tabla[-1]['pago'], tabla[0]['pago'])

    def test_la_cuota_registrada_coincide_con_la_de_la_tabla(self):
        """Si difirieran, el detalle mostraría una cuota y la tabla otra."""
        monto, tasa, plazo = Decimal('100000'), Decimal('12'), 7
        cuota = calculate_payment_for_term(monto, tasa, plazo)
        tabla = build_amortization_schedule(
            monto=monto, tasa_anual=tasa, modo='fixed_term',
            tipo_pago='mensual', plazo=plazo, fecha_inicio=date(2025, 1, 1),
        )
        self.assertEqual(Decimal(str(tabla[0]['pago'])), cuota)

    def test_los_importes_devengados_siguen_con_redondeo_normal(self):
        """ROUND_UP es sólo para la cuota; intereses y saldos no cambian."""
        self.assertEqual(quantize_money(Decimal('1.234')), Decimal('1.23'))
        self.assertEqual(quantize_money(Decimal('1.235')), Decimal('1.24'))


class PortafolioCalculoTests(TestCase):
    """Valuación de posiciones. Las cifras se contrastan contra la fórmula de
    Banxico  P = VN / (1 + r·t/360)  para que el devengo sea consistente con
    el precio de descuento real de un CETE."""

    def test_precio_cete_formula_banxico(self):
        # CETE a 28 días con rendimiento 10% anual, valor nominal $10.
        precio = precio_cete(Decimal('10'), Decimal('10'), 28)
        esperado = Decimal('10') / (Decimal('1') + Decimal('0.10') * Decimal(28) / Decimal(360))
        self.assertEqual(precio, esperado.quantize(Decimal('0.0000001')))
        self.assertLess(precio, Decimal('10'))  # se compra bajo par

    def test_el_devengo_reproduce_el_valor_nominal_al_vencimiento(self):
        """Invertir el precio de descuento y devengar el plazo completo debe
        devolver exactamente el valor nominal. Si no, las dos fórmulas no
        estarían describiendo el mismo instrumento."""
        precio = precio_cete(Decimal('10'), Decimal('10'), 28)
        final = valor_devengado(precio, Decimal('10'), 28, 360)
        self.assertEqual(final, Decimal('10.00'))

    def test_devengo_lineal_a_mitad_de_plazo(self):
        # $10,000 al 10% anual, base 360: 28 días rinden 10000·0.10·28/360 = 77.78
        valor = valor_devengado(Decimal('10000'), Decimal('10'), 28, 360)
        self.assertEqual(valor, Decimal('10077.78'))
        # A la mitad del plazo, la mitad del rendimiento
        mitad = valor_devengado(Decimal('10000'), Decimal('10'), 14, 360)
        self.assertEqual(mitad, Decimal('10038.89'))

    def test_base_365_rinde_menos_que_base_360(self):
        """Misma tasa y días: la base 360 devenga más por día."""
        v360 = valor_devengado(Decimal('10000'), Decimal('10'), 30, 360)
        v365 = valor_devengado(Decimal('10000'), Decimal('10'), 30, 365)
        self.assertGreater(v360, v365)

    def test_rendimiento_esperado(self):
        rend = rendimiento_esperado(Decimal('10000'), Decimal('10'), 28, 360)
        self.assertEqual(rend, Decimal('77.78'))

    def test_tasa_cero_no_devenga(self):
        self.assertEqual(valor_devengado(Decimal('5000'), Decimal('0'), 90, 360), Decimal('5000.00'))

    def test_dias_se_acotan_al_plazo(self):
        """Una posición vencida no sigue creciendo indefinidamente."""
        compra = date(2025, 1, 1)
        self.assertEqual(dias_transcurridos(compra, date(2025, 1, 15), 28), 14)
        self.assertEqual(dias_transcurridos(compra, date(2025, 2, 1), 28), 28)
        self.assertEqual(dias_transcurridos(compra, date(2026, 1, 1), 28), 28)

    def test_fecha_anterior_a_la_compra_no_devenga(self):
        self.assertEqual(dias_transcurridos(date(2025, 6, 1), date(2025, 1, 1), 28), 0)

    def test_tasa_efectiva_supera_la_nominal_por_reinversion(self):
        efectiva = tasa_efectiva_anual(Decimal('10'), 28, 360)
        self.assertGreater(efectiva, Decimal('10'))
        self.assertLess(efectiva, Decimal('11'))


class InversionModelTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.user = User.objects.create_user(username='inv_tester', password='pw')
        cls.hoy = date(2026, 8, 9)

    def _cete(self, **kw):
        datos = dict(
            owner=self.user, plataforma=Inversion.PLATAFORMA_CETESDIRECTO,
            nombre='CETES 28 días', tipo=Inversion.TIPO_DESCUENTO,
            monto_invertido=Decimal('10000'), fecha_compra=self.hoy - timedelta(days=14),
            tasa_anual=Decimal('10'), plazo_dias=28, base_dias=360,
        )
        datos.update(kw)
        return Inversion.objects.create(**datos)

    def test_valor_estimado_devenga_por_dias(self):
        cete = self._cete()
        self.assertEqual(cete.valor_estimado(self.hoy), Decimal('10038.89'))

    def test_valor_al_vencimiento(self):
        self.assertEqual(self._cete().valor_al_vencimiento(), Decimal('10077.78'))

    def test_fecha_vencimiento(self):
        cete = self._cete()
        self.assertEqual(cete.fecha_vencimiento, cete.fecha_compra + timedelta(days=28))

    def test_posicion_vencida_no_sigue_creciendo(self):
        cete = self._cete(fecha_compra=self.hoy - timedelta(days=200))
        self.assertTrue(cete.vencida)
        self.assertEqual(cete.valor_estimado(self.hoy), cete.valor_al_vencimiento())

    def test_fondo_usa_valor_capturado_y_no_proyecta(self):
        fondo = self._cete(tipo=Inversion.TIPO_FONDO, nombre='BONDDIA',
                           plazo_dias=0, tasa_anual=Decimal('0'),
                           valor_manual=Decimal('10500'))
        self.assertTrue(fondo.es_fondo)
        self.assertIsNone(fondo.fecha_vencimiento)
        self.assertEqual(fondo.valor_estimado(self.hoy), Decimal('10500'))

    def test_valor_manual_tiene_prioridad_sobre_la_proyeccion(self):
        cete = self._cete(valor_manual=Decimal('10050'))
        self.assertEqual(cete.valor_estimado(self.hoy), Decimal('10050'))

    def test_rendimiento_incluye_lo_ya_cobrado(self):
        """En Briq los rendimientos salen de la posición al bolsillo; sin
        contarlos, el rendimiento total quedaría subestimado."""
        briq = self._cete(plataforma=Inversion.PLATAFORMA_BRIQ,
                          nombre='Torre GDL', tipo=Inversion.TIPO_TASA_FIJA,
                          base_dias=365)
        sin_cobros = briq.rendimiento(self.hoy)
        MovimientoInversion.objects.create(
            inversion=briq, fecha=self.hoy, monto=Decimal('120'),
            tipo=MovimientoInversion.TIPO_RENDIMIENTO,
        )
        self.assertEqual(briq.rendimiento(self.hoy), sin_cobros + Decimal('120'))


class PortafolioViewTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.user = User.objects.create_user(username='port_tester', password='pw')

    def setUp(self):
        self.client.login(username='port_tester', password='pw')

    def _datos(self, **kw):
        datos = {
            'plataforma': 'cetesdirecto', 'nombre': 'CETES 28 días',
            'tipo': 'descuento', 'monto_invertido': '10000',
            'fecha_compra': '2026-08-01', 'tasa_anual': '10',
            'plazo_dias': '28', 'base_dias': '360', 'valor_manual': '', 'notas': '',
        }
        datos.update(kw)
        return datos

    def test_alta_de_posicion(self):
        response = self.client.post(reverse('prestamos:nueva_inversion'), self._datos())
        self.assertEqual(response.status_code, 302)
        inv = Inversion.objects.get(nombre='CETES 28 días')
        self.assertEqual(inv.owner, self.user)
        self.assertEqual(inv.base_dias, 360)

    def test_instrumento_a_plazo_exige_tasa_y_plazo(self):
        response = self.client.post(reverse('prestamos:nueva_inversion'),
                                    self._datos(tasa_anual='', plazo_dias=''))
        self.assertEqual(response.status_code, 200)
        self.assertIn('tasa_anual', response.context['form'].errors)
        self.assertIn('plazo_dias', response.context['form'].errors)
        self.assertFalse(Inversion.objects.exists())

    def test_fondo_exige_valor_capturado(self):
        response = self.client.post(reverse('prestamos:nueva_inversion'),
                                    self._datos(tipo='fondo', tasa_anual='', plazo_dias=''))
        self.assertEqual(response.status_code, 200)
        self.assertIn('valor_manual', response.context['form'].errors)

    def test_fondo_con_valor_se_registra(self):
        response = self.client.post(reverse('prestamos:nueva_inversion'), self._datos(
            tipo='fondo', nombre='BONDDIA', tasa_anual='', plazo_dias='', valor_manual='10500',
        ))
        self.assertEqual(response.status_code, 302)
        self.assertEqual(Inversion.objects.get(nombre='BONDDIA').valor_manual, Decimal('10500'))

    def test_dashboard_consolida_totales(self):
        self.client.post(reverse('prestamos:nueva_inversion'), self._datos())
        self.client.post(reverse('prestamos:nueva_inversion'), self._datos(
            plataforma='briq', nombre='Torre GDL', tipo='tasa_fija',
            monto_invertido='5000', base_dias='365', plazo_dias='365', tasa_anual='14',
        ))
        response = self.client.get(reverse('prestamos:portafolio'))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context['total_invertido'], Decimal('15000'))
        self.assertGreater(response.context['total_valor'], Decimal('15000'))
        self.assertEqual(len(response.context['por_plataforma']), 2)

    def test_dashboard_vacio_no_divide_entre_cero(self):
        response = self.client.get(reverse('prestamos:portafolio'))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context['rendimiento_pct'], Decimal('0.00'))

    def test_registrar_rendimiento_cobrado(self):
        self.client.post(reverse('prestamos:nueva_inversion'), self._datos())
        inv = Inversion.objects.get(nombre='CETES 28 días')
        self.client.post(reverse('prestamos:registrar_movimiento_inversion', args=[inv.pk]),
                         {'tipo': 'rendimiento', 'monto': '77.78', 'fecha': '2026-08-29'})
        self.assertEqual(inv.movimientos.count(), 1)

    def test_portafolio_ajeno_no_es_visible(self):
        self.client.post(reverse('prestamos:nueva_inversion'), self._datos())
        inv = Inversion.objects.get(nombre='CETES 28 días')
        otro = User.objects.create_user(username='otro_inv', password='pw')
        self.client.force_login(otro)
        self.assertEqual(
            self.client.get(reverse('prestamos:detalle_inversion', args=[inv.pk])).status_code, 404)
        self.assertEqual(len(self.client.get(reverse('prestamos:portafolio')).context['posiciones']), 0)

    def test_requiere_login(self):
        self.client.logout()
        response = self.client.get(reverse('prestamos:portafolio'))
        self.assertEqual(response.status_code, 302)
        self.assertIn('login', response.url)


class DetalleAccionesRapidasTests(TestCase):
    """Botones de pago e incremento en la cabecera del detalle.

    Los formularios viven en modales, no repetidos al final de la página: dos
    formularios con los mismos `name` en el mismo documento son una fuente de
    errores silenciosos.
    """

    @classmethod
    def setUpTestData(cls):
        cls.user = User.objects.create_user(username='botones_tester', password='pw')
        cls.prestamo = Prestamo.objects.create(
            owner=cls.user, nombre_cliente='Oscar',
            monto_original=Decimal('408000'), tasa_interes_anual=Decimal('0'),
            tipo_pago='semanal', fecha_inicio=date(2025, 4, 3),
            saldo_actual=Decimal('408000'), modo='fixed_payment',
            pago_mensual=Decimal('3975'),
        )

    def setUp(self):
        self.client.login(username='botones_tester', password='pw')

    def _html(self, prestamo=None):
        prestamo = prestamo or self.prestamo
        return self.client.get(
            reverse('prestamos:detalle_prestamo', args=[prestamo.pk])).content.decode()

    def test_los_botones_abren_los_modales(self):
        html = self._html()
        self.assertIn('data-bs-target="#modalPago"', html)
        self.assertIn('data-bs-target="#modalIncremento"', html)
        self.assertIn('id="modalPago"', html)
        self.assertIn('id="modalIncremento"', html)

    def test_no_hay_formularios_duplicados(self):
        html = self._html()
        self.assertEqual(
            html.count(reverse('prestamos:registrar_pago', args=[self.prestamo.pk])), 1)
        self.assertEqual(
            html.count(reverse('prestamos:registrar_incremento', args=[self.prestamo.pk])), 1)

    def test_el_pago_sigue_registrandose(self):
        self.client.post(reverse('prestamos:registrar_pago', args=[self.prestamo.pk]),
                         {'monto': '3975', 'fecha': '2026-08-09', 'descripcion': 'Pago'})
        self.assertEqual(self.prestamo.movimientos.filter(tipo='pago').count(), 1)

    def test_una_deuda_usa_su_propio_vocabulario(self):
        deuda = Prestamo.objects.create(
            owner=self.user, rol=Prestamo.ROL_DEUDA, nombre_cliente='Banco',
            concepto='Casa', monto_original=Decimal('100'), tasa_interes_anual=Decimal('0'),
            tipo_pago='mensual', fecha_inicio=date(2025, 1, 1), saldo_actual=Decimal('100'),
            modo='fixed_payment', pago_mensual=Decimal('10'),
        )
        html = self._html(deuda)
        self.assertIn('Registrar Pago Realizado', html)
        self.assertIn('Registrar Cargo Adicional', html)


class InteresSobreFaltanteTests(TestCase):
    """El interés del período corre sobre lo que faltó por pagar, no sobre la
    cuota entera, y se suma al capital."""

    def _prestamo(self, tasa='10', **kw):
        datos = dict(
            nombre_cliente='Oscar', monto_original=Decimal('408000'),
            tasa_interes_anual=Decimal(tasa), tipo_pago='semanal',
            fecha_inicio=date(2025, 4, 3), saldo_actual=Decimal('408000'),
            modo='fixed_payment', pago_mensual=Decimal('3975'),
        )
        datos.update(kw)
        return Prestamo.objects.create(**datos)

    def test_interes_proporcional_al_faltante(self):
        p = self._prestamo()
        Movimiento.objects.create(prestamo=p, fecha=date(2025, 4, 10),
                                  monto=Decimal('3000'), tipo='pago')
        p.actualizar_saldo(date(2025, 4, 11))
        cargo = p.movimientos.get(tipo='interes_cargo')
        # faltante 975 sobre tasa semanal 10%/52
        esperado = (Decimal('975') * Decimal('10') / Decimal('100') / Decimal('52'))
        self.assertEqual(cargo.monto, esperado.quantize(Decimal('0.01')))

    def test_sin_pago_el_interes_corre_sobre_la_cuota_completa(self):
        p = self._prestamo()
        p.actualizar_saldo(date(2025, 4, 11))
        cargo = p.movimientos.get(tipo='interes_cargo')
        esperado = (Decimal('3975') * Decimal('10') / Decimal('100') / Decimal('52'))
        self.assertEqual(cargo.monto, esperado.quantize(Decimal('0.01')))

    def test_cuota_cubierta_no_genera_cargo(self):
        p = self._prestamo()
        Movimiento.objects.create(prestamo=p, fecha=date(2025, 4, 10),
                                  monto=Decimal('3975'), tipo='pago')
        p.actualizar_saldo(date(2025, 4, 11))
        self.assertEqual(p.movimientos.filter(tipo='interes_cargo').count(), 0)

    def test_el_cargo_se_suma_al_capital(self):
        p = self._prestamo()
        p.actualizar_saldo(date(2025, 4, 11))
        cargo = p.movimientos.get(tipo='interes_cargo')
        self.assertEqual(p.saldo_actual, Decimal('408000') + cargo.monto)

    def test_tasa_cero_no_crea_movimientos_vacios(self):
        """Antes se creaba un movimiento de $0.00 por período, que sólo ensuciaba
        el historial."""
        p = self._prestamo(tasa='0')
        p.actualizar_saldo(date(2025, 5, 10))
        self.assertEqual(p.movimientos.filter(tipo='interes_cargo').count(), 0)

    def test_movimiento_anterior_al_inicio_no_bloquea_los_pagos(self):
        """Un movimiento con el año mal capturado (p. ej. 0005 en vez de 2025)
        atascaba el cursor: ningún pago posterior se contabilizaba y todos los
        períodos salían como no cubiertos pese a estar al corriente."""
        p = self._prestamo()
        Movimiento.objects.create(prestamo=p, fecha=date(5, 4, 20),
                                  monto=Decimal('3975'), tipo='pago')
        for dia in (10, 17, 24):
            Movimiento.objects.create(prestamo=p, fecha=date(2025, 4, dia),
                                      monto=Decimal('3975'), tipo='pago')
        p.actualizar_saldo(date(2025, 4, 25))
        self.assertEqual(p.movimientos.filter(tipo='interes_cargo').count(), 0)

    def test_el_movimiento_anterior_al_inicio_sigue_afectando_el_saldo(self):
        """No se ignora: se aplica al balance, sólo que no pertenece a ningún período."""
        p = self._prestamo(tasa='0')
        Movimiento.objects.create(prestamo=p, fecha=date(5, 4, 20),
                                  monto=Decimal('1000'), tipo='pago')
        p.actualizar_saldo(date(2025, 4, 4))
        self.assertEqual(p.saldo_actual, Decimal('407000.00'))

    def test_pago_parcial_en_varios_abonos_suma(self):
        """Dos abonos que juntos cubren la cuota no devengan interés."""
        p = self._prestamo()
        for dia, monto in ((6, '2000'), (9, '1975')):
            Movimiento.objects.create(prestamo=p, fecha=date(2025, 4, dia),
                                      monto=Decimal(monto), tipo='pago')
        p.actualizar_saldo(date(2025, 4, 11))
        self.assertEqual(p.movimientos.filter(tipo='interes_cargo').count(), 0)


class MovimientoInversionEdicionTests(TestCase):
    """Editar y borrar movimientos del portafolio."""

    @classmethod
    def setUpTestData(cls):
        cls.user = User.objects.create_user(username='mov_inv_tester', password='pw')
        cls.inversion = Inversion.objects.create(
            owner=cls.user, plataforma=Inversion.PLATAFORMA_OTRA, nombre='IMSSdigital',
            tipo=Inversion.TIPO_FONDO, monto_invertido=Decimal('5987.85'),
            fecha_compra=date(2024, 9, 13), valor_manual=Decimal('5987.85'),
        )

    def setUp(self):
        self.client.login(username='mov_inv_tester', password='pw')
        self.mov = MovimientoInversion.objects.create(
            inversion=self.inversion, fecha=date(2024, 11, 1), monto=Decimal('9979.75'),
            tipo=MovimientoInversion.TIPO_APORTACION, descripcion='',
        )

    def test_los_botones_aparecen_en_la_tabla(self):
        html = self.client.get(
            reverse('prestamos:detalle_inversion', args=[self.inversion.pk])).content.decode()
        self.assertIn(reverse('prestamos:editar_movimiento_inversion', args=[self.mov.pk]), html)
        self.assertIn(reverse('prestamos:borrar_movimiento_inversion', args=[self.mov.pk]), html)

    def test_editar_actualiza_los_campos(self):
        response = self.client.post(
            reverse('prestamos:editar_movimiento_inversion', args=[self.mov.pk]),
            {'tipo': 'rendimiento', 'monto': '150.50', 'fecha': '2024-12-01',
             'descripcion': 'Corregido'})
        self.assertEqual(response.status_code, 302)
        self.mov.refresh_from_db()
        self.assertEqual(self.mov.tipo, 'rendimiento')
        self.assertEqual(self.mov.monto, Decimal('150.50'))
        self.assertEqual(self.mov.fecha, date(2024, 12, 1))
        self.assertEqual(self.mov.descripcion, 'Corregido')

    def test_editar_con_monto_invalido_no_guarda(self):
        response = self.client.post(
            reverse('prestamos:editar_movimiento_inversion', args=[self.mov.pk]),
            {'tipo': 'aportacion', 'monto': '0', 'fecha': '2024-11-01', 'descripcion': ''})
        self.assertEqual(response.status_code, 200)
        self.mov.refresh_from_db()
        self.assertEqual(self.mov.monto, Decimal('9979.75'))

    def test_borrar_elimina_el_movimiento(self):
        response = self.client.post(
            reverse('prestamos:borrar_movimiento_inversion', args=[self.mov.pk]))
        self.assertEqual(response.status_code, 302)
        self.assertFalse(MovimientoInversion.objects.filter(pk=self.mov.pk).exists())

    def test_borrar_por_get_no_elimina(self):
        """Un GET no debe destruir datos: sólo redirige."""
        self.client.get(reverse('prestamos:borrar_movimiento_inversion', args=[self.mov.pk]))
        self.assertTrue(MovimientoInversion.objects.filter(pk=self.mov.pk).exists())

    def test_no_se_puede_tocar_el_movimiento_de_otro(self):
        otro = User.objects.create_user(username='ajeno_mov', password='pw')
        self.client.force_login(otro)
        self.assertEqual(self.client.get(
            reverse('prestamos:editar_movimiento_inversion', args=[self.mov.pk])).status_code, 404)
        self.assertEqual(self.client.post(
            reverse('prestamos:borrar_movimiento_inversion', args=[self.mov.pk])).status_code, 404)
        self.assertTrue(MovimientoInversion.objects.filter(pk=self.mov.pk).exists())

    def test_requiere_login(self):
        self.client.logout()
        response = self.client.get(
            reverse('prestamos:editar_movimiento_inversion', args=[self.mov.pk]))
        self.assertEqual(response.status_code, 302)
        self.assertIn('login', response.url)


class ImportadorParsingTests(TestCase):
    """Lectura y validación de archivos. Sin tocar la base."""

    def _csv(self, contenido, nombre='datos.csv'):
        return SimpleUploadedFile(nombre, contenido.encode('utf-8'), content_type='text/csv')

    def test_lee_csv_con_coma(self):
        f = self._csv('fecha,monto,tipo\n2025-04-18,3975.00,pago\n')
        filas, error = leer_tabla(f, 'datos.csv')
        self.assertIsNone(error)
        self.assertEqual(len(filas), 1)
        self.assertEqual(filas[0]['monto'], '3975.00')

    def test_lee_csv_con_punto_y_coma(self):
        """Excel en español exporta con ';'."""
        f = self._csv('fecha;monto;tipo\n2025-04-18;3975.00;pago\n')
        filas, error = leer_tabla(f, 'datos.csv')
        self.assertIsNone(error)
        self.assertEqual(len(filas), 1)

    def test_encabezados_con_acentos_y_mayusculas(self):
        f = self._csv('Fecha,MONTO,Descripción\n2025-04-18,100,Hola\n')
        filas, _ = leer_tabla(f, 'datos.csv')
        self.assertIn('descripcion', filas[0])
        self.assertIn('monto', filas[0])

    def test_lee_excel(self):
        from openpyxl import Workbook
        libro = Workbook()
        hoja = libro.active
        hoja.append(['fecha', 'monto', 'tipo'])
        hoja.append([date(2025, 4, 18), 3975, 'pago'])
        buffer = BytesIO()
        libro.save(buffer)
        buffer.seek(0)
        subido = SimpleUploadedFile('datos.xlsx', buffer.read())
        filas, error = leer_tabla(subido, 'datos.xlsx')
        self.assertIsNone(error)
        self.assertEqual(filas[0]['fecha'], date(2025, 4, 18))

    def test_formato_no_soportado(self):
        f = SimpleUploadedFile('datos.pdf', b'%PDF-1.4', content_type='application/pdf')
        filas, error = leer_tabla(f, 'datos.pdf')
        self.assertIn('Formato no reconocido', error)

    def test_montos_con_simbolos(self):
        self.assertEqual(a_decimal('$3,975.00'), Decimal('3975.00'))
        self.assertEqual(a_decimal(' 1 234.50 '.replace(' ', '')), Decimal('1234.50'))

    def test_fechas_en_varios_formatos(self):
        for texto in ('2025-04-18', '18/04/2025', '18-04-2025'):
            self.assertEqual(a_fecha(texto), date(2025, 4, 18))

    def test_rechaza_anio_absurdo(self):
        """El bug de las fechas 0005 no debe poder entrar por importación."""
        with self.assertRaises(ValueError) as ctx:
            a_fecha('0005-04-20')
        self.assertIn('mal capturado', str(ctx.exception))

    def test_fila_con_error_se_reporta_con_su_linea(self):
        f = self._csv('fecha,monto,tipo\n2025-04-18,3975,pago\nsin_fecha,abc,pago\n')
        filas, _ = leer_tabla(f, 'datos.csv')
        validas, errores = procesar(filas, 'movimientos_prestamo')
        self.assertEqual(len(validas), 1)
        self.assertEqual(len(errores), 1)
        self.assertEqual(errores[0]['linea'], 3)

    def test_tipo_invalido_lista_las_opciones(self):
        f = self._csv('fecha,monto,tipo\n2025-04-18,100,berenjena\n')
        filas, _ = leer_tabla(f, 'datos.csv')
        _, errores = procesar(filas, 'movimientos_inversion')
        self.assertIn('Opciones:', errores[0]['motivo'])

    def test_prestamo_pago_fijo_exige_cuota(self):
        f = self._csv('rol,contraparte,monto,tasa,frecuencia,fecha_inicio,modo\n'
                      'prestamo,Oscar,408000,21,semanal,2025-04-03,pago_fijo\n')
        filas, _ = leer_tabla(f, 'datos.csv')
        _, errores = procesar(filas, 'prestamos')
        self.assertIn('Pago Fijo', errores[0]['motivo'])

    def test_fondo_exige_valor(self):
        f = self._csv('plataforma,nombre,tipo,monto,fecha_compra\n'
                      'otra,BONDDIA,fondo,5000,2025-01-01\n')
        filas, _ = leer_tabla(f, 'datos.csv')
        _, errores = procesar(filas, 'inversiones')
        self.assertIn('valor', errores[0]['motivo'])


class ImportarViewTests(TestCase):
    """Flujo completo: previsualizar y confirmar."""

    @classmethod
    def setUpTestData(cls):
        cls.user = User.objects.create_user(username='import_tester', password='pw')
        cls.prestamo = Prestamo.objects.create(
            owner=cls.user, nombre_cliente='Oscar', monto_original=Decimal('408000'),
            tasa_interes_anual=Decimal('21'), tipo_pago='semanal',
            fecha_inicio=date(2025, 4, 3), saldo_actual=Decimal('408000'),
            modo='fixed_payment', pago_mensual=Decimal('3975'),
        )

    def setUp(self):
        self.client.login(username='import_tester', password='pw')

    def _subir(self, contenido, tipo='movimientos_prestamo', destino=None, nombre='p.csv'):
        datos = {'tipo': tipo,
                 'archivo': SimpleUploadedFile(nombre, contenido.encode('utf-8'))}
        if destino is not None:
            datos['destino_id'] = destino
        return self.client.post(reverse('prestamos:importar'), datos)

    def test_previsualizar_no_escribe_nada(self):
        antes = Movimiento.objects.count()
        r = self._subir('fecha,monto,tipo\n2025-04-18,3975,pago\n', destino=self.prestamo.pk)
        self.assertEqual(r.status_code, 200)
        self.assertTrue(r.context['previsualizacion'])
        self.assertEqual(len(r.context['validas']), 1)
        self.assertEqual(Movimiento.objects.count(), antes)

    def test_confirmar_crea_los_movimientos(self):
        self._subir('fecha,monto,tipo\n2025-04-18,3975,pago\n2025-04-25,3975,pago\n',
                    destino=self.prestamo.pk)
        r = self.client.post(reverse('prestamos:importar_confirmar'))
        self.assertEqual(r.status_code, 302)
        self.assertEqual(self.prestamo.movimientos.filter(tipo='pago').count(), 2)

    def test_confirmar_recalcula_el_saldo(self):
        self._subir('fecha,monto,tipo\n2025-04-10,3975,pago\n', destino=self.prestamo.pk)
        self.client.post(reverse('prestamos:importar_confirmar'))
        self.prestamo.refresh_from_db()
        self.assertLess(self.prestamo.saldo_actual, Decimal('408000'))

    def test_confirmar_sin_previsualizar_no_hace_nada(self):
        r = self.client.post(reverse('prestamos:importar_confirmar'))
        self.assertEqual(r.status_code, 302)
        self.assertEqual(Movimiento.objects.count(), 0)

    def test_las_filas_con_error_no_se_importan(self):
        self._subir('fecha,monto,tipo\n2025-04-18,3975,pago\nbasura,x,pago\n',
                    destino=self.prestamo.pk)
        self.client.post(reverse('prestamos:importar_confirmar'))
        self.assertEqual(self.prestamo.movimientos.filter(tipo='pago').count(), 1)

    def test_avisa_de_duplicados_sin_bloquear(self):
        Movimiento.objects.create(prestamo=self.prestamo, fecha=date(2025, 4, 18),
                                  monto=Decimal('3975'), tipo='pago')
        r = self._subir('fecha,monto,tipo\n2025-04-18,3975,pago\n', destino=self.prestamo.pk)
        self.assertEqual(len(r.context['duplicados']), 1)
        self.assertEqual(len(r.context['validas']), 1)

    def test_no_se_puede_importar_a_un_prestamo_ajeno(self):
        otro = User.objects.create_user(username='ajeno_imp', password='pw')
        ajeno = Prestamo.objects.create(
            owner=otro, nombre_cliente='De otro', monto_original=Decimal('100'),
            tasa_interes_anual=Decimal('0'), tipo_pago='mensual',
            fecha_inicio=date(2025, 1, 1), saldo_actual=Decimal('100'))
        r = self._subir('fecha,monto,tipo\n2025-04-18,100,pago\n', destino=ajeno.pk)
        self.assertEqual(r.status_code, 200)
        self.assertFalse(r.context.get('previsualizacion'))
        self.assertEqual(ajeno.movimientos.count(), 0)

    def test_alta_masiva_de_prestamos(self):
        contenido = ('rol,contraparte,concepto,monto,tasa,frecuencia,fecha_inicio,modo,pago\n'
                     'prestamo,Marco,Auto,450000,22,mensual,2024-01-29,pago_fijo,13000\n'
                     'deuda,Banco,Casa,500000,9,mensual,2025-01-01,pago_fijo,10000\n')
        self._subir(contenido, tipo='prestamos')
        self.client.post(reverse('prestamos:importar_confirmar'))
        self.assertTrue(Prestamo.objects.filter(nombre_cliente='Marco', owner=self.user).exists())
        deuda = Prestamo.objects.get(nombre_cliente='Banco')
        self.assertTrue(deuda.es_deuda)
        self.assertEqual(deuda.owner, self.user)

    def test_alta_masiva_de_inversiones(self):
        contenido = ('plataforma,nombre,tipo,monto,fecha_compra,tasa,plazo_dias\n'
                     'cetesdirecto,CETES 28,descuento,50000,2026-07-20,9.75,28\n')
        self._subir(contenido, tipo='inversiones')
        self.client.post(reverse('prestamos:importar_confirmar'))
        inv = Inversion.objects.get(nombre='CETES 28')
        self.assertEqual(inv.owner, self.user)
        self.assertEqual(inv.base_dias, 360)

    def test_archivo_demasiado_grande(self):
        grande = SimpleUploadedFile('p.csv', b'x' * (3 * 1024 * 1024))
        r = self.client.post(reverse('prestamos:importar'), {
            'tipo': 'movimientos_prestamo', 'destino_id': self.prestamo.pk, 'archivo': grande})
        self.assertEqual(r.status_code, 200)
        self.assertFalse(r.context.get('previsualizacion'))

    def test_requiere_login(self):
        self.client.logout()
        r = self.client.get(reverse('prestamos:importar'))
        self.assertEqual(r.status_code, 302)
        self.assertIn('login', r.url)


class ImportadorFormatosLocalesTests(TestCase):
    """Dos fallos que sólo aparecen con archivos reales, no con los de laboratorio."""

    def _csv(self, contenido):
        return SimpleUploadedFile('d.csv', contenido.encode('utf-8'))

    def test_coma_decimal_del_excel_en_espanol(self):
        """'9,75' es 9.75, no 975. Quitar todas las comas creaba una tasa del 975%."""
        self.assertEqual(a_decimal('9,75'), Decimal('9.75'))
        self.assertEqual(a_decimal('10,5'), Decimal('10.50'))
        self.assertEqual(a_decimal('1.234,56'), Decimal('1234.56'))

    def test_coma_de_millar_en_formato_ingles(self):
        self.assertEqual(a_decimal('$3,975.00'), Decimal('3975.00'))
        self.assertEqual(a_decimal('1,234'), Decimal('1234.00'))

    def test_encabezados_sinonimos(self):
        """'Fecha de Pago' debe valer como 'fecha'."""
        f = self._csv('Fecha de Pago,Importe,Tipo\n2025-04-18,3975,pago\n')
        filas, _ = leer_tabla(f, 'd.csv')
        validas, errores = procesar(filas, 'movimientos_prestamo')
        self.assertEqual(errores, [])
        self.assertEqual(validas[0]['fecha'], date(2025, 4, 18))
        self.assertEqual(validas[0]['monto'], Decimal('3975.00'))

    def test_la_columna_original_gana_sobre_el_alias(self):
        f = self._csv('fecha,fecha_de_pago,monto,tipo\n2025-04-18,2020-01-01,100,pago\n')
        filas, _ = leer_tabla(f, 'd.csv')
        validas, _ = procesar(filas, 'movimientos_prestamo')
        self.assertEqual(validas[0]['fecha'], date(2025, 4, 18))

    def test_csv_espanol_completo(self):
        """Separador ';', coma decimal y fecha DD/MM/AAAA a la vez."""
        f = self._csv('plataforma;nombre;tipo;monto;fecha_compra;tasa;plazo_dias\n'
                      'cetesdirecto;CETES 28;descuento;50.000,00;20/07/2026;9,75;28\n')
        filas, _ = leer_tabla(f, 'd.csv')
        validas, errores = procesar(filas, 'inversiones')
        self.assertEqual(errores, [])
        self.assertEqual(validas[0]['monto_invertido'], Decimal('50000.00'))
        self.assertEqual(validas[0]['tasa_anual'], Decimal('9.75'))
        self.assertEqual(validas[0]['fecha_compra'], date(2026, 7, 20))


class AportacionesEnElValorTests(TestCase):
    """Las aportaciones y retiros deben moverse al saldo.

    Antes valor_estimado() sólo miraba monto_invertido y valor_manual, así que
    aportar dinero no cambiaba nada: la posición seguía valiendo lo mismo.
    """

    @classmethod
    def setUpTestData(cls):
        cls.user = User.objects.create_user(username='aport_tester', password='pw')

    def _fondo(self, **kw):
        datos = dict(
            owner=self.user, plataforma=Inversion.PLATAFORMA_OTRA, nombre='IMSSdigital',
            tipo=Inversion.TIPO_FONDO, monto_invertido=Decimal('5987.85'),
            fecha_compra=date(2024, 9, 13), valor_manual=Decimal('5987.85'),
        )
        datos.update(kw)
        return Inversion.objects.create(**datos)

    def _mov(self, inv, tipo, monto, fecha):
        return MovimientoInversion.objects.create(
            inversion=inv, tipo=tipo, monto=Decimal(monto), fecha=fecha)

    def test_las_aportaciones_suben_el_valor_del_fondo(self):
        f = self._fondo()
        self._mov(f, MovimientoInversion.TIPO_APORTACION, '10312.42', date(2024, 10, 1))
        self._mov(f, MovimientoInversion.TIPO_APORTACION, '9979.75', date(2024, 11, 1))
        self._mov(f, MovimientoInversion.TIPO_APORTACION, '10312.42', date(2024, 12, 1))
        esperado = Decimal('5987.85') + Decimal('10312.42') + Decimal('9979.75') + Decimal('10312.42')
        self.assertEqual(f.valor_estimado(date(2026, 8, 10)), esperado)

    def test_capital_invertido_suma_aportaciones(self):
        f = self._fondo()
        self._mov(f, MovimientoInversion.TIPO_APORTACION, '10000', date(2024, 10, 1))
        self.assertEqual(f.capital_invertido, Decimal('15987.85'))

    def test_los_retiros_bajan_valor_y_capital(self):
        f = self._fondo()
        self._mov(f, MovimientoInversion.TIPO_RETIRO, '1000', date(2024, 10, 1))
        self.assertEqual(f.capital_invertido, Decimal('4987.85'))
        self.assertEqual(f.valor_estimado(date(2026, 8, 10)), Decimal('4987.85'))

    def test_una_aportacion_no_se_cuenta_como_rendimiento(self):
        """Compararla contra monto_invertido inflaría el rendimiento por el aporte entero."""
        f = self._fondo()
        self._mov(f, MovimientoInversion.TIPO_APORTACION, '10000', date(2024, 10, 1))
        self.assertEqual(f.rendimiento(date(2026, 8, 10)), Decimal('0.00'))

    def test_movimiento_anterior_al_corte_no_se_cuenta_dos_veces(self):
        """Si el valor capturado ya incluye una aportación, volver a sumarla la duplicaría."""
        f = self._fondo(valor_manual=Decimal('16300.27'), fecha_valor=date(2024, 10, 31))
        self._mov(f, MovimientoInversion.TIPO_APORTACION, '10312.42', date(2024, 10, 1))  # ya incluida
        self._mov(f, MovimientoInversion.TIPO_APORTACION, '9979.75', date(2024, 11, 1))   # posterior
        self.assertEqual(f.valor_estimado(date(2026, 8, 10)),
                         Decimal('16300.27') + Decimal('9979.75'))

    def test_fondo_sin_valor_capturado_usa_el_capital(self):
        f = self._fondo(valor_manual=None)
        self._mov(f, MovimientoInversion.TIPO_APORTACION, '5000', date(2024, 10, 1))
        self.assertEqual(f.valor_estimado(date(2026, 8, 10)), Decimal('10987.85'))

    def test_aportacion_a_plazo_devenga_desde_su_propia_fecha(self):
        """Dinero que entra a mitad del plazo no rinde como si hubiera estado desde el inicio."""
        inv = Inversion.objects.create(
            owner=self.user, plataforma=Inversion.PLATAFORMA_CETESDIRECTO, nombre='CETES',
            tipo=Inversion.TIPO_DESCUENTO, monto_invertido=Decimal('10000'),
            fecha_compra=date(2026, 1, 1), tasa_anual=Decimal('10'),
            plazo_dias=360, base_dias=360,
        )
        self._mov(inv, MovimientoInversion.TIPO_APORTACION, '10000', date(2026, 7, 1))
        hasta = date(2026, 12, 27)  # 360 días del original, 179 de la aportación
        base = Decimal('10000') * (Decimal(1) + Decimal('0.10'))            # ciclo completo
        aporte = Decimal('10000') * (Decimal(1) + Decimal('0.10') * Decimal(179) / Decimal(360))
        self.assertEqual(inv.valor_estimado(hasta),
                         (base + aporte).quantize(Decimal('0.01')))

    def test_movimiento_futuro_no_afecta_el_valor_de_hoy(self):
        f = self._fondo()
        self._mov(f, MovimientoInversion.TIPO_APORTACION, '5000', date(2027, 1, 1))
        self.assertEqual(f.valor_estimado(date(2026, 8, 10)), Decimal('5987.85'))

    def test_el_portafolio_suma_el_capital_no_la_compra_inicial(self):
        f = self._fondo()
        self._mov(f, MovimientoInversion.TIPO_APORTACION, '10000', date(2024, 10, 1))
        self.client.login(username='aport_tester', password='pw')
        r = self.client.get(reverse('prestamos:portafolio'))
        self.assertEqual(r.context['total_invertido'], Decimal('15987.85'))
        self.assertEqual(r.context['total_valor'], Decimal('15987.85'))
