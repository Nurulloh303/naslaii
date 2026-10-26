from django.urls import path

from . import views

urlpatterns = [
    path("overview", views.overview, name="admin-overview"),
    path("users", views.users_list, name="admin-users"),
    path("users/<int:user_id>/balance", views.adjust_balance, name="admin-balance"),
    path("tariffs", views.tariffs, name="admin-tariffs"),
    path("promos", views.promos_collection, name="admin-promos"),
    path("promos/<str:code>", views.promo_detail, name="admin-promo-detail"),
    path("generations", views.generations, name="admin-generations"),
    path("orders", views.orders, name="admin-orders"),
    # ApiKey boshqaruvi — admin boshqalarga kalit yaratadi va credit qo'shadi
    path("api-keys", views.api_keys_collection, name="admin-api-keys"),
    path("api-keys/<int:key_id>", views.api_key_detail, name="admin-api-key-detail"),
]
