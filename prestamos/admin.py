
from django.contrib import admin
from .models import Cliente, Prestamo, Movimiento

@admin.register(Cliente)
class ClienteAdmin(admin.ModelAdmin):
    list_display = ('nombre', 'telefono')
    search_fields = ('nombre', 'telefono')

@admin.register(Prestamo)
class PrestamoAdmin(admin.ModelAdmin):
    list_display = (
        'nombre_cliente',
        'monto_original',
        'tasa_interes_anual',
        'fecha_inicio',
        'saldo_actual',
        'ultimo_pago',
        'activo'
    )
    list_filter = ('activo', 'tipo_pago', 'modo')
    search_fields = ('nombre_cliente', 'telefono')
    readonly_fields = ('saldo_actual', 'ultimo_pago')  # Hace estos campos solo lectura en admin para evitar ediciones manuales
    actions = ['actualizar_saldos_seleccionados']  # Acción custom para actualizar saldos en bulk

    def actualizar_saldos_seleccionados(self, request, queryset):
        for prestamo in queryset:
            prestamo.actualizar_saldo()
        self.message_user(request, "Saldos actualizados exitosamente.")
    actualizar_saldos_seleccionados.short_description = "Actualizar saldos de préstamos seleccionados"

@admin.register(Movimiento)
class MovimientoAdmin(admin.ModelAdmin):
    list_display = ('prestamo', 'tipo', 'monto', 'fecha', 'descripcion')
    list_filter = ('tipo', 'fecha')
    search_fields = ('prestamo__nombre_cliente', 'descripcion')
    date_hierarchy = 'fecha'  # Para navegación por fechas