import os
import re

from rest_framework import serializers

from .models import GenerationJob

IMAGE_DATA_URL_RE = re.compile(r"^data:image/(png|jpeg|webp);base64,[A-Za-z0-9+/=]+$")

# Как называется площадка в списке проектов. Фронтенд показывает эту
# строку как есть, поэтому здесь готовые подписи, а не коды.
MARKETPLACE_LABELS = {"uzum": "Uzum", "wb": "WB", "ozon": "Ozon"}


def is_image_data_url(value) -> bool:
    return isinstance(value, str) and bool(IMAGE_DATA_URL_RE.match(value))


# Относительный адрес «/media/...» — умолчание, и оно же правильное для
# развёртывания, где сайт и API на одном домене (naslai.uz, а /api и
# /media проксирует nginx). Это важно не только для удобства: у ссылки на
# ЧУЖОЙ домен браузер игнорирует атрибут download, и кнопка «скачать»
# начинает просто открывать картинку.
#
# Ставьте NASLAI_ABSOLUTE_MEDIA_URLS=1, только если API вынесен на
# отдельный домен вроде api.naslai.uz.
ABSOLUTE_MEDIA_URLS = os.environ.get("NASLAI_ABSOLUTE_MEDIA_URLS", "0") == "1"


def _absolute(url: str, request=None) -> str:
    if not url:
        return ""
    if not ABSOLUTE_MEDIA_URLS or request is None or url.startswith(("http://", "https://", "data:")):
        return url
    return request.build_absolute_uri(url)


def result_payload(raw: dict, index: int, request=None) -> dict:
    """Одна готовая картинка в виде, который ждёт интерфейс.

    В базе лежит `url`, а компонент результата читает `previewUrl` и
    `downloadUrl`. Переименование делаем здесь, а не в провайдере: тогда
    и старые задачи, сохранённые раньше, продолжают открываться.
    """
    url = _absolute(str(raw.get("url") or ""), request)
    return {
        "id": str(raw.get("id") or f"result-{index + 1}"),
        "title": raw.get("title") or f"{index + 1}-variant",
        "previewUrl": url,
        "downloadUrl": url,
        "width": raw.get("width") or 0,
        "height": raw.get("height") or 0,
        "referenceUsed": raw.get("referenceUsed"),
        "promptTier": raw.get("promptTier"),
    }


def job_payload(job: GenerationJob, request=None) -> dict:
    """Задача генерации целиком — тип `GenerationJob` на фронтенде."""
    from billing.pricing import quote_for

    settings = job.settings or {}
    results = job.results or []
    elapsed = (job.updated_at - job.created_at).total_seconds() * 1000 if job.updated_at else 0

    try:
        quote = quote_for(settings)
    except (TypeError, ValueError, KeyError):
        # Настройки могли сохраниться до изменения тарифов. Задача важнее
        # цены: без сметы карточка всё равно откроется.
        quote = None

    return {
        "id": str(job.pk),
        "status": job.status,
        "progress": job.progress,
        "message": job.message,
        "error": job.error or None,
        "quote": quote,
        "results": [result_payload(item, index, request) for index, item in enumerate(results)],
        "elapsedMs": int(max(0, elapsed)),
        "tokensCharged": job.tokens_charged,
        "cancelled": job.cancelled,
        "createdAt": job.created_at.isoformat(),
        "context": {
            "title": job.title,
            "category": job.category,
            "summary": (quote or {}).get("label", ""),
            "contentType": job.content_type,
        },
    }


def project_payload(job: GenerationJob, request=None) -> dict:
    """Карточка проекта в кабинете — тип `AccountProject`."""
    settings = job.settings or {}
    results = job.results or []
    first = results[0] if results else None

    if job.content_type == "photo":
        marketplace = "Foto"
    elif job.content_type == "video":
        marketplace = "Video"
    else:
        marketplace = MARKETPLACE_LABELS.get(str(settings.get("marketplace", "uzum")).lower(), "Uzum")

    return {
        "id": str(job.pk),
        "name": job.title or f"Loyiha #{job.pk}",
        "status": "ready" if job.status == "success" and results else "draft",
        "createdAt": job.created_at.isoformat(),
        "marketplace": marketplace,
        # null — значит показывать «rasm yo‘q», а не битую картинку.
        "previewUrl": _absolute(str(first.get("url") or ""), request) if first else None,
        "resultCount": len(results),
        "available": bool(results),
    }


class BriefSerializer(serializers.Serializer):
    title = serializers.CharField(allow_blank=True, required=False, default="")
    # Подзаголовок пишет тот, кто видел фото: AI при анализе или продавец.
    subtitle = serializers.CharField(allow_blank=True, required=False, default="", max_length=40)
    category = serializers.CharField(allow_blank=True, required=False, default="")
    benefits = serializers.ListField(child=serializers.CharField(allow_blank=True), required=False, default=list)


class GenerationCreateSerializer(serializers.Serializer):
    assetDataUrl = serializers.CharField()
    brief = BriefSerializer(required=False, default=dict)
    settings = serializers.DictField()

    def validate_assetDataUrl(self, value):
        if not is_image_data_url(value):
            raise serializers.ValidationError({"error": "INVALID_IMAGE", "message": "To'g'ri mahsulot rasmini yuklang"})
        return value


class GeneratedAssetSerializer(serializers.Serializer):
    id = serializers.CharField()
    url = serializers.CharField()
    width = serializers.IntegerField()
    height = serializers.IntegerField()
    title = serializers.CharField()


class GenerationJobSerializer(serializers.ModelSerializer):
    """Оставлен для админки и тестов. Интерфейс читает `job_payload`."""

    results = serializers.JSONField()

    class Meta:
        model = GenerationJob
        fields = [
            "id",
            "content_type",
            "status",
            "progress",
            "message",
            "error",
            "title",
            "subtitle",
            "category",
            "benefits",
            "settings",
            "results",
            "tokens_charged",
            "cancelled",
            "created_at",
        ]
