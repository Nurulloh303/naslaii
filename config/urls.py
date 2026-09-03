"""Адреса API.

Совпадают с тем, что зовёт фронтенд (`src/app/account/accountApi.ts` и
`src/features/create/services/createApi.ts`). Меняете путь или формат
ответа — интерфейс ломается молча, без ошибки в консоли Django.
"""

from django.conf import settings
from django.conf.urls.static import static
from django.contrib import admin
from django.urls import include, path

from accounts import views as account_views
from audit import views as audit_views
from billing import views as billing_views
from generation import views as generation_views

urlpatterns = [
    # Адрес админки выносится в окружение: /admin/ перебирают ботами
    # круглосуточно, и смена пути убирает почти весь этот шум.
    path(f"{settings.DJANGO_ADMIN_PATH}/", admin.site.urls),

    # --- аккаунт ------------------------------------------------------
    path("api/auth/", include("accounts.urls")),
    path("api/account", account_views.me, name="account"),
    path("api/profile", account_views.profile, name="profile"),

    # --- токены и цены ------------------------------------------------
    path("api/", include("billing.urls")),

    # --- генерация ----------------------------------------------------
    path("api/analyze", generation_views.analyze, name="analyze"),
    path("api/generations", generation_views.create_generation, name="generation-create"),
    path("api/generations/<int:pk>", generation_views.generation_detail, name="generation-detail"),
    path("api/generations/<int:pk>/cancel", generation_views.cancel_generation, name="generation-cancel"),
    path("api/generations/<int:pk>/results/<int:variant>/regenerate", generation_views.regenerate_slide, name="generation-regenerate-slide"),
    path("api/projects", generation_views.projects_list, name="projects"),
    path("api/projects/<int:pk>", generation_views.project_detail, name="project-detail"),
    path("api/projects/<int:pk>/archive.zip", generation_views.project_archive, name="project-archive"),
    path("api/style-templates", generation_views.style_templates_list, name="style-templates-list"),
    path("api/style-templates/<str:file_name>", generation_views.style_template_file, name="style-template-file"),

    path("api/promo/", include("promos.urls")),
    path("api/admin/", include("adminapi.urls")),

    # --- разбор карточки ----------------------------------------------
    path("api/audit/", include("audit.urls")),
    # Короткий адрес: фронтенд зовёт именно его.
    path("api/audit", audit_views.run_audit, name="audit-run-short"),

    # --- покупка токенов ----------------------------------------------
    path("api/orders", billing_views.orders, name="orders"),
    path("api/orders/<str:order_id>", billing_views.cancel_order, name="order-cancel"),

    path("api/api-keys", account_views.api_keys, name="api-keys"),
    path("api/api-keys/<int:key_id>", account_views.revoke_api_key, name="api-key-revoke"),
]

if settings.DEBUG:
    # На боевом сервере /media/ раздаёт nginx: Django для этого медленный
    # и в один поток.
    urlpatterns += static(settings.MEDIA_URL, document_root=settings.MEDIA_ROOT)
