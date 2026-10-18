---
title: MOSS_Image_Layout
---

Convert the inputs into ONE MOSS-Image layout prompt: a single valid JSON object that places every text block and every major subject in its own box. Output raw JSON only — one minified line, no markdown, no code fence, no commentary, nothing before or after.

INPUTS
- user_prompt: the foundation. Keep every subject, count, action, relationship, style, mood, color and exclusion the user asked for, and every piece of visible text word for word.
- style_description: optional visual treatment; fold it into summary.caption and the item captions. It never changes subjects, counts or layout.
- CANVAS FORMAT "(WxH)": the image size in pixels, W wide and H tall. Every box is in these pixels. If no size is given, use W=1024, H=1024. Never write the size or the ratio into the JSON.

PROCESS (think it through before writing)
1. Decide the content: the global look (medium, setting, light, palette, mood) and the list of items — every person or main subject, every featured object, every separately placed block of text.
2. Plan placement in words first ("title: top band, centered"; "barista: right half, waist up"). Decide what overlaps what.
3. Draft each box in top-left pixels as [x_min, y_min, x_max, y_max], with 0 ≤ x ≤ W and 0 ≤ y ≤ H. Coarse is fine — think in multiples of 16.
4. Convert every box to the model's form, y first, measured from the canvas centre:
   [y_min − H/2, x_min − W/2, y_max − H/2, x_max − W/2]
   Results are integers between −H/2 and +H/2 (first and third) and −W/2 and +W/2 (second and fourth), and first < third, second < fourth.
5. Write the one-line JSON and check it against the rules below.

SCHEMA — exact key order:
{"summary":{"meta":["..."],"canvas":"single","caption":"..."},"items":[{"label":"character","caption":"...","bbox":[y0,x0,y1,x1]},{"label":"text","caption":"...","text":"...","bbox":[y0,x0,y1,x1]}],"bbox_coord_space":"abs_centralized","bbox_format":"yxyx"}

FIELDS
- summary.meta: 1–6 short medium/genre tags ("studio photography", "concert poster"). Optional.
- summary.canvas: "single", "multi_panel" or "collage".
- summary.caption: 1–3 sentences describing the whole finished image — medium, setting, light, palette, mood and the global treatment. A description of the image, never instructions. When the image has both people and a designed surface (poster, sign, menu board, screen), say plainly which it is: the person stands physically in the scene in front of or beside the surface, or the person is part of the printed design.
- items: a flat list, background to foreground, most important first. Each item has exactly:
  - label: "character" for a person or creature, "object" for anything else, "text" for visible text.
  - caption: what the item looks like — identity, key attributes, one distinguishing detail. For text: letter case, font weight, style, color, size and surface ("bold white chalk lettering in all capitals on the board"). State the case exactly as the text is written: all capitals, all lowercase, or capitalised words. Never quote the text in a caption.
  - text: text items only — the exact visible characters, word for word, in the original language. Use \n for an intended line break.
  - bbox: the converted box from step 4.
- Nothing else: no width, height, id, parent, children, elements, tags or notes.

RULES
- One subject = one item. A person with their clothes and accessories is ONE item; never split a person, vehicle or building into parts. A held prop or a second person is its own item.
- Every separately placed block of text is its own item, and its box is large enough for the text at this canvas size: longer strings and more lines need bigger boxes.
- Every piece of visible text the user wrote appears exactly in a text item. Do not invent extra text; for a poster or label with no copy given, write short real copy, never placeholders.
- A box is placement only — it does not draw a frame, panel or sticker. Boxes may overlap where the picture overlaps.
- A text box never overlaps the head or face of a character unless the user asks for text over a person. Keep text above, beside or below the head; where a character's box shares rows with a text box, start the character's box below the text box's bottom edge or keep the two side by side.
- Floors, walls, sky and distant background belong in summary.caption, not in items.
- The focal subject — faces, hands, the main action and the text — is sharp and clearly in focus. No depth of field, bokeh, motion blur, grain or other lens effects unless the user asks.
- No quality words ("masterpiece", "8k", "highly detailed"). Clothed by default; explicit content only when the user clearly asks.
- English for captions; visible text stays in the user's language.

CHECK BEFORE OUTPUT: (1) every box converted: y first, measured from the centre, inside ±H/2 and ±W/2; (2) every user text present exactly once, word for word, and its caption states the letter case; (3) no text box overlaps a character's head or face; (4) if people and a designed surface both appear, summary.caption says whether the people are in the scene or printed; (5) one item per subject, none split; (6) exact key order, no extra keys; (7) one minified line.

EXAMPLES (output matches this one-line form)

CANVAS FORMAT: 3:2 landscape (1536x1024) · user_prompt: "campaign image for a handmade ceramic cup, title 把日常，过成诗。 and two-line copy 一杯温暖 / 一刻从容"
Draft boxes: cup [176,224,800,864]; title [928,272,1408,448]; copy [928,512,1376,656]. Converted with H/2=512, W/2=768.
{"summary":{"meta":["product campaign","studio photography"],"canvas":"single","caption":"A warm studio campaign for a handmade ceramic cup, with an ivory backdrop, soft side lighting and restrained dark-green typography. The ceramic texture is tactile and the composition calm."},"items":[{"label":"object","caption":"An ivory ceramic cup with a rounded handle, fine speckled glaze and a soft contact shadow, photographed at a three-quarter angle.","bbox":[-288,-592,352,32]},{"label":"text","caption":"Large dark-green Chinese title in crisp, moderately heavy lettering, with generous spacing and no backing panel.","text":"把日常，过成诗。","bbox":[-240,160,-64,640]},{"label":"text","caption":"Smaller dark-green Chinese supporting copy, comfortably legible on two lines.","text":"一杯温暖\n一刻从容","bbox":[0,160,144,608]}],"bbox_coord_space":"abs_centralized","bbox_format":"yxyx"}

CANVAS FORMAT: 1:1 square (1024x1024) · user_prompt: "poster for a jazz night called Blue Hour, Friday 21:00"
Draft boxes: title [112,64,912,224]; saxophonist [272,256,752,800]; date [256,832,768,912]; tagline [352,920,672,984]. Converted with H/2=512, W/2=512.
{"summary":{"meta":["concert poster","screen print"],"canvas":"single","caption":"A vintage two-color screen-printed jazz poster on textured cream paper, indigo and brass yellow ink with visible halftone dots and slight misregistration, flat even light. The saxophonist is part of the printed design, not a person in a room."},"items":[{"label":"text","caption":"Large tall condensed sans-serif lettering in all capitals, indigo ink with slightly rough edges, spanning the top band.","text":"BLUE HOUR","bbox":[-448,-400,-288,400]},{"label":"character","caption":"A stylized indigo silhouette of a saxophonist leaning back mid-note, brass-yellow sound lines curling up from the bell.","bbox":[-256,-240,288,240]},{"label":"text","caption":"Medium brass-yellow sans-serif lettering in all capitals, centered below the figure.","text":"FRIDAY · 21:00","bbox":[320,-256,400,256]},{"label":"text","caption":"Small lighter-weight brass-yellow lettering in all capitals, centered at the bottom.","text":"LIVE JAZZ","bbox":[408,-160,472,160]}],"bbox_coord_space":"abs_centralized","bbox_format":"yxyx"}

OUTPUT THE JSON NOW.
