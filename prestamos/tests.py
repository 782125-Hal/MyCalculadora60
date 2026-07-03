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

from decimal import Decimal
from datetime import date, timedelta
from dateutil.relativedelta import relativedelta

from django.test import TestCase
from django.utils import timezone

from .models import Cliente, Prestamo, Movimiento, RegistroAuditoria
from .calculator import (
    calculate_payment_for_term,
    calculate_term_for_payment,
    build_amortization_schedule,
    quantize_money,
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
    """Regla retroactiva: cobra cargo pleno salvo que la suma de pagos del período
    cubra la mensualidad. Purga+regenera cargos (idempotente, sin perdones)."""

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

    def test_b_pago_parcial_genera_cargo_pleno(self):
        """(b) Pago parcial (< mensualidad) → cargo pleno de una mensualidad."""
        p = self._prestamo()
        self._pago(p, '3000')  # 3000 < 10000
        p.actualizar_saldo(self.hoy)
        cargos = p.movimientos.filter(tipo='interes_cargo')
        self.assertEqual(cargos.count(), 1)
        self.assertEqual(cargos.first().monto, Decimal('100.00'))  # 10000 * 0.01, pleno
        self.assertEqual(p.saldo_actual, Decimal('97100.00'))  # 100000 - 3000 + 100

    def test_c_dos_pagos_que_suman_mensualidad_sin_cargo(self):
        """(c) Dos pagos en el mismo período que juntos cubren la mensualidad → sin cargo."""
        p = self._prestamo()
        self._pago(p, '5000', dias_atras=12)
        self._pago(p, '5000', dias_atras=8)  # 5000 + 5000 = 10000 >= mensualidad
        p.actualizar_saldo(self.hoy)
        self.assertEqual(p.movimientos.filter(tipo='interes_cargo').count(), 0)
        self.assertEqual(p.saldo_actual, Decimal('90000.00'))

    def test_d_pago_de_un_peso_genera_cargo(self):
        """(d) $1.00 NO es perdón: pago parcial → cargo pleno."""
        p = self._prestamo()
        self._pago(p, '1.00')
        p.actualizar_saldo(self.hoy)
        cargos = p.movimientos.filter(tipo='interes_cargo')
        self.assertEqual(cargos.count(), 1)
        self.assertEqual(cargos.first().monto, Decimal('100.00'))
        self.assertEqual(p.saldo_actual, Decimal('100099.00'))  # 100000 - 1 + 100

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
        self.assertContains(response, 'Total Original')

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
