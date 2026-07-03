from django.urls import path
from .views import (
    home,
    CalculadoraView,
    RegistrarPrestamoView,
    lista_prestamos,
    PrestamoDetailView,
    registrar_pago,
    registrar_incremento,
    editar_movimiento,
    borrar_movimiento,
    editar_prestamo,
    delete_prestamo,  # Corrected from borrar_prestamo
    crear_prestamo,
    inversiones,
    registrar_inversion,
    export_prestamos_csv,
    export_prestamo_csv,
    export_prestamo_pdf,
)

app_name = 'prestamos'

urlpatterns = [
    path('', home, name='home'),
    path('calculadora/', CalculadoraView.as_view(), name='calculadora_financiera'),
    path('registrar-prestamo/', RegistrarPrestamoView.as_view(), name='registrar_prestamo'),
    path('lista-prestamos/', lista_prestamos, name='lista_prestamos'),
    path('prestamo/<int:pk>/', PrestamoDetailView.as_view(), name='detalle_prestamo'),
    path('prestamo/<int:prestamo_id>/registrar-pago/', registrar_pago, name='registrar_pago'),
    path('prestamo/<int:prestamo_id>/registrar-incremento/', registrar_incremento, name='registrar_incremento'),
    path('movimiento/<int:movimiento_id>/editar/', editar_movimiento, name='editar_movimiento'),
    path('movimiento/<int:movimiento_id>/borrar/', borrar_movimiento, name='borrar_movimiento'),
    path('prestamo/<int:prestamo_id>/editar/', editar_prestamo, name='editar_prestamo'),
    path('prestamo/<int:prestamo_id>/borrar/', delete_prestamo, name='borrar_prestamo'),  # Use delete_prestamo
    path('crear-prestamo/', crear_prestamo, name='crear_prestamo'),
    path('inversiones/', inversiones, name='inversiones'),
    path('registrar-inversion/', registrar_inversion, name='registrar_inversion'),

    # Exportaciones CSV (Fase 3)
    path('export/prestamos/', export_prestamos_csv, name='export_prestamos_csv'),
    path('prestamo/<int:pk>/export/', export_prestamo_csv, name='export_prestamo_csv'),
    path('prestamo/<int:pk>/pdf/', export_prestamo_pdf, name='export_prestamo_pdf'),
]