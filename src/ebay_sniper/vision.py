"""Compare listing photos with the reference images, using a local embedding model.

Everything runs on this machine: no listing photo leaves it (see the hard
constraints in CLAUDE.md). torch and open_clip are an optional extra
(``uv sync --extra vision``) and are imported only when a model is loaded.

Three signals, measured on the reference sets on 2026-10-01 and on the stored
listings on 2026-10-02:

- ``match``: the highest cosine similarity between any listing photo and any
  positive reference. It tells this watch model from unrelated objects, but
  other watches of the brand score as high as some references.
- ``colour``: the projection of the photo embedding on a silver-minus-gold
  direction built from text prompts. Image similarity alone ranks the
  gold-tone variant as high as the wanted silver-tone one (photos of the same
  model on a wrist are closer to each other than the case colours are), while
  this probe separates them.
- ``web``: the projection on a spider-web-dial-minus-plain-dial direction,
  built the same way. Other Futura watches score near zero, the references
  and other spider web dials clearly above it.
"""

from __future__ import annotations

import hashlib
import importlib.util
import io
import logging
import re
from array import array
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, Protocol

from ebay_sniper.config import VisionConfig
from ebay_sniper.images import ImageCache, ImageFetchError
from ebay_sniper.models import VisionScore

if TYPE_CHECKING:
    from PIL.Image import Image

log = logging.getLogger(__name__)

Vector = Sequence[float]

IMAGE_SUFFIXES = frozenset({".jpg", ".jpeg", ".png", ".webp"})
# Silver-tone versus gold-tone descriptions; their differences are averaged
# into one direction in the embedding space. The direction is not normalized:
# editing, adding or removing a prompt rescales the scores, so run `calibrate`
# again and revisit the thresholds.
COLOUR_PROMPTS: tuple[tuple[str, str], ...] = (
    ("a silver watch", "a gold watch"),
    ("a silver-tone metal watch case", "a gold-tone metal watch case"),
    (
        "a photo of a silver watch with a spider web dial",
        "a photo of a gold watch with a spider web dial",
    ),
    ("a stainless steel watch", "a gold plated watch"),
)
# Spider-web dial versus plain dial descriptions, averaged the same way.
WEB_PROMPTS: tuple[tuple[str, str], ...] = (
    ("a watch with a spider web pattern on the dial", "a watch with a plain dial"),
    ("a spider web watch dial", "a plain white watch dial"),
    (
        "a wristwatch with a black spider web printed on a white mother of pearl dial",
        "a wristwatch with a plain dial and hour markers",
    ),
    ("a photo of a watch with a cobweb design and a spider", "a photo of an ordinary watch"),
)
# The colour of a listing is the most silver-looking of the photos closest to
# the positives: recall comes first, a missed listing costs more than a
# useless notification. The web score is taken over every photo instead: a
# close-up of the dial is often not among the photos closest to the positives,
# and only an actual web pattern raises it.
COLOUR_TOP_PHOTOS = 3
# Large photos are shrunk before preprocessing, which only needs a few hundred
# pixels, to save memory and time.
MAX_DECODE_SIDE = 1024
# Suggested thresholds sit this far below the lowest positive reference:
# listing photos are usually worse than curated references, and a missed
# listing costs more than a useless notification.
MATCH_MARGIN = 0.05
COLOUR_MARGIN = 0.01
WEB_MARGIN = 0.01


class VisionError(RuntimeError):
    """A listing could not be scored."""


def vision_available() -> bool:
    """Whether the optional dependencies are installed."""
    return all(importlib.util.find_spec(name) for name in ("torch", "open_clip", "PIL"))


class Embedder(Protocol):
    """Turns images and texts into L2-normalised vectors in one shared space."""

    @property
    def model_id(self) -> str: ...

    def embed_images(self, images: Sequence[bytes]) -> list[Vector]: ...

    def embed_texts(self, texts: Sequence[str]) -> list[Vector]: ...


class OpenClipEmbedder:
    """An open_clip model, loaded on first use and kept for the life of the process."""

    def __init__(self, model: str, pretrained: str, *, batch_size: int = 16) -> None:
        self._model_name = model
        self._pretrained = pretrained
        self._batch_size = batch_size
        self._loaded: tuple[Any, Any, Any] | None = None

    @property
    def model_id(self) -> str:
        return f"{self._model_name}/{self._pretrained}"

    def embed_images(self, images: Sequence[bytes]) -> list[Vector]:
        import torch

        model, preprocess, _ = self._load()
        vectors: list[Vector] = []
        for start in range(0, len(images), self._batch_size):
            batch = [
                preprocess(decode_image(data)) for data in images[start : start + self._batch_size]
            ]
            with torch.inference_mode():
                features = model.encode_image(torch.stack(batch))
                features = torch.nn.functional.normalize(features, dim=-1)
            vectors.extend(features.tolist())
        return vectors

    def embed_texts(self, texts: Sequence[str]) -> list[Vector]:
        import torch

        model, _, tokenizer = self._load()
        with torch.inference_mode():
            features = model.encode_text(tokenizer(list(texts)))
            features = torch.nn.functional.normalize(features, dim=-1)
        return features.tolist()

    def _load(self) -> tuple[Any, Any, Any]:
        if self._loaded is None:
            import open_clip

            log.info("Loading the image model %s", self.model_id)
            model, _, preprocess = open_clip.create_model_and_transforms(
                self._model_name, pretrained=self._pretrained
            )
            model.eval()
            self._loaded = (model, preprocess, open_clip.get_tokenizer(self._model_name))
        return self._loaded


def decode_image(data: bytes) -> Image:
    """An RGB image, upright according to its EXIF orientation."""
    from PIL import Image, ImageOps

    with Image.open(io.BytesIO(data)) as raw:
        raw.draft("RGB", (MAX_DECODE_SIDE, MAX_DECODE_SIDE))
        image = ImageOps.exif_transpose(raw).convert("RGB")
    image.thumbnail((MAX_DECODE_SIDE, MAX_DECODE_SIDE))
    return image


class EmbeddingCache:
    """Vectors on disk, keyed by model and content hash, so nothing is embedded twice."""

    def __init__(self, directory: Path, model_id: str) -> None:
        slug = re.sub(r"[^A-Za-z0-9._-]+", "_", model_id)
        self._dir = directory / slug

    def get_or_compute(
        self, blobs: Sequence[bytes], compute: Callable[[Sequence[bytes]], list[Vector]]
    ) -> list[Vector]:
        keys = [hashlib.sha256(blob).hexdigest() for blob in blobs]
        vectors: list[Vector | None] = [self._read(key) for key in keys]
        missing = [i for i, vector in enumerate(vectors) if vector is None]
        if missing:
            computed = compute([blobs[i] for i in missing])
            for i, vector in zip(missing, computed, strict=True):
                self._write(keys[i], vector)
                vectors[i] = vector
        return [vector for vector in vectors if vector is not None]

    def _path(self, key: str) -> Path:
        return self._dir / key[:2] / f"{key}.f32"

    def _read(self, key: str) -> Vector | None:
        path = self._path(key)
        if not path.exists():
            return None
        values = array("f")
        values.frombytes(path.read_bytes())
        return values.tolist()

    def _write(self, key: str, vector: Vector) -> None:
        path = self._path(key)
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_bytes(array("f", vector).tobytes())
        tmp.replace(path)


@dataclass(frozen=True, slots=True)
class ReferenceImage:
    path: Path
    vector: Vector


@dataclass(frozen=True, slots=True)
class References:
    positives: tuple[ReferenceImage, ...]
    negatives: tuple[ReferenceImage, ...]
    # Directions, not normalized: silver-tone minus gold-tone, spider-web dial
    # minus plain dial.
    colour_axis: Vector
    web_axis: Vector


def reference_files(directory: Path) -> list[Path]:
    if not directory.is_dir():
        return []
    return sorted(path for path in directory.iterdir() if path.suffix.lower() in IMAGE_SUFFIXES)


def dot(a: Vector, b: Vector) -> float:
    return sum(x * y for x, y in zip(a, b, strict=True))


def prompt_axis(wanted: Sequence[Vector], unwanted: Sequence[Vector]) -> list[float]:
    """The mean of the wanted-minus-unwanted differences of paired prompts."""
    pairs = list(zip(wanted, unwanted, strict=True))
    return [sum(w[i] - u[i] for w, u in pairs) / len(pairs) for i in range(len(pairs[0][0]))]


def score_vectors(
    photos: Sequence[Vector],
    positives: Sequence[Vector],
    negatives: Sequence[Vector],
    colour_axis: Vector,
    web_axis: Vector,
    *,
    model_id: str,
) -> VisionScore:
    """Score the photos of one listing against the references."""
    if not photos:
        raise VisionError("no photos to score")
    if not positives:
        raise VisionError("no positive reference images")
    matches = [max(dot(photo, ref) for ref in positives) for photo in photos]
    negative = max(
        (dot(photo, ref) for photo in photos for ref in negatives), default=float("-inf")
    )
    ranked = sorted(range(len(photos)), key=lambda i: matches[i], reverse=True)
    colours = [dot(photos[i], colour_axis) for i in ranked[:COLOUR_TOP_PHOTOS]]
    return VisionScore(
        match=matches[ranked[0]],
        negative=negative if negatives else 0.0,
        colour=max(colours),
        best_photo=ranked[0],
        photos=len(photos),
        model=model_id,
        web=max(dot(photo, web_axis) for photo in photos),
    )


def below_threshold_reason(score: VisionScore, config: VisionConfig) -> str | None:
    """Why the listing should not be notified, or None. Always None in shadow mode."""
    if not config.filter:
        return None
    if score.match < config.match_threshold:
        return f"photos not similar enough (match {score.match:.3f} < {config.match_threshold:.3f})"
    if score.colour < config.colour_threshold:
        return f"looks gold-tone (colour {score.colour:+.3f} < {config.colour_threshold:+.3f})"
    # Scores stored before the web probe existed have no web value: not filtered.
    if score.web is not None and score.web < config.web_threshold:
        return f"no spider web dial (web {score.web:+.3f} < {config.web_threshold:+.3f})"
    return None


@dataclass(frozen=True, slots=True)
class ReferenceScore:
    path: Path
    positive: bool
    score: VisionScore


def score_references(refs: References, model_id: str) -> list[ReferenceScore]:
    """Every reference image scored against all the others (leave-one-out)."""
    positives = [ref.vector for ref in refs.positives]
    negatives = [ref.vector for ref in refs.negatives]
    results = [
        ReferenceScore(
            ref.path,
            True,
            score_vectors(
                [ref.vector],
                positives[:i] + positives[i + 1 :],
                negatives,
                refs.colour_axis,
                refs.web_axis,
                model_id=model_id,
            ),
        )
        for i, ref in enumerate(refs.positives)
    ]
    results += [
        ReferenceScore(
            ref.path,
            False,
            score_vectors(
                [ref.vector],
                positives,
                negatives[:i] + negatives[i + 1 :],
                refs.colour_axis,
                refs.web_axis,
                model_id=model_id,
            ),
        )
        for i, ref in enumerate(refs.negatives)
    ]
    return results


def suggest_thresholds(scores: Sequence[ReferenceScore]) -> tuple[float, float, float]:
    """(match, colour, web) thresholds that keep every positive reference, with a margin."""
    positives = [result.score for result in scores if result.positive]
    if not positives:
        raise VisionError("no positive reference images")
    return (
        round(min(score.match for score in positives) - MATCH_MARGIN, 3),
        round(min(score.colour for score in positives) - COLOUR_MARGIN, 3),
        round(min(score.web for score in positives if score.web is not None) - WEB_MARGIN, 3),
    )


class VisionScorer:
    """Downloads the photos of a listing and scores them against the references."""

    def __init__(
        self,
        config: VisionConfig,
        embedder: Embedder,
        embeddings: EmbeddingCache,
        images: ImageCache,
    ) -> None:
        self._config = config
        self._embedder = embedder
        self._embeddings = embeddings
        self._images = images
        self._references: References | None = None

    @property
    def model_id(self) -> str:
        return self._embedder.model_id

    def references(self) -> References:
        """The reference embeddings, computed once (and cached on disk by file hash)."""
        if self._references is None:
            positives = self._embed_files(reference_files(self._config.positive_dir))
            negatives = self._embed_files(reference_files(self._config.negative_dir))
            if not positives:
                raise VisionError(f"no images in {self._config.positive_dir}")
            texts = [text for pair in (*COLOUR_PROMPTS, *WEB_PROMPTS) for text in pair]
            vectors = self._embeddings.get_or_compute(
                [text.encode() for text in texts],
                lambda blobs: self._embedder.embed_texts([blob.decode() for blob in blobs]),
            )
            colour, web = vectors[: 2 * len(COLOUR_PROMPTS)], vectors[2 * len(COLOUR_PROMPTS) :]
            self._references = References(
                positives=positives,
                negatives=negatives,
                colour_axis=prompt_axis(colour[0::2], colour[1::2]),
                web_axis=prompt_axis(web[0::2], web[1::2]),
            )
            log.info("Reference images: %d positive, %d negative", len(positives), len(negatives))
        return self._references

    def score(self, image_urls: Sequence[str]) -> VisionScore:
        """Score the first ``max_photos`` photos; photos that fail to download are skipped."""
        refs = self.references()
        blobs: list[bytes] = []
        failures: list[str] = []
        for url in image_urls[: self._config.max_photos]:
            try:
                blobs.append(self._images.fetch(url))
            except ImageFetchError as exc:
                failures.append(str(exc))
        if failures:
            log.warning("%d photo(s) could not be downloaded: %s", len(failures), failures[0])
        if not blobs:
            raise VisionError("no photo could be downloaded" if failures else "no photos")
        try:
            vectors = self._embeddings.get_or_compute(blobs, self._embedder.embed_images)
        except Exception as exc:
            # A corrupt image or a model problem must not stop the cycle.
            raise VisionError(f"embedding failed: {type(exc).__name__}: {exc}") from exc
        return score_vectors(
            vectors,
            [ref.vector for ref in refs.positives],
            [ref.vector for ref in refs.negatives],
            refs.colour_axis,
            refs.web_axis,
            model_id=self.model_id,
        )

    def _embed_files(self, paths: Sequence[Path]) -> tuple[ReferenceImage, ...]:
        if not paths:
            return ()
        vectors = self._embeddings.get_or_compute(
            [path.read_bytes() for path in paths], self._embedder.embed_images
        )
        return tuple(
            ReferenceImage(path, vector) for path, vector in zip(paths, vectors, strict=True)
        )
