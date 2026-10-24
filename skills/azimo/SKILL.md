---
name: azimo
description: Make business videos (project pitches, product introductions, founder cards and more) with Axiom Lab (https://axiomlab.online). Use when the user wants a pitch video, product video, promo or narrated explainer film.
---

# Making a video with Axiom Lab

1. Gather what the film is about, who will watch it and what they should do next. Except for a person card, material is required (PDF, slides, DOCX, images or video): upload it first with `azimo_upload_asset`.
2. Create a plan (free) with `azimo_create_plan`, passing `prompt`, `duration_max` and `asset_ids`; for a project pitch set `video_type: project`, then give just the prompt and material, or fill every field in `content_data` (`azimo_list_video_types` lists them). Films are 16:9 and 30–180 seconds.
3. Show the user the plan and **only after they confirm** start production with `azimo_create_video`. Each delivered film is charged once; a film that fails or is cancelled is free.
4. Wait by calling `azimo_wait_video` repeatedly. Once the material is read the video stops at `needs_input` (`confirm_duration`) with a suggested length: tell the user the suggested length, call `azimo_resume_video` with the `duration_sec` they confirm, then keep waiting.
5. Once the video is `complete`, give the user the film and cover download links from `azimo_get_video`.

Never start production before the user confirms. A timeout is not a failure: check the status before retrying.
