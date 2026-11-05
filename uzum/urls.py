from django.urls import path

from . import views

urlpatterns = [
    path("categories", views.categories, name="uzum-categories"),
    path("categories/<int:category_id>", views.category_detail, name="uzum-category"),
    path("dictionary", views.dictionary, name="uzum-dictionary"),
    path("prefill", views.prefill, name="uzum-prefill"),
    path("validate", views.validate, name="uzum-validate"),
    path("exports", views.exports, name="uzum-exports"),
    path("exports/<int:pk>", views.export_detail, name="uzum-export"),
    path("exports/<int:pk>/download", views.export_download, name="uzum-export-download"),
]
