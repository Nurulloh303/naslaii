"""Django admin login sahifasini parol tanlashdan (brute-force) himoya qilish.

DRF so'rov cheklovlari (config/throttling.py) faqat /api/ ga ishlaydi —
Django admin'ning o'z login formasi (/<DJANGO_ADMIN_PATH>/login/) ularning
tashqarisida qoladi. Bu middleware bitta IP manzildan muvaffaqiyatsiz
urinishlarni sanaydi va limitdan oshsa vaqtincha 429 qaytaradi.

Sozlamalar (.env):
  ADMIN_LOGIN_MAX_FAILURES=5     — ruxsat etilgan xato urinishlar soni
  ADMIN_LOGIN_LOCK_MINUTES=30    — bloklash davomiyligi
"""

from __future__ import annotations

import logging
import os

from django.conf import settings
from django.core.cache import cache
from django.http import HttpResponse

logger = logging.getLogger("security.admin")

MAX_FAILURES = max(1, int(os.environ.get("ADMIN_LOGIN_MAX_FAILURES", 5)))
LOCK_SECONDS = max(60, int(os.environ.get("ADMIN_LOGIN_LOCK_MINUTES", 30)) * 60)


def client_ip(request) -> str:
    """nginx orqasida haqiqiy manzil X-Real-IP / X-Forwarded-For da keladi."""
    real = request.META.get("HTTP_X_REAL_IP", "").strip()
    if real:
        return real
    forwarded = request.META.get("HTTP_X_FORWARDED_FOR", "")
    if forwarded:
        return forwarded.split(",")[0].strip()
    return request.META.get("REMOTE_ADDR", "") or "unknown"


class AdminLoginGuardMiddleware:
    def __init__(self, get_response):
        self.get_response = get_response
        self.login_path = f"/{settings.DJANGO_ADMIN_PATH}/login/"

    def __call__(self, request):
        if request.path != self.login_path or request.method != "POST":
            return self.get_response(request)

        ip = client_ip(request)
        key = f"admin-login-fail:{ip}"
        failures = cache.get(key, 0)
        if failures >= MAX_FAILURES:
            logger.warning("Admin login bloklangan: ip=%s urinishlar=%s", ip, failures)
            return HttpResponse(
                "Juda ko'p muvaffaqiyatsiz urinish. Keyinroq qayta urinib ko'ring.",
                status=429, content_type="text/plain; charset=utf-8",
            )

        response = self.get_response(request)

        # Muvaffaqiyatli kirish — redirect (302). Xato parol — forma qayta (200).
        if response.status_code == 200:
            cache.set(key, failures + 1, LOCK_SECONDS)
            logger.warning(
                "Admin login xato: ip=%s email=%s urinish=%s/%s",
                ip, str(request.POST.get("username", ""))[:80], failures + 1, MAX_FAILURES,
            )
        elif response.status_code in (301, 302):
            cache.delete(key)
            logger.info("Admin login muvaffaqiyatli: ip=%s email=%s", ip, str(request.POST.get("username", ""))[:80])
        return response
