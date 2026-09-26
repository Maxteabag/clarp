"""DeepSeek: OpenCode pinned to the DeepSeek model catalogue.

A model family, not a CLI. The card runs through the OpenCode binary with
its own catalogue and config fields, so the chooser reads "DeepSeek ->
model" instead of "OpenCode -> provider -> model". Composition over an
existing strategy, not a copy.
"""
from __future__ import annotations

from .base import BackendBrand

from .opencode import OpenCodeBackend


class DeepSeekBackend(OpenCodeBackend):
    """OpenCode with the DeepSeek catalogue and config fields."""
    # --- catalogue data (was the BackendAdapter registry row) ------------
    id = 'deepseek'
    label = 'DeepSeek'
    required_binary = 'opencode'
    aliases = ('deep-seek',)
    badge = 'BackendDeepSeek'
    detail = 'Runs DeepSeek models through OpenCode.'
    symbol = 'water.waves'
    brand = BackendBrand('#4d6bfe', '#2b47d6', '#6f88ff', '#3554e6')
    config_model_field = 'deepseek_model'
    config_effort_field = 'deepseek_effort'
    model_family = 'deepseek'
    fallback_models = (
        ('fireworks-ai/accounts/fireworks/routers/deepseek-pro-latest', 'DeepSeek Pro (latest, Fireworks)'),
        ('fireworks-ai/accounts/fireworks/routers/deepseek-flash-latest', 'DeepSeek Flash (latest, Fireworks)'),
        ('fireworks-ai/accounts/fireworks/models/deepseek-v4-pro', 'DeepSeek V4 Pro (Fireworks)'),
        ('fireworks-ai/accounts/fireworks/models/deepseek-v4p1-flash', 'DeepSeek V4.1 Flash (Fireworks)'),
        ('huggingface/deepseek-ai/DeepSeek-V4-Pro', 'DeepSeek V4 Pro (Hugging Face)'),
        ('huggingface/deepseek-ai/DeepSeek-V4.1-Flash', 'DeepSeek V4.1 Flash (Hugging Face)'),
    )

