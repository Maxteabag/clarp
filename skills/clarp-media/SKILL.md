---
name: clarp-media
description: Publish local images into Clarp chat and agent galleries. Use when an agent needs to show the user an image or gallery in Clarp.
---

# Clarp Media

Run the installed publisher and include its returned Markdown exactly:

```bash
clarp-media-publish FILE \
  --session "$CLAUDE_PWA_SESSION" --caption "Description"
```

Use `--gallery` with multiple images. Include the returned Markdown in the chat
reply so a single image renders inline and a gallery uses the established
swipeable gallery. Do not create an `image` or `image_gallery` artifact: images
are media, not artifacts. Do not copy files into Clarp storage or write media
database rows directly; the server owns storage. Preserve profile-gallery,
preview, swipe, zoom, and native sharing behavior.
