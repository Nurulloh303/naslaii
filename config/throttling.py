"""Ограничители частоты по видам операций.

Отдельные классы, а не `throttle_scope`, — потому что у функциональных
представлений DRF не видит атрибут области и молча пропускает ограничение.

Ставки задаются в settings.REST_FRAMEWORK["DEFAULT_THROTTLE_RATES"] и
переопределяются переменными окружения THROTTLE_*.
"""

from rest_framework.throttling import AnonRateThrottle, UserRateThrottle


class LoginThrottle(AnonRateThrottle):
    """Вход по паролю. Держит перебор пароля по одному адресу."""

    scope = "login"


class RegisterThrottle(AnonRateThrottle):
    """Регистрация. Без неё аккаунты создают пачками ради стартовых токенов."""

    scope = "register"


class PromoThrottle(UserRateThrottle):
    """Активация промокода. Коды короткие, их перебирают."""

    scope = "promo"


class GenerationThrottle(UserRateThrottle):
    """Создание генерации: каждый запрос — деньги, уплаченные провайдеру."""

    scope = "generation"


class AuditThrottle(UserRateThrottle):
    """Разбор карточки. Дневной лимит есть и в самом представлении,
    здесь — защита от долбёжки запросами."""

    scope = "audit"


class AnalyzeThrottle(UserRateThrottle):
    """Разбор фотографии моделью — тоже платный вызов."""

    scope = "analyze"


class LayerExtractThrottle(UserRateThrottle):
    """Magic Layers: bosilgan nuqtadan element ajratish (SAM2 + eraser — pullik)."""

    scope = "layer_extract"


class PlanThrottle(UserRateThrottle):
    """Sahifalar matn rejasi: kirgan foydalanuvchida OpenAI chaqiriladi."""

    scope = "plan"


class ResizeThrottle(UserRateThrottle):
    """O'lchamni moslash bepul — AI kengaytirish fal.ai'da pullik, shuning uchun soatlik chegara."""

    scope = "resize"



class UzumExportThrottle(UserRateThrottle):
    """Uzum XLSM eksporti: rasm nusxalari va ~1.5 MB fayl yasaladi."""

    scope = "uzum_export"


class UzumPrefillThrottle(UserRateThrottle):
    """Uzum prefill: tavsif bo'lmasa AI (OpenAI) chaqiriladi."""

    scope = "uzum_prefill"
