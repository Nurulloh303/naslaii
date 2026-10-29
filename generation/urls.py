from django.urls import path

from . import views

# Подключается из config/urls.py напрямую по адресам /api/generations*.
# Этот файл оставлен, чтобы приложение можно было смонтировать отдельно.
urlpatterns = [
    path("", views.create_generation, name="generation-create"),
    path("<int:pk>", views.generation_detail, name="generation-detail"),
    path("<int:pk>/cancel", views.cancel_generation, name="generation-cancel"),
    path("<int:pk>/results/<int:variant>/layers", views.magic_layers_view, name="generation-magic-layers"),
    path("<int:pk>/results/<int:variant>/layers/extract", views.magic_layers_extract_view,
         name="generation-magic-layers-extract"),
]
