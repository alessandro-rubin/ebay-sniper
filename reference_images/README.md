# Reference images

Used by the photo filter to decide whether a listing shows the wanted variant.

- `positive/`: the wanted variant, from as many angles and lighting conditions
  as possible (front, three-quarter, on the wrist, caseback). Aim for 15 to 20.
  Every listing photo is compared with these (the `match` score).
- `negative/`: what must not trigger a notification: other colour variants
  (for example the full gold-tone case), damaged pieces (cracked crystal,
  missing hands, worn plating), and similar but different watches with spider
  web dials. Today they filter nothing: `calibrate` scores them to show how
  close the thresholds let them come, and `report` shows the similarity of
  each listing to the closest one. They will matter once a classifier is trained on the
  embeddings (planned when each set holds about 30 images).

Guidelines:

- JPEG, PNG or WebP, any size (they are resized by the model preprocessing).
- Strip EXIF metadata before committing (location data in phone photos).
- Crops that show only the watch work better than wide shots.
- Keep file names descriptive, for example `futura-spider-gold-02.jpg`.
- After adding or removing images run `uv run ebay-sniper calibrate`: the
  suggested thresholds may change. Embeddings are cached by file hash, so only
  the new images are computed.
