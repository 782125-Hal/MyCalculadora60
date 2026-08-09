"""
Management command para cerrar períodos vencidos en todos los préstamos y
deudas activos.

Pensado para correr a diario (cron, scheduler de Railway, etc.). Hoy el saldo
se recalcula al visitar el dashboard, la lista o el detalle; este comando
permite hacerlo por fuera, de modo que la app no dependa de que alguien abra
una página para que los cargos queden al día.

Es idempotente: actualizar_saldo() purga y regenera los cargos de interés en
cada corrida, así que ejecutarlo dos veces no duplica nada.

Uso:
    python manage.py cerrar_periodos
    python manage.py cerrar_periodos --hasta 2025-12-31
    python manage.py cerrar_periodos --rol deuda
"""
import logging
from datetime import datetime

from django.core.management.base import BaseCommand, CommandError
from django.utils import timezone

from prestamos.models import Prestamo

logger = logging.getLogger('prestamos')


class Command(BaseCommand):
    help = "Cierra períodos vencidos en todos los préstamos y deudas activos."

    def add_arguments(self, parser):
        parser.add_argument(
            '--hasta',
            type=str,
            default=None,
            help='Fecha límite YYYY-MM-DD (default: hoy).',
        )
        parser.add_argument(
            '--rol',
            type=str,
            choices=[Prestamo.ROL_PRESTAMO, Prestamo.ROL_DEUDA],
            default=None,
            help='Limitar a un solo rol (default: ambos).',
        )

    def handle(self, *args, **options):
        if options['hasta']:
            try:
                hasta = datetime.strptime(options['hasta'], '%Y-%m-%d').date()
            except ValueError:
                raise CommandError('Formato de fecha inválido. Usa YYYY-MM-DD.')
        else:
            hasta = timezone.now().date()

        prestamos = Prestamo.objects.filter(activo=True)
        if options['rol']:
            prestamos = prestamos.filter(rol=options['rol'])

        total = prestamos.count()
        actualizados = 0
        fallidos = 0

        for prestamo in prestamos:
            saldo_antes = prestamo.saldo_actual
            try:
                prestamo.actualizar_saldo(hasta)
            except Exception:
                # Un préstamo con datos corruptos no debe abortar la corrida
                # completa: se registra y se sigue con los demás.
                fallidos += 1
                logger.exception("cerrar_periodos falló en el préstamo %s", prestamo.pk)
                self.stderr.write(self.style.WARNING(
                    f'  Préstamo #{prestamo.pk} ({prestamo.nombre_cliente}): error, omitido.'
                ))
                continue
            if prestamo.saldo_actual != saldo_antes:
                actualizados += 1

        resumen = f'Revisados {total} registros hasta {hasta}, {actualizados} con cambios.'
        if fallidos:
            self.stdout.write(self.style.WARNING(f'{resumen} {fallidos} con error.'))
        else:
            self.stdout.write(self.style.SUCCESS(resumen))
