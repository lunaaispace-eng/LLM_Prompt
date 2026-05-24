---
title: LTX Video — Scene Detail List
---
Generate one visual detail list for a set of video scene prompts.

Input format the user will provide:
- A detail label (for example: Camera Motion, Lighting, Weather, Time of Day, Character Movement, Emotion, Facial Expression)
- A numbered list of scene prompts

Task:
For each scene prompt, generate exactly one matching detail line for the requested label.

Rules:
- Output only the list — one line per scene, in the same order as the input scenes
- Do not include numbers, bullets, labels, titles, quotes, markdown, or explanations
- Each line must be short, specific, and fit the requested label only
- Do not combine multiple categories in one line
- Do not repeat the scene prompt text
- Avoid vague words like cinematic, beautiful, dramatic, or interesting unless the label specifically asks for mood or tone
- If a scene does not clearly imply a value for the label, invent a simple value that fits the scene

Label-specific rules:
- Camera Motion: output only camera movement phrases (slow dolly forward, wide pan right, low tracking shot, etc.)
- Lighting: output only lighting descriptions (warm golden hour, flickering neon, harsh midday sun, etc.)
- Weather: output only weather conditions (light rain, overcast, clear, foggy, etc.)
- Time of Day: output only time phrases (golden hour, late afternoon, midnight, early dawn, etc.)
- Character Movement: output only movement description (walks forward slowly, spins and raises arms, leans against wall, etc.)
- Emotion / Facial Expression: output only the emotion or expression (intense focus, wide smile, determined stare, etc.)
- Custom label: apply the label as the sole category

Output only the list of detail lines, one per line, matching the scene count exactly.
