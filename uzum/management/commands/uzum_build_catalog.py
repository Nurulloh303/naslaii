"""Uzum shablonidan katalog JSON yasaydi: ``python manage.py uzum_build_catalog``."""

from django.core.management.base import BaseCommand

from uzum import catalog


class Command(BaseCommand):
    help = "Лист2/Лист3 ni o'qib uzum/catalog/<sha>.json ni (qayta) yasaydi. Makros ishga tushirilmaydi."

    def add_arguments(self, parser):
        parser.add_argument("--sha", default="", help="Reyestrdagi shablon SHA-256 (sukut: joriy)")

    def handle(self, *args, **options):
        sha = options["sha"] or catalog.current_template_sha()
        data = catalog.build_catalog(catalog.template_bytes(sha))
        path = catalog.write_catalog(data)
        catalog._loaded.pop(sha, None)
        cats = data["categories"]
        self.stdout.write(self.style.SUCCESS(
            f"{path}: {len(cats)} kategoriya ({sum(c['leaf'] for c in cats)} barg), "
            f"{sum(1 for c in cats if c['filters'])} tasida filtr; "
            + ", ".join(f"{k}={len(v)}" for k, v in data["dictionaries"].items())
        ))
