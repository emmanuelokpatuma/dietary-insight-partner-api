"""Request models (pydantic v2). Unknown fields are rejected so partner typos surface early."""
from __future__ import annotations

from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field, StringConstraints

Short = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=100)]


class Portion(BaseModel):
    model_config = ConfigDict(extra="forbid")
    amount: float = Field(gt=0, le=100)
    unit: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=20)]
    grams: float | None = Field(default=None, gt=0, le=5000)


class OfficialNutrition(BaseModel):
    model_config = ConfigDict(extra="forbid")
    kcal: float = Field(ge=0, le=10000)
    protein_g: float = Field(ge=0, le=1000)
    carbs_g: float = Field(ge=0, le=1000)
    fat_g: float = Field(ge=0, le=1000)


class MenuItemIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    partner_item_id: Short
    name: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=200)]
    description: Annotated[str, StringConstraints(strip_whitespace=True, max_length=1000)] | None = None
    ingredients: list[Short] | None = Field(default=None, max_length=60)
    portion: Portion | None = None
    official_nutrition: OfficialNutrition | None = None
    declared_allergens: list[Short] | None = Field(default=None, max_length=14)
