import os

from rest_framework.decorators import api_view, permission_classes
from rest_framework.generics import ListAPIView
from rest_framework.permissions import AllowAny, IsAuthenticated
from rest_framework.response import Response

from .models import LedgerEntry
from .pricing import quote_for, token_packs
from .serializers import LedgerEntrySerializer, QuoteRequestSerializer


@api_view(["POST"])
@permission_classes([AllowAny])
def quote(request):
    serializer = QuoteRequestSerializer(data=request.data)
    serializer.is_valid(raise_exception=True)
    return Response(quote_for(serializer.validated_data["settings"]))


class LedgerListView(ListAPIView):
    serializer_class = LedgerEntrySerializer
    permission_classes = [IsAuthenticated]

    def get_queryset(self):
        return LedgerEntry.objects.filter(user=self.request.user)


def payments_enabled() -> bool:
    return os.environ.get("PAYMENTS_ENABLED", "0") == "1"


@api_view(["GET", "POST"])
@permission_classes([IsAuthenticated])
def orders(request):
    """Заказы на покупку токенов.

    Платежи Click и Payme ещё не подключены, и это намеренно: токены
    нельзя начислять до подтверждения от провайдера, а обработчика
    уведомлений пока нет. Поэтому эндпоинт существует и отвечает понятно,
    но заказ не создаёт — иначе появится «оплаченный» заказ без денег.

    Когда платежи появятся: заводите модель заказа, отдавайте checkoutUrl
    и начисляйте токены ТОЛЬКО из обработчика уведомления провайдера.
    """
    if request.method == "GET":
        return Response({"orders": []})

    if not payments_enabled():
        pack_id = str(request.data.get("packId") or "")
        pack = next((item for item in token_packs() if item["id"] == pack_id), None)
        if pack is None:
            return Response({"error": "PACK_NOT_FOUND", "message": "Bunday paket topilmadi"}, status=404)

        provider = request.data.get("provider")
        if provider not in {"click", "payme"}:
            return Response({"error": "BAD_PROVIDER", "message": "To‘lov usuli noto‘g‘ri"}, status=400)

        return Response(
            {
                # Статус unavailable фронтенд знает и показывает как
                # «to‘lov ulanmagan», а не как ошибку.
                "order": {
                    "id": "",
                    "packId": pack["id"],
                    "tokens": pack["tokens"],
                    "priceUzs": pack["priceUzs"],
                    "provider": provider,
                    "status": "unavailable",
                    "createdAt": None,
                },
                "checkoutUrl": None,
                "message": "Onlayn to‘lov hali ulanmagan. Tokenlarni qo‘lga olish uchun administratorga yozing.",
            }
        )

    return Response(
        {"error": "PAYMENTS_NOT_IMPLEMENTED", "message": "To‘lov provayderi ulanmagan"},
        status=501,
    )


@api_view(["DELETE"])
@permission_classes([IsAuthenticated])
def cancel_order(request, order_id: str):
    """Отмена заказа. Пока заказов не существует — отвечаем «нечего
    отменять», чтобы кабинет не показывал ошибку на пустом списке."""
    return Response({"ok": True})
