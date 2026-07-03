
from django.contrib import admin
from .models import Cliente, Prestamo, Movimiento, RegistroAuditoria

@admin.register(Cliente)
class ClienteAdmin(admin.ModelAdmin):
    list_display = ('nombre', 'owner', 'telefono')
    list_filter = ('owner',)
    search_fields = ('nombre', 'telefono')

@admin.register(Prestamo)
class PrestamoAdmin(admin.ModelAdmin):
    list_display = (
        'nombre_cliente',
        'owner',
        'monto_original',
        'tasa_interes_anual',
        'fecha_inicio',
        'saldo_actual',
        'ultimo_pago',
        'activo'
    )
    list_filter = ('activo', 'tipo_pago', 'modo', 'owner')
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


@admin.register(RegistroAuditoria)
class RegistroAuditoriaAdmin(admin.ModelAdmin):
    """Bitácora de solo lectura: no se puede crear/editar/borrar desde el admin."""
    list_display = ('fecha', 'usuario_nombre', 'accion', 'modelo', 'objeto_id', 'detalle')
    list_filter = ('accion', 'modelo', 'fecha')
    search_fields = ('usuario_nombre', 'detalle')
    date_hierarchy = 'fecha'
    readonly_fields = ('usuario', 'usuario_nombre', 'accion', 'modelo', 'objeto_id', 'detalle', 'fecha')

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False