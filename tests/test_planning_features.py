"""Признаки блюда для оптимизатора (TZ-M8 §6.1): белковая база."""

import os
import sys
import unittest
from decimal import Decimal

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from app.web.planner import _normal  # noqa: E402
from app.web.planning.candidates import CandidateScore, Synonyms  # noqa: E402
from app.web.planning.features import (  # noqa: E402
    DEFAULT_PROTEIN_BASE, attach_bases, protein_base,
)

SYNONYMS = Synonyms.from_rows(
    [
        {"term": "курицы", "canonical": "курица", "kind": "form"},
        {"term": "курица", "canonical": "poultry", "kind": "protein_base"},
        {"term": "свинина", "canonical": "meat", "kind": "protein_base"},
        {"term": "фасоль", "canonical": "legumes", "kind": "protein_base"},
    ]
)


def ingredient(name: str, quantity: str, unit: str = "g") -> dict:
    return {"normalized_name": name, "quantity_min": Decimal(quantity), "unit_code": unit}


class ProteinBaseTests(unittest.TestCase):
    def test_base_comes_from_the_heaviest_protein_not_the_main_ingredient(self) -> None:
        """В плове главный ингредиент — рис, но семья различает плов с курицей."""
        pilaf = {
            "ingredients": [
                ingredient("рис", "500"),
                ingredient("курица", "400"),
                ingredient("морковь", "200"),
            ]
        }
        self.assertEqual(protein_base(pilaf, SYNONYMS, _normal), "poultry")

    def test_two_proteins_the_heavier_one_wins(self) -> None:
        recipe = {
            "ingredients": [ingredient("курица", "150"), ingredient("свинина", "600")]
        }
        self.assertEqual(protein_base(recipe, SYNONYMS, _normal), "meat")

    def test_word_forms_are_resolved_through_the_dictionary(self) -> None:
        recipe = {"ingredients": [ingredient("филе курицы", "300")]}
        self.assertEqual(protein_base(recipe, SYNONYMS, _normal), "poultry")

    def test_kilograms_outweigh_grams(self) -> None:
        recipe = {
            "ingredients": [
                ingredient("курица", "900"),
                ingredient("фасоль", "1", "kg"),
            ]
        }
        self.assertEqual(protein_base(recipe, SYNONYMS, _normal), "legumes")

    def test_vegetable_dish_falls_back_to_veg(self) -> None:
        recipe = {"ingredients": [ingredient("кабачок", "500"), ingredient("укроп", "20")]}
        self.assertEqual(protein_base(recipe, SYNONYMS, _normal), DEFAULT_PROTEIN_BASE)

    def test_attach_bases_fills_every_candidate(self) -> None:
        recipes = [
            {"id": 1, "ingredients": [ingredient("свинина", "300")]},
            {"id": 2, "ingredients": [ingredient("кабачок", "300")]},
        ]
        scores = {1: CandidateScore(1), 2: CandidateScore(2)}
        attach_bases(scores, recipes, SYNONYMS, _normal)
        self.assertEqual(scores[1].protein_base, "meat")
        self.assertEqual(scores[2].protein_base, "veg")


if __name__ == "__main__":
    unittest.main()
