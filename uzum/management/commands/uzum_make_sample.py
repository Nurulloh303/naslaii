"""Sinov uchun namuna eksport: ``python manage.py uzum_make_sample --email seller@example.com --out /tmp/sample.xlsm``.

Serverda ishga tushirilsa, rasm havolalari haqiqatan ochiq bo'ladi (Uzum ularni
yuklab oladi) — real import sinovi uchun shu yo'l. Kategoriya: 15598
"Футбольные мячи", 2 ta SKU (oq/qora), sintetik 1080×1440 rasm.
Namuna matnlar va IKPU (09506620001000000) — faqat sinov uchun: importdan oldin
haqiqiy IKPU ni tasnif.soliq.uz dan oling (``--ikpu``).
"""

import base64
import io
from pathlib import Path

from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand, CommandError
from PIL import Image, ImageDraw

from uzum import services


def synthetic_ball(background=(236, 240, 244)) -> str:
    image = Image.new("RGB", (1080, 1440), background)
    draw = ImageDraw.Draw(image)
    cx, cy, r = 540, 700, 360
    draw.ellipse([cx - r + 18, cy + r - 30, cx + r - 18, cy + r + 30], fill=(200, 205, 210))  # soya
    draw.ellipse([cx - r, cy - r, cx + r, cy + r], fill=(250, 250, 250), outline=(30, 30, 30), width=8)
    pent = [(cx, cy - 110), (cx + 105, cy - 34), (cx + 65, cy + 90), (cx - 65, cy + 90), (cx - 105, cy - 34)]
    draw.polygon(pent, fill=(25, 25, 25))
    for x, y in pent:
        dx, dy = x - cx, y - cy
        draw.line([(x, y), (cx + dx * 2.6, cy + dy * 2.6)], fill=(30, 30, 30), width=8)
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    return "data:image/png;base64," + base64.b64encode(buffer.getvalue()).decode("ascii")


def sample_draft() -> dict:
    sku = {"weightG": 450, "heightMm": 230, "widthMm": 230, "lengthMm": 230, "barcode": ""}
    return {
        "categoryId": 15598,
        "brand": "Отсутствует бренд",
        "model": "",
        "country": "Узбекистан",
        "titleRu": "Футбольный мяч, размер 5",
        "titleUz": "Futbol to'pi, 5-o'lcham",
        "descriptionRu": "Футбольный мяч размера 5 для тренировок и игры на открытых площадках. "
                         "Сшитые панели, прорезиненная камера, хорошо держит форму.",
        "descriptionUz": "Mashg'ulot va ochiq maydonda o'ynash uchun 5-o'lchamdagi futbol to'pi. "
                         "Tikilgan panellar, rezina kamera, shaklini yaxshi saqlaydi.",
        "shortRu": "Футбольный мяч, размер 5",
        "shortUz": "Futbol to'pi, 5-o'lcham",
        "ikpu": "09506620001000000",  # NAMUNA: haqiqiy IKPU ni tasnif.soliq.uz dan oling
        "groupKey": "naslai-sample-football-5",
        "photos": [{"dataUrl": synthetic_ball()}],
        "filters": {},
        "skus": [
            {**sku, "sellerId": "NSL-BALL-WHITE", "color": "Белый", "price": 120000, "oldPrice": 150000},
            {**sku, "sellerId": "NSL-BALL-BLACK", "color": "Черный", "price": 120000, "oldPrice": 120000},
        ],
    }


class Command(BaseCommand):
    help = "Namuna Uzum XLSM eksportini yaratadi (15598, 2 SKU, sintetik rasm)."

    def add_arguments(self, parser):
        parser.add_argument("--email", required=True, help="Eksport egasi (mavjud foydalanuvchi)")
        parser.add_argument("--out", default="", help="Faylni shu yo'lga ham nusxalash")
        parser.add_argument("--ikpu", default="", help="Haqiqiy IKPU (tasnif.soliq.uz)")

    def handle(self, *args, **options):
        user = get_user_model().objects.filter(email=options["email"]).first()
        if user is None:
            raise CommandError("Foydalanuvchi topilmadi")
        draft = sample_draft()
        if options["ikpu"]:
            draft["ikpu"] = options["ikpu"]
        outcome, value = services.create_export(user, draft)
        if outcome != "ok":
            raise CommandError(f"Eksport yaratilmadi: {value}")
        payload = services.export_payload(value)
        if options["out"]:
            data = services.export_storage().open(value.file_path, "rb").read()
            out = Path(options["out"])
            out.parent.mkdir(parents=True, exist_ok=True)
            out.write_bytes(data)
            self.stdout.write(f"Fayl: {out} ({len(data)} bayt)")
        self.stdout.write(self.style.SUCCESS(f"Eksport #{value.pk}: {payload['photoUrls']}"))
