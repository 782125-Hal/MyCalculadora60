"""
URL configuration for MyCalculadora60 project.

The `urlpatterns` list routes URLs to views. For more information please see:
    https://docs.djangoproject.com/en/5.2/topics/http/urls/
Examples:
Function views
    1. Add an import:  from my_app import views
    2. Add a URL to urlpatterns:  path('', views.home, name='home')
Class-based views
    1. Add an import:  from other_app.views import Home
    2. Add a URL to urlpatterns:  path('', Home.as_view(), name='home')
Including another URLconf
    1. Import the include() function: from django.urls import include, path
    2. Add a URL to urlpatterns:  path('blog/', include('blog.urls'))
"""

# MyCalculadora60/urls.py
from django.contrib import admin
from django.urls import path, include
from prestamos import views  # Importa las vistas de la aplicación prestamos

urlpatterns = [
    path('admin/', admin.site.urls),
    path('prestamos/', include('prestamos.urls', namespace='prestamos')),
    path('api/', include('prestamos.api_urls')),
    path('', views.home, name='home'),  # Ruta raíz usando la vista home de prestamos
]