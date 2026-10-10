"""The streaming detector must find the same events as batch prediction, once each."""

import io
import json
from pathlib import Path

import numpy as np

from pothole_ml.config import PipelineConfig
from pothole_ml.predict import predict_json
from pothole_ml.serve import StreamingDetector, serve
from pothole_ml.synth import synth_session
from pothole_ml.train import train
from pothole_ml.dataset import build_dataset, load_dataset


def _trained(tmp_path: Path):
    raw, labels = synth_session(np.random.default_rng(1), minutes=3, n_potholes=6, n_bumps=6, drop_rate=0.05)
    raw_path, labels_path = tmp_path / "raw.json", tmp_path / "labels.json"
    raw_path.write_text(json.dumps(raw))
    labels_path.write_text(json.dumps(labels))
    build_dataset([raw_path], [labels_path], PipelineConfig(), tmp_path / "ds.npz", verbose=False)
    cfg = PipelineConfig.from_dict(json.loads((tmp_path / "ds.meta.json").read_text())["config"])
    train(cfg, load_dataset(tmp_path / "ds.npz"), tmp_path / "art", epochs=3, verbose=False)
    return raw, tmp_path / "art" / "model.pt", tmp_path / "art" / "config.json"


def test_streaming_matches_batch_and_never_repeats(tmp_path):
    raw, model_path, config_path = _trained(tmp_path)
    det = StreamingDetector(model_path, config_path)
    batch = predict_json(raw, det.model, det.cfg, include_windows=False)["events"]

    streamed = []
    for chunk in raw:
        reply = det.push("phone", chunk)
        assert "error" not in reply
        streamed += reply["events"]
    # the very last lag_ms of the drive is still pending, so compare up to the final cutoff
    cutoff = det.devices["phone"].emitted_until_ms
    expected = [e for e in batch if e["timestamp"] <= cutoff]
    assert [e["timestamp"] for e in streamed] == [e["timestamp"] for e in expected]
    assert [e["label"] for e in streamed] == [e["label"] for e in expected]
    ts = np.array([e["timestamp"] for e in streamed])
    assert (np.diff(ts) >= det.cfg.nms_ms).all()


def test_devices_are_independent(tmp_path):
    raw, model_path, config_path = _trained(tmp_path)
    det = StreamingDetector(model_path, config_path)
    a = [e for c in raw[:30] for e in det.push("a", c)["events"]]
    b = [e for c in raw[:30] for e in det.push("b", c)["events"]]
    assert a == b
    det.reset("a")
    assert "a" not in det.devices and "b" in det.devices


def test_serve_protocol_survives_bad_input(tmp_path):
    raw, model_path, config_path = _trained(tmp_path)
    det = StreamingDetector(model_path, config_path)
    requests = [json.dumps({"ping": True}), "garbage", json.dumps({"device": "x"}),
                json.dumps({"device": "x", "chunk": raw[0]}), json.dumps({"device": "x", "reset": True}),
                json.dumps({"device": "x", "chunk": {"started_at": raw[0]["started_at"], "samples": []}})]
    out = io.StringIO()
    serve(det, io.StringIO("\n".join(requests) + "\n"), out)
    replies = [json.loads(line) for line in out.getvalue().splitlines()]
    assert len(replies) == len(requests)
    assert replies[0]["ok"] is True and replies[0]["lag_ms"] == det.lag_ms
    assert "error" in replies[1]
    assert replies[2] == {"device": "x", "error": "missing chunk"}
    assert replies[3]["device"] == "x" and "events" in replies[3]
    assert replies[4] == {"device": "x", "ok": True}
    assert replies[5]["events"] == [] and replies[5]["warning"] == "empty chunk"
