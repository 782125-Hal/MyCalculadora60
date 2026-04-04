from rest_framework.routers import DefaultRouter
from .api_views import ClienteViewSet, PrestamoViewSet, MovimientoViewSet

router = DefaultRouter()
router.register(r'clientes', ClienteViewSet, basename='cliente')
router.register(r'prestamos', PrestamoViewSet, basename='prestamo')
router.register(r'movimientos', MovimientoViewSet, basename='movimiento')

urlpatterns = router.urls
