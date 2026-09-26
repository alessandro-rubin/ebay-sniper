# Reference images

Used by the vision filter (milestone M3) to decide whether a listing shows the
wanted variant.

- `positive/`: the wanted variant, from as many angles and lighting conditions
  as possible (front, three-quarter, on the wrist, caseback). Aim for 15 to 20.
- `negative/`: what must not trigger a notification: other colour variants
  (for example the full gold-tone case), damaged pieces (cracked crystal,
  missing hands, worn plating), and similar but different watches with spider
  web dials.

Guidelines:

- JPEG or PNG, any size (they are resized by the model preprocessing).
- Strip EXIF metadata before committing (location data in phone photos).
- Crops that show only the watch work better than wide shots.
- Keep file names descriptive, for example `futura-spider-gold-02.jpg`.
