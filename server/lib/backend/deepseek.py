"""DeepSeek: OpenCode pinned to the DeepSeek model catalogue.

A model family, not a CLI. The card runs through the OpenCode binary with
its own catalogue and config fields, so the chooser reads "DeepSeek ->
model" instead of "OpenCode -> provider -> model". Composition over an
existing strategy, not a copy.
"""
from __future__ import annotations

from ..backend_catalogue import DEEPSEEK
from .base import CatalogueDefault, BackendBrand

from .opencode import OpenCodeBackend


class DeepSeekBackend(OpenCodeBackend):
    """OpenCode with the DeepSeek catalogue and config fields."""
    # --- catalogue data ---------------------------------------------------
    _catalogue_definition = DEEPSEEK
    id = CatalogueDefault('id')
    label = CatalogueDefault('label')
    required_binary = CatalogueDefault('required_binary')
    aliases = CatalogueDefault('aliases')
    badge = CatalogueDefault('badge')
    detail = CatalogueDefault('detail')
    symbol = CatalogueDefault('symbol')
    brand = CatalogueDefault('brand')
    config_model_field = CatalogueDefault('config_model_field')
    config_effort_field = CatalogueDefault('config_effort_field')
    model_family = CatalogueDefault('model_family')
    model_id_filter = CatalogueDefault('model_id_filter')
    fallback_models = CatalogueDefault('fallback_models')

