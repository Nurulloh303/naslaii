"""ApiKey orqali autentifikatsiya.

Tashqi dasturchilar Authorization: Bearer nsk_live_xxx sarlavhasi bilan
so'rov yuboradi. Biz kalitning SHA-256 xeshini bazadan qidiramiz va
tegishli foydalanuvchini topamiz.

Xavfsizlik:
  * Bazada faqat xesh saqlanadi — kalitning o'zi yo'q.
  * Birinchi 18 belgi (prefix) indekslanadi: to'liq scan yo'q.
  * Kalit topilmasa yoki balansi 0 bo'lsa — 401.
"""
from __future__ import annotations

import hashlib
import logging
from django.utils import timezone
from rest_framework.authentication import BaseAuthentication
from rest_framework.exceptions import AuthenticationFailed

from accounts.models import ApiKey

logger = logging.getLogger(__name__)

KEY_PREFIX = "nsk_live_"


class ApiKeyAuthentication(BaseAuthentication):
    """DRF authentication class — ApiKey bilan kiradigan tashqi klientlar uchun."""

    def authenticate(self, request):
        auth_header = request.headers.get("Authorization", "")
        if not auth_header.startswith("Bearer "):
            return None  # Boshqa authentication class ga o'tkazamiz

        token = auth_header[len("Bearer "):].strip()
        if not token.startswith(KEY_PREFIX):
            return None  # Bu bizning kalit emas, skip

        digest = hashlib.sha256(token.encode()).hexdigest()
        try:
            api_key = (
                ApiKey.objects
                .select_related("user")
                .get(digest=digest)
            )
        except ApiKey.DoesNotExist:
            raise AuthenticationFailed("API kalit noto'g'ri yoki mavjud emas.")

        # last_used_at ni yangilash (har safar emas — faqat agar 1 soat o'tgan bo'lsa)
        now = timezone.now()
        if not api_key.last_used_at or (now - api_key.last_used_at).seconds > 3600:
            ApiKey.objects.filter(pk=api_key.pk).update(last_used_at=now)

        # request ga api_key ni yopishtiramiz — view ichida foydalanish uchun
        request.api_key = api_key
        return (api_key.user, api_key)

    def authenticate_header(self, request):
        return "Bearer"
