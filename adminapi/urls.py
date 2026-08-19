from django.urls import path

from . import views

urlpatterns = [
    path("overview", views.overview, name="admin-overview"),
    path("users", views.users_list, name="admin-users"),
    path("users/<int:user_id>/balance", views.adjust_balance, name="admin-balance"),
    path("promos", views.promos_collection, name="admin-promos"),
    path("promos/<str:code>", views.promo_detail, name="admin-promo-detail"),
    path("generations", views.generations, name="admin-generations"),
    path("orders", views.orders, name="admin-orders"),
]
