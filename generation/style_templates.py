"""Галерея готовых шаблонов дизайна — зеркало server/index.mjs (Node).

~130 инфографик, вручную загруженных продавцом в media/style-templates/
и размеченных отдельным скриптом (scripts/classify-style-templates.mjs,
живёт на фронтенд-стороне репозитория) по тем же 8 стилям, что и
STYLE_PRESETS на клиенте. Раздаём манифест и сами картинки публично —
это не персональные данные, а общая витрина шаблонов для всех продавцов.

Путь к файлам настраивается через STYLE_TEMPLATES_DIR (settings.py),
по умолчанию — media/style-templates/ рядом с manage.py.
"""
import json
import os

from django.conf import settings as django_settings

STYLE_TEMPLATE_MIME = {
    "jpg": "image/jpeg",
    "jpeg": "image/jpeg",
    "png": "image/png",
    "webp": "image/webp",
}


def _templates_dir() -> str:
    return str(getattr(
        django_settings,
        "STYLE_TEMPLATES_DIR",
        os.path.join(django_settings.MEDIA_ROOT, "style-templates"),
    ))


def _manifest_path() -> str:
    return os.path.join(_templates_dir(), "manifest.json")


def load_style_templates() -> list:
    """Список {file, category}. Манифест читаем каждый раз заново —
    файл маленький (~12 КБ), а кэш только мешал бы при пополнении
    галереи новыми шаблонами без перезапуска сервера."""
    try:
        with open(_manifest_path(), "r", encoding="utf-8") as handle:
            data = json.load(handle)
    except (OSError, ValueError):
        return []
    if not isinstance(data, list):
        return []
    result = []
    for entry in data:
        if not isinstance(entry, dict):
            continue
        file_name = str(entry.get("file") or "").strip()
        category = str(entry.get("category") or "").strip()
        if file_name and category:
            result.append({"file": file_name, "category": category})
    return result


def style_template_mime(file_name: str) -> str:
    ext = file_name.lower().rsplit(".", 1)[-1] if "." in file_name else ""
    return STYLE_TEMPLATE_MIME.get(ext, "application/octet-stream")


def resolve_style_template_path(file_name: str):
    """Путь к файлу, только если он реально есть в манифесте — так
    запрос никогда не выйдет за пределы папки шаблонов, даже если кто-то
    подставит "../" в адрес."""
    entry = next((item for item in load_style_templates() if item["file"] == file_name), None)
    if entry is None:
        return None
    path = os.path.join(_templates_dir(), entry["file"])
    return path if os.path.isfile(path) else None
