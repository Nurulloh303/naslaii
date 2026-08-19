"""Единый вид ошибки API.

Фронтенд у каждой неудачи читает поле `message` и показывает его человеку.
DRF по умолчанию отвечает то `{"detail": "..."}`, то `{"email": ["..."]}` —
в интерфейсе это превращается в общее «Не удалось выполнить запрос», и
человек не понимает, что именно исправить.

Здесь любой отказ приводится к виду:

    {"error": "КОД", "message": "текст для человека", "details": {...}}

`details` остаётся для отладки; интерфейс на него не смотрит.
"""

from __future__ import annotations

from django.http import Http404
from rest_framework import status as http_status
from rest_framework.exceptions import (
    AuthenticationFailed,
    NotAuthenticated,
    PermissionDenied,
    Throttled,
    ValidationError,
)
from rest_framework.response import Response
from rest_framework.views import exception_handler as drf_exception_handler

# Текст «Kabinetga kiring» фронтенд узнаёт и по нему сбрасывает
# сохранённый аккаунт. Меняете здесь — поменяйте и в AccountContext.tsx.
NOT_AUTHENTICATED_MESSAGE = "Kabinetga kiring"

DEFAULT_MESSAGES = {
    400: "So‘rovda xatolik bor",
    401: NOT_AUTHENTICATED_MESSAGE,
    403: "Bu amalga ruxsat yo‘q",
    404: "Topilmadi",
    405: "Bu usul qo‘llab-quvvatlanmaydi",
    409: "Amal allaqachon bajarilgan",
    413: "Fayl juda katta",
    429: "Juda ko‘p so‘rov. Biroz kuting.",
    500: "Serverda xatolik. Birozdan so‘ng urinib ko‘ring.",
}

DEFAULT_CODES = {
    400: "BAD_REQUEST",
    401: "UNAUTHORIZED",
    403: "FORBIDDEN",
    404: "NOT_FOUND",
    405: "METHOD_NOT_ALLOWED",
    409: "CONFLICT",
    413: "PAYLOAD_TOO_LARGE",
    429: "RATE_LIMITED",
    500: "SERVER_ERROR",
}


def _first_text(value) -> str | None:
    """Достаёт первую осмысленную строку из ответа DRF любой вложенности."""
    if isinstance(value, str):
        return value.strip() or None
    if isinstance(value, dict):
        # Своё сообщение важнее автоматического: код вида
        # ValidationError({"message": "..."}) писали именно для человека.
        for key in ("message", "detail", "error"):
            if key in value:
                found = _first_text(value[key])
                if found:
                    return found
        for item in value.values():
            found = _first_text(item)
            if found:
                return found
        return None
    if isinstance(value, (list, tuple)):
        for item in value:
            found = _first_text(item)
            if found:
                return found
    return None


def _explicit_code(value) -> str | None:
    """Код ошибки, если представление его явно указало (INSUFFICIENT_TOKENS и т. п.)."""
    if isinstance(value, dict):
        raw = value.get("error")
        if isinstance(raw, str) and raw.isupper():
            return raw
        if isinstance(raw, (list, tuple)) and raw and isinstance(raw[0], str) and raw[0].isupper():
            return raw[0]
        for item in value.values():
            found = _explicit_code(item)
            if found:
                return found
    if isinstance(value, (list, tuple)):
        for item in value:
            found = _explicit_code(item)
            if found:
                return found
    return None


def api_exception_handler(exc, context):
    if isinstance(exc, Http404):
        # Админка намеренно отвечает 404, а не 403: посторонний не должен
        # даже узнать, что раздел существует.
        return Response(
            {"error": DEFAULT_CODES[404], "message": DEFAULT_MESSAGES[404]},
            status=http_status.HTTP_404_NOT_FOUND,
        )

    response = drf_exception_handler(exc, context)
    if response is None:
        # Необработанное исключение оставляем Django: иначе оно не попадёт
        # ни в логи, ни в трассировку, и причину будет не найти.
        return None

    code_number = response.status_code
    payload = response.data

    message = _first_text(payload)
    error_code = _explicit_code(payload)

    if isinstance(exc, (NotAuthenticated, AuthenticationFailed)):
        message = NOT_AUTHENTICATED_MESSAGE
        error_code = error_code or "UNAUTHORIZED"
    elif isinstance(exc, PermissionDenied):
        error_code = error_code or "FORBIDDEN"
    elif isinstance(exc, Throttled):
        wait = int(exc.wait or 0)
        message = f"Juda ko‘p so‘rov. {wait} soniyadan so‘ng urinib ko‘ring." if wait else DEFAULT_MESSAGES[429]
        error_code = error_code or "RATE_LIMITED"
    elif isinstance(exc, ValidationError):
        error_code = error_code or "VALIDATION_ERROR"

    response.data = {
        "error": error_code or DEFAULT_CODES.get(code_number, "ERROR"),
        "message": message or DEFAULT_MESSAGES.get(code_number, "So‘rovni bajarib bo‘lmadi"),
    }
    # Разбор по полям оставляем для отладки, интерфейс его не читает.
    if isinstance(payload, dict) and code_number == 400:
        response.data["details"] = payload

    return response
