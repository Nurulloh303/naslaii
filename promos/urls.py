from django.urls import path

from . import views

urlpatterns = [
    path("redeem", views.redeem, name="promo-redeem"),
]
