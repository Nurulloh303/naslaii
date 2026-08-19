from django.urls import path

from . import views

urlpatterns = [
    path("questions", views.questions, name="audit-questions"),
    path("run", views.run_audit, name="audit-run"),
]
