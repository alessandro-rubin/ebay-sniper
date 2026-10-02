from __future__ import annotations

import math
import os
import time
from collections.abc import Sequence
from datetime import timedelta
from pathlib import Path

import httpx
import pytest
import respx

from ebay_sniper.config import VisionConfig
from ebay_sniper.images import ImageCache, ImageFetchError, resize_ebay_image
from ebay_sniper.models import Listing, VisionScore
from ebay_sniper.vision import (
    COLOUR_PROMPTS,
    WEB_PROMPTS,
    EmbeddingCache,
    References,
    VisionError,
    VisionScorer,
    below_threshold_reason,
    prompt_axis,
    reference_files,
    score_references,
    score_vectors,
    suggest_thresholds,
    vision_available,
)

PHOTO = "https://i.ebayimg.com/images/g/AAA/s-l225.jpg"
JPEG = {"content-type": "image/jpeg"}


def unit(*values: float) -> list[float]:
    norm = math.sqrt(sum(v * v for v in values))
    return [v / norm for v in values]


# Directions in a 3-d toy space: the wanted watch, other watches, and the colour axis.
WANTED = unit(1, 0, 0)
OTHER = unit(0, 1, 0)
AXIS = [0.0, 0.0, 1.0]
# The web probe reuses the second direction.
WEB_AXIS = [0.0, 1.0, 0.0]


def test_resize_ebay_image() -> None:
    assert resize_ebay_image(PHOTO, "s-l500") == PHOTO.replace("s-l225", "s-l500")
    assert resize_ebay_image("https://example.com/a.jpg", "s-l500") == "https://example.com/a.jpg"


def test_score_vectors_takes_the_best_photo_its_colour_and_web() -> None:
    photos = [unit(0.2, 0.3, -0.5), unit(1, 0.1, 0.3), unit(1, 0.3, -0.2), unit(0.05, 1, 0.9)]
    score = score_vectors(photos, [WANTED], [OTHER], AXIS, WEB_AXIS, model_id="m")
    assert score.best_photo == 1
    assert score.match == pytest.approx(photos[1][0])
    assert score.negative == pytest.approx(max(p[1] for p in photos))
    # The most silver-looking of the three photos closest to the positives...
    assert score.colour == pytest.approx(max(photos[i][2] for i in range(3)))
    # ...but the most web-looking of all the photos.
    assert score.web == pytest.approx(photos[3][1])
    assert (score.photos, score.model) == (4, "m")


def test_score_vectors_needs_photos_and_positives() -> None:
    with pytest.raises(VisionError, match="no photos"):
        score_vectors([], [WANTED], [], AXIS, WEB_AXIS, model_id="m")
    with pytest.raises(VisionError, match="no positive"):
        score_vectors([WANTED], [], [], AXIS, WEB_AXIS, model_id="m")
    assert score_vectors([WANTED], [WANTED], [], AXIS, WEB_AXIS, model_id="m").negative == 0.0


def test_prompt_axis_averages_the_prompt_pairs() -> None:
    assert prompt_axis([[1.0, 0.0], [0.5, 0.5]], [[0.0, 1.0], [0.5, -0.5]]) == [0.5, 0.0]


def score(match: float, colour: float, web: float | None = None) -> VisionScore:
    return VisionScore(
        match=match, negative=0.0, colour=colour, best_photo=0, photos=1, model="m", web=web
    )


def test_below_threshold_reason() -> None:
    shadow = VisionConfig(match_threshold=0.9, colour_threshold=0.5, web_threshold=0.01)
    assert below_threshold_reason(score(0.1, -1.0, -1.0), shadow) is None
    active = shadow.model_copy(update={"filter": True})
    assert "not similar enough" in (below_threshold_reason(score(0.8, 0.6), active) or "")
    assert "gold-tone" in (below_threshold_reason(score(0.95, 0.4), active) or "")
    assert "no spider web" in (below_threshold_reason(score(0.95, 0.6, 0.0), active) or "")
    assert below_threshold_reason(score(0.95, 0.6, 0.02), active) is None
    # Scored before the web probe existed: not filtered on it.
    assert below_threshold_reason(score(0.95, 0.6), active) is None


def test_leave_one_out_and_suggested_thresholds(tmp_path: Path) -> None:
    from ebay_sniper.vision import ReferenceImage

    refs = References(
        positives=(
            ReferenceImage(tmp_path / "p1", unit(1, 0, 0.1)),
            ReferenceImage(tmp_path / "p2", unit(1, 0.1, 0.05)),
        ),
        negatives=(ReferenceImage(tmp_path / "n1", unit(1, 0, -0.3)),),
        colour_axis=AXIS,
        web_axis=WEB_AXIS,
    )
    results = score_references(refs, "m")
    assert [(r.path.name, r.positive) for r in results] == [
        ("p1", True),
        ("p2", True),
        ("n1", False),
    ]
    # Each positive is compared with the other one only.
    assert results[0].score.match == pytest.approx(
        sum(a * b for a, b in zip(*[r.vector for r in refs.positives], strict=True))
    )
    match, colour, web = suggest_thresholds(results)
    assert match == round(min(r.score.match for r in results[:2]) - 0.05, 3)
    assert colour == round(min(r.score.colour for r in results[:2]) - 0.01, 3)
    # p1 has no component along the web axis.
    assert web == -0.01


def test_embedding_cache_computes_each_blob_once(tmp_path: Path) -> None:
    calls: list[list[bytes]] = []

    def compute(blobs: Sequence[bytes]) -> list[list[float]]:
        calls.append(list(blobs))
        return [[float(len(blob)), 0.5] for blob in blobs]

    cache = EmbeddingCache(tmp_path, "Model/pre")
    assert cache.get_or_compute([b"a", b"bb"], compute) == [[1.0, 0.5], [2.0, 0.5]]
    assert cache.get_or_compute([b"bb", b"ccc"], compute) == [[2.0, 0.5], [3.0, 0.5]]
    assert calls == [[b"a", b"bb"], [b"ccc"]]
    # Another model does not share vectors.
    EmbeddingCache(tmp_path, "Other/pre").get_or_compute([b"a"], compute)
    assert calls[-1] == [b"a"]


def test_image_cache_prefers_the_configured_size_and_caches(
    tmp_path: Path, respx_mock: respx.MockRouter
) -> None:
    resized = respx_mock.get(PHOTO.replace("s-l225", "s-l500")).respond(
        200, content=b"x", headers=JPEG
    )
    cache = ImageCache(httpx.Client(), tmp_path, size="s-l500")
    assert cache.fetch(PHOTO) == b"x"
    assert cache.fetch(PHOTO) == b"x"
    assert resized.call_count == 1


def test_image_cache_falls_back_to_the_original_url(
    tmp_path: Path, respx_mock: respx.MockRouter
) -> None:
    respx_mock.get(PHOTO.replace("s-l225", "s-l500")).respond(404)
    respx_mock.get(PHOTO).respond(200, content=b"y", headers=JPEG)
    assert ImageCache(httpx.Client(), tmp_path).fetch(PHOTO) == b"y"


def test_image_cache_rejects_non_images(tmp_path: Path, respx_mock: respx.MockRouter) -> None:
    respx_mock.get(url__startswith="https://i.ebayimg.com/").respond(
        200, content=b"<html>", headers={"content-type": "text/html"}
    )
    with pytest.raises(ImageFetchError, match="text/html"):
        ImageCache(httpx.Client(), tmp_path).fetch(PHOTO)


def test_image_cache_prune(tmp_path: Path, respx_mock: respx.MockRouter) -> None:
    respx_mock.get(url__startswith="https://i.ebayimg.com/").respond(
        200, content=b"x", headers=JPEG
    )
    cache = ImageCache(httpx.Client(), tmp_path, max_age=timedelta(days=1))
    cache.fetch(PHOTO)
    cache.fetch(PHOTO.replace("AAA", "BBB"))
    old = next(tmp_path.glob("*/*.img"))
    stale = time.time() - 2 * 86400
    os.utime(old, (stale, stale))
    assert cache.prune() == 1
    assert len(list(tmp_path.glob("*/*.img"))) == 1


class FakeEmbedder:
    """Image bytes name their direction: b"wanted...", b"other...", b"gold...".

    Texts: silver-tone along the colour axis, gold-tone against it; spider-web
    dials along the wanted direction, plain dials against it.
    """

    model_id = "fake/model"

    def __init__(self) -> None:
        self.image_batches: list[int] = []

    def embed_images(self, images: Sequence[bytes]) -> list[list[float]]:
        self.image_batches.append(len(images))
        vectors = []
        for data in images:
            if data.startswith(b"wanted"):
                vectors.append(unit(1, 0, 0.2))
            elif data.startswith(b"gold"):
                vectors.append(unit(1, 0, -0.2))
            else:
                vectors.append(unit(0, 1, 0))
        return vectors

    def embed_texts(self, texts: Sequence[str]) -> list[list[float]]:
        vectors = []
        for text in texts:
            if "silver" in text or "steel" in text:
                vectors.append([0.0, 0.0, 1.0])
            elif "gold" in text:
                vectors.append([0.0, 0.0, -1.0])
            elif "web" in text:
                vectors.append([1.0, 0.0, 0.0])
            else:
                vectors.append([-1.0, 0.0, 0.0])
        return vectors


@pytest.fixture
def references(tmp_path: Path) -> VisionConfig:
    positive, negative = tmp_path / "positive", tmp_path / "negative"
    positive.mkdir()
    negative.mkdir()
    (positive / "a.jpg").write_bytes(b"wanted-a")
    (positive / "b.webp").write_bytes(b"wanted-b")
    (positive / "notes.txt").write_text("not an image")
    (negative / "gold.jpg").write_bytes(b"gold-1")
    return VisionConfig(enabled=True, positive_dir=positive, negative_dir=negative, max_photos=2)


def scorer_for(config: VisionConfig, tmp_path: Path, embedder: FakeEmbedder) -> VisionScorer:
    return VisionScorer(
        config,
        embedder,
        EmbeddingCache(tmp_path / "emb", embedder.model_id),
        ImageCache(httpx.Client(), tmp_path / "photos"),
    )


def test_reference_files_skip_other_files(references: VisionConfig) -> None:
    assert [p.name for p in reference_files(references.positive_dir)] == ["a.jpg", "b.webp"]
    assert reference_files(references.positive_dir / "missing") == []


def test_scorer_downloads_the_first_photos_and_scores_them(
    references: VisionConfig, tmp_path: Path, respx_mock: respx.MockRouter
) -> None:
    urls = [f"https://i.ebayimg.com/images/g/{n}/s-l225.jpg" for n in ("X", "Y", "Z")]
    respx_mock.get(url__startswith="https://i.ebayimg.com/images/g/X/").respond(
        200, content=b"other", headers=JPEG
    )
    respx_mock.get(url__startswith="https://i.ebayimg.com/images/g/Y/").respond(
        200, content=b"wanted-photo", headers=JPEG
    )
    embedder = FakeEmbedder()
    result = scorer_for(references, tmp_path, embedder).score(urls)
    assert (result.best_photo, result.photos, result.model) == (1, 2, "fake/model")
    assert result.match > 0.9
    assert result.colour > 0
    assert result.web is not None and result.web > 1
    assert len(COLOUR_PROMPTS) == len(WEB_PROMPTS) == 4
    # Third photo beyond max_photos: never requested (respx would fail on it).


def test_scorer_skips_failed_downloads_and_fails_without_photos(
    references: VisionConfig, tmp_path: Path, respx_mock: respx.MockRouter
) -> None:
    respx_mock.get(url__startswith="https://i.ebayimg.com/images/g/X/").respond(404)
    respx_mock.get(url__startswith="https://i.ebayimg.com/images/g/Y/").respond(
        200, content=b"gold-photo", headers=JPEG
    )
    scorer = scorer_for(references, tmp_path, FakeEmbedder())
    x = "https://i.ebayimg.com/images/g/X/s-l225.jpg"
    y = "https://i.ebayimg.com/images/g/Y/s-l225.jpg"
    assert scorer.score([x, y]).colour < 0
    with pytest.raises(VisionError, match="no photo could be downloaded"):
        scorer.score([x])
    with pytest.raises(VisionError, match="no photos"):
        scorer.score([])


def test_scorer_without_positive_references(tmp_path: Path) -> None:
    config = VisionConfig(enabled=True, positive_dir=tmp_path / "none", negative_dir=tmp_path)
    with pytest.raises(VisionError, match="no images"):
        scorer_for(config, tmp_path, FakeEmbedder()).references()


def test_photos_best_first() -> None:
    listing = Listing(
        listing_id="1",
        item_id="v1|1|0",
        title="t",
        url="u",
        marketplace="EBAY_IT",
        query="q",
        image_urls=("a", "b", "c"),
    )
    assert listing.photos_best_first == ("a", "b", "c")
    assert listing.with_vision(score(0.9, 0.0)).photos_best_first == ("a", "b", "c")
    best_c = VisionScore(match=0.9, negative=0, colour=0, best_photo=2, photos=3, model="m")
    assert listing.with_vision(best_c).photos_best_first == ("c", "a", "b")


@pytest.mark.vision
@pytest.mark.skipif(not vision_available(), reason="vision extra not installed")
def test_real_model_separates_the_reference_sets(tmp_path: Path) -> None:
    """Loads the configured model (downloaded on first use): run with -m vision."""
    from ebay_sniper.config import load_config

    config = load_config(Path(__file__).parents[1] / "config.toml")
    from ebay_sniper.vision import OpenClipEmbedder

    embedder = OpenClipEmbedder(config.vision.model, config.vision.pretrained)
    scorer = VisionScorer(
        config.vision,
        embedder,
        # An empty cache, so that the model really runs.
        EmbeddingCache(tmp_path / "embeddings", embedder.model_id),
        ImageCache(httpx.Client(), tmp_path / "photos"),
    )
    results = score_references(scorer.references(), embedder.model_id)
    match, colour, web = suggest_thresholds(results)
    active = config.vision.model_copy(
        update={
            "filter": True,
            "match_threshold": match,
            "colour_threshold": colour,
            "web_threshold": web,
        }
    )
    gold = [r for r in results if "gold" in r.path.name and "futura" in r.path.name]
    assert gold
    assert all(below_threshold_reason(r.score, active) for r in gold)
    # The same case with a plain dial is only told apart by the web probe.
    plain = [r for r in results if "plain-dial" in r.path.name]
    assert plain
    assert all("spider web" in (below_threshold_reason(r.score, active) or "") for r in plain)
    assert all(below_threshold_reason(r.score, active) is None for r in results if r.positive)
