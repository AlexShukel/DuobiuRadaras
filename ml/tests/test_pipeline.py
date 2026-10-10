import json

import numpy as np

from pothole_ml import dataset, plot, predict, synth, train
from pothole_ml.config import PipelineConfig
from pothole_ml.predict import Candidate, nms


def test_nms_merges_overlapping_candidates():
    cands = [Candidate(1000 + d, 0, 0, 1, s, i)
             for i, (d, s) in enumerate([(0, 0.6), (50, 0.9), (100, 0.7), (150, 0.8), (200, 0.5)])]
    kept = nms(cands, nms_ms=600)
    assert len(kept) == 1 and kept[0].score == 0.9 and kept[0].t_ms == 1050


def test_nms_keeps_separated_candidates():
    cands = [Candidate(1000, 0, 0, 1, 0.9, 0), Candidate(3000, 0, 0, 2, 0.8, 1)]
    assert [c.t_ms for c in nms(cands, 600)] == [1000, 3000]


def test_end_to_end_smoke(tmp_path):
    rng = np.random.default_rng(0)
    chunks, labels = synth.synth_session(rng, minutes=3, n_potholes=6, n_bumps=6, drop_rate=0.0)
    synth.write_session(tmp_path / "synth", chunks, labels)

    cfg = PipelineConfig()
    out = tmp_path / "dataset.npz"
    ws, cfg, meta = dataset.build_dataset([tmp_path / "synth/raw.json"],
                                          [tmp_path / "synth/labels.json"], cfg, out, verbose=False)
    assert meta["alignment"]["matched"] == 12
    assert meta["windows"]["pothole"] > 0 and meta["windows"]["bump"] > 0

    data = dataset.load_dataset(out)
    art = tmp_path / "artifacts"
    train.train(cfg, data, art, epochs=2, patience=5, verbose=False)
    for name in ("model.pt", "config.json", "report.txt", "report.json", "history.json"):
        assert (art / name).exists()

    result = predict.predict_file(tmp_path / "synth/raw.json", art / "model.pt", art / "config.json")
    assert set(result) == {"events", "windows", "summary"}
    assert result["summary"]["windows"] == len(result["windows"])
    for e in result["events"]:
        assert set(e) == {"timestamp", "iso", "latitude", "longitude", "label", "score"}
        assert e["label"] in ("pothole", "bump")

    # the HTML report renders from the saved artifacts
    page = plot.build_report(out, art)
    assert "<svg" in page and "How the recording was split" in page

    # a single chunk object is accepted too
    one = tmp_path / "one.json"
    one.write_text(json.dumps(chunks[0]))
    single = predict.predict_file(one, art / "model.pt", art / "config.json", include_windows=False)
    assert single["summary"]["chunks"] == 1 and single["summary"]["windows"] == 3
    assert "windows" not in single
