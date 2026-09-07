---
name: clarp-transcription-model-installation
description: Recommend and install a managed Clarp server transcription model. Use when a user wants model options, a model download, better language coverage, or help enabling Host transcription. Do not use for custom adapter installation.
---

# Clarp Transcription Model Installation

Start by inspecting the Host's live catalog:

```bash
clarp-admin transcription list
```

Offer two or three models marked `download` or `installed`, prioritizing the
catalog's recommendation. Explain language coverage, download size, and weight
in plain language. Never suggest a model marked `unsupported`.

Before downloading or changing the active model, get explicit authorization
for the exact model and whether it should become the Host default. A model
download can be large and changes Host storage; selecting it changes future
transcription behavior.

Install the approved model:

```bash
clarp-admin transcription install <model-id>
```

If the user also approved making it the default:

```bash
clarp-admin transcription use <model-id>
```

Restart the Clarp server after changing the default, using the platform and
service path reported by `clarp-admin paths`. Then verify the live catalog:

```bash
clarp-admin transcription test
```

Report the installed model ID, whether it became the default, and the test
result. Do not claim success from downloaded files alone; the live capability
response must advertise the model.

Use `clarp-transcription` for removal, diagnostics, or custom adapters.
