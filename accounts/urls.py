from django.urls import path

from . import views

urlpatterns = [
    path("register", views.register, name="register"),
    path("login", views.login, name="login"),
    path("logout", views.logout, name="logout"),
    # Кто вошёл. Зовётся при загрузке страницы и ставит cookie csrftoken.
    path("session", views.session, name="session"),
    path("me", views.me, name="me"),
    path("providers", views.providers, name="auth-providers"),
    path("google", views.google_login, name="auth-google"),
    path("telegram", views.telegram_login, name="auth-telegram"),
]
