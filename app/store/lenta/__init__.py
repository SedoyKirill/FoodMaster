"""Lenta catalogue collector."""

from .models import ProductCard, ProductDetails
from .parsing import parse_product_card_text, parse_product_details_text

__all__ = [
    "ProductCard",
    "ProductDetails",
    "parse_product_card_text",
    "parse_product_details_text",
]

