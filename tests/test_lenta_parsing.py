from decimal import Decimal
import unittest

from app.store.lenta.parsing import (
    extract_product_id,
    money_to_kopecks,
    parse_pack,
    parse_product_card_text,
    parse_product_details_text,
)
from app.store.lenta.api import (
    category_id,
    parse_api_product_card,
    parse_api_product_details,
)


class PackParsingTests(unittest.TestCase):
    def test_decimal_litres_are_converted_to_ml(self) -> None:
        raw, quantity, unit = parse_pack("1,5л")
        self.assertEqual(raw, "1,5л")
        self.assertEqual(quantity, Decimal("1500.0"))
        self.assertEqual(unit, "ml")

    def test_multipack_is_summed(self) -> None:
        raw, quantity, unit = parse_pack("4×100 г")
        self.assertEqual(raw, "4×100 г")
        self.assertEqual(quantity, Decimal("400"))
        self.assertEqual(unit, "g")

    def test_money_uses_integer_kopecks(self) -> None:
        self.assertEqual(money_to_kopecks("1 324,99"), 132499)


class ProductCardParsingTests(unittest.TestCase):
    def test_public_card_with_loyalty_and_regular_price(self) -> None:
        text = """
        Добавить в избранное
        4.9
        Молоко пастеризованное ПРОСТОКВАШИНО 2,5%, без змж
        930мл
        Цена за 1 шт
        С Картой №1
        86,99 ₽
        105,29 ₽
        -17%
        до 157 шт
        """
        card = parse_product_card_text(
            text,
            "https://lenta.com/product/moloko-past-rossiya-930ml-80424/",
            category_slug="moloko-128",
        )
        self.assertEqual(card.external_id, "80424")
        self.assertEqual(card.name, "Молоко пастеризованное ПРОСТОКВАШИНО 2,5%, без змж")
        self.assertEqual(card.pack_quantity, Decimal("930"))
        self.assertEqual(card.pack_unit, "ml")
        self.assertEqual(card.price_unit, "piece")
        self.assertEqual(card.loyalty_price_kop, 8699)
        self.assertEqual(card.regular_price_kop, 10529)
        self.assertEqual(card.discount_percent, 17)
        self.assertEqual(card.purchase_limit, 157)

    def test_badges_before_rating_do_not_become_name(self) -> None:
        text = """
        Баллы за отзыв
        Наша марка
        4.9
        Яйцо куриное 365 ДНЕЙ СО
        10шт
        Цена за 1 шт
        С Картой №1
        149,99 ₽
        157,89 ₽
        до 3 шт
        """
        card = parse_product_card_text(
            text,
            "https://lenta.com/product/yajjco-kurinoe-rossiya-10sht-674564/",
        )
        self.assertEqual(card.name, "Яйцо куриное 365 ДНЕЙ СО")
        self.assertEqual(card.pack_quantity, Decimal("10"))
        self.assertEqual(card.pack_unit, "piece")

    def test_product_id_extraction(self) -> None:
        self.assertEqual(
            extract_product_id("https://lenta.com/product/foo-123456/"),
            "123456",
        )

    def test_explicitly_unavailable_card_is_not_orderable(self) -> None:
        card = parse_product_card_text(
            "Молоко\n930 мл\nЦена за 1 л\n105,29 ₽\nНет в наличии",
            "https://lenta.com/product/moloko-80424/",
        )

        self.assertFalse(card.available_for_order)


class ProductDetailsParsingTests(unittest.TestCase):
    def test_details_are_split_into_structured_fields(self) -> None:
        text = """
        Молоко пастеризованное ПРОСТОКВАШИНО 2,5%, без змж, 930мл
        Арт. 080424
        Пищевая ценность
        На 100г продукта
        Белки – 3г, жиры – 2,5г, углеводы – 4,7г
        Состав
        Молоко нормализованное.
        Характеристики
        Бренд
        ПРОСТОКВАШИНО
        Энергетическая ценность
        53 кКал
        Условия хранения
        При температуре от +2 до +6.
        Срок годности
        14 суток
        Артикул
        080424
        Описание
        Обычное молоко.
        """
        details = parse_product_details_text(text)
        self.assertEqual(details.article, "080424")
        self.assertEqual(details.brand, "ПРОСТОКВАШИНО")
        self.assertEqual(details.composition, "Молоко нормализованное.")
        self.assertEqual(details.kcal_100, Decimal("53"))
        self.assertEqual(details.protein_100, Decimal("3"))
        self.assertEqual(details.fat_100, Decimal("2.5"))
        self.assertEqual(details.carb_100, Decimal("4.7"))
        self.assertEqual(details.shelf_life_text, "14 суток")


class ApiParsingTests(unittest.TestCase):
    def test_category_slug_ends_with_numeric_id(self) -> None:
        self.assertEqual(category_id("molochnye-produkty-yajjco-3"), 3)
        self.assertEqual(category_id("17036"), 17036)

    def test_api_card_keeps_regular_and_loyalty_prices(self) -> None:
        card = parse_api_product_card(
            {
                "id": 674564,
                "slug": "yajjco-kurinoe-s0-rossiya-10sht",
                "name": "Яйцо куриное 365 ДНЕЙ СО, 10шт",
                "display": {"name": "Яйцо куриное 365 ДНЕЙ СО", "package": "10 шт"},
                "weight": {"package": "10шт"},
                "count": 3,
                "prices": {
                    "cost": 14999,
                    "costRegular": 15789,
                    "isLoyaltyCardPrice": True,
                    "isPromoactionPrice": False,
                },
                "features": {"isBlockedForSale": False, "isPersonalPrice": False},
                "saleLimit": {"maxSaleQuantity": 3},
                "units": {"saleUnit": "шт"},
                "images": [{"original": "https://example.invalid/image.png"}],
            },
            "molochnye-produkty-yajjco-3",
        )
        self.assertEqual(card.regular_price_kop, 15789)
        self.assertEqual(card.loyalty_price_kop, 14999)
        self.assertEqual(card.pack_quantity, Decimal("10"))
        self.assertEqual(card.purchase_limit, 3)
        self.assertIsNone(card.image_url)
        self.assertNotIn("images", card.raw_text)

    def test_api_details_extract_nutrition_and_storage(self) -> None:
        details = parse_api_product_details(
            {
                "display": {"name": "Яйцо куриное"},
                "attributes": [
                    {"alias": "brand", "slug": "brand", "name": "Бренд", "value": "365 ДНЕЙ"},
                    {"alias": "ingredients", "slug": "sostav", "name": "Состав", "value": "яйцо"},
                    {
                        "alias": "nutritionalValue",
                        "slug": "pishchevaya-cennost",
                        "name": "Пищевая ценность",
                        "value": "Белки – 12,7г, жиры – 11,5г, углеводы – 0,67г",
                    },
                    {
                        "slug": "energeticheskaya-cennost",
                        "name": "Энергетическая ценность",
                        "value": "157 кКал/653 кДж",
                    },
                    {
                        "slug": "usloviya-hraneniya",
                        "name": "Условия хранения",
                        "value": "От 0 до +20 °C",
                    },
                    {"slug": "srok-godnosti", "name": "Срок годности", "value": "25 суток"},
                ],
            }
        )
        self.assertEqual(details.brand, "365 ДНЕЙ")
        self.assertEqual(details.kcal_100, Decimal("157"))
        self.assertEqual(details.protein_100, Decimal("12.7"))
        self.assertEqual(details.storage_conditions, "От 0 до +20 °C")
        self.assertEqual(details.shelf_life_text, "25 суток")


if __name__ == "__main__":
    unittest.main()
