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
    portafolio,
    nueva_inversion,
    detalle_inversion,
    registrar_movimiento_inversion,
    borrar_inversion,
    editar_movimiento_inversion,
    borrar_movimiento_inversion,
    importar,
    importar_confirmar,
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

    # Portafolio de inversiones
    path('portafolio/', portafolio, name='portafolio'),
    path('portafolio/nueva/', nueva_inversion, name='nueva_inversion'),
    path('portafolio/<int:pk>/', detalle_inversion, name='detalle_inversion'),
    path('portafolio/<int:pk>/movimiento/', registrar_movimiento_inversion, name='registrar_movimiento_inversion'),
    path('portafolio/<int:pk>/borrar/', borrar_inversion, name='borrar_inversion'),
    path('portafolio/movimiento/<int:pk>/editar/', editar_movimiento_inversion, name='editar_movimiento_inversion'),
    path('portafolio/movimiento/<int:pk>/borrar/', borrar_movimiento_inversion, name='borrar_movimiento_inversion'),

    # Importación desde CSV / Excel
    path('importar/', importar, name='importar'),
    path('importar/confirmar/', importar_confirmar, name='importar_confirmar'),

    # Exportaciones CSV (Fase 3)
    path('export/prestamos/', export_prestamos_csv, name='export_prestamos_csv'),
    path('prestamo/<int:pk>/export/', export_prestamo_csv, name='export_prestamo_csv'),
    path('prestamo/<int:pk>/pdf/', export_prestamo_pdf, name='export_prestamo_pdf'),
]